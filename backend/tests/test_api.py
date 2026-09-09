import hashlib
import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from deepcontrib.api import create_app
from deepcontrib.models import ArtifactRecord, TaskRecord, TaskStatus, utc_now
from deepcontrib.patch_generation import PatchProposal
from deepcontrib.plan import FileReference, ImplementationPlan, save_plan
from deepcontrib.publish import PublishResult, PublishStep
from deepcontrib.service import TaskExecution
from deepcontrib.store import InMemoryTaskStore
from deepcontrib.test_runner import TestResult as TestRunResult
from deepcontrib.test_runner import VerificationResult


def _client(tmp_path: Path) -> tuple[TestClient, InMemoryTaskStore]:
    store = InMemoryTaskStore()

    def executor(task):
        path = tmp_path / f"{task.task_id}.json"
        path.write_text('{"problem": "fixture"}\n', encoding="utf-8")
        payload = path.read_bytes()
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{task.task_id}",
            task_id=task.task_id,
            kind="plan_json",
            version=1,
            path=str(path),
            sha256=hashlib.sha256(payload).hexdigest(),
            base_sha="a" * 40,
            created_at=utc_now(),
        )
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha="a" * 40,
            artifacts=(artifact,),
        )

    app = create_app(store=store, executor=executor, auto_start=False)
    return TestClient(app), store


def test_task_api_covers_create_snapshot_events_and_cancel(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    created = client.post(
        "/api/v1/tasks",
        json={
            "repo": "https://github.com/acme/project",
            "issue": 12,
            "thread_id": "api-thread",
        },
    )
    task_id = created.json()["data"]["task_id"]

    assert created.status_code == 201
    assert created.headers["location"].endswith(task_id)
    fetched = client.get(f"/api/v1/tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["status"] == "queued"

    events = client.get(f"/api/v1/tasks/{task_id}/events")
    assert events.status_code == 200
    assert "task.created" in events.text
    assert events.headers["content-type"].startswith("text/event-stream")

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert client.get("/api/v1/tasks/missing").status_code == 404


def test_health_endpoint_reports_local_capabilities(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    assert response.json()["data"]["single_user"] is True


def test_task_export_contains_metadata_and_artifacts(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    created = client.post(
        "/api/v1/tasks", json={"repo": "https://github.com/acme/project", "issue": 1}
    )
    task_id = created.json()["data"]["task_id"]
    client.post(f"/api/v1/tasks/{task_id}/resume")

    exported = client.get(f"/api/v1/tasks/{task_id}/export")

    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        assert "task.json" in names
        assert "approvals.json" in names
        assert "events.json" in names
        assert any(name.startswith("artifacts/") for name in names)


def test_task_api_runs_stage_approves_artifact_and_supports_sse_cursor(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path)
    created = client.post(
        "/api/v1/tasks", json={"repo": "https://github.com/acme/project", "issue": 1}
    )
    task_id = created.json()["data"]["task_id"]
    resumed = client.post(f"/api/v1/tasks/{task_id}/resume")
    assert resumed.json()["data"]["status"] == "awaiting_plan_approval"
    approval_event = next(
        item
        for item in store.list_events(task_id)
        if item.event_type == "approval.required"
    )
    approval_id = str(approval_event.payload["approval_id"])
    approval = store.get_approval(approval_id)
    assert approval is not None
    listed_approvals = client.get(f"/api/v1/tasks/{task_id}/approvals")
    assert listed_approvals.status_code == 200
    assert listed_approvals.json()["data"][0]["approval_id"] == approval_id

    artifact = client.get(
        f"/api/v1/tasks/{task_id}/artifacts/{approval_event.payload['artifact_id']}"
    )
    assert artifact.status_code == 200
    assert artifact.headers["x-artifact-sha256"] == approval.artifact_hash

    approved = client.post(
        f"/api/v1/tasks/{task_id}/approvals/{approval_id}",
        json={
            "decision": "approve",
            "artifact_hash": approval.artifact_hash,
            "base_sha": approval.base_sha,
        },
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "drafting_patch"

    replay = client.get(
        f"/api/v1/tasks/{task_id}/events",
        headers={"Last-Event-ID": str(approval_event.event_id)},
    )
    assert replay.status_code == 200
    assert "approval.required" not in replay.text


def test_task_api_auto_generates_patch_after_plan_approval(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    def executor(task: TaskRecord) -> TaskExecution:
        task_root = data_root / "tasks" / task.task_id
        snapshot = task_root / "snapshot"
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "src" / "parser.py").write_text(
            "old\n", encoding="utf-8", newline="\n"
        )
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Fix parser",
            base_sha="a" * 40,
            problem="old behavior",
            relevant_files=[
                FileReference(
                    path="src/parser.py", start_line=1, end_line=1, reason="evidence"
                )
            ],
        )
        artifacts = save_plan(plan, task_root / "artifacts")
        payload = artifacts.json_path.read_bytes()
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=plan.base_sha,
            artifacts=(
                ArtifactRecord(
                    artifact_id="plan-artifact",
                    task_id=task.task_id,
                    kind="plan_json",
                    version=1,
                    path=str(artifacts.json_path),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    base_sha=plan.base_sha,
                    created_at=utc_now(),
                ),
            ),
        )

    def generator(
        _task: TaskRecord, _plan: ImplementationPlan, _snapshot_root: Path
    ) -> PatchProposal:
        return PatchProposal(
            diff=("--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n")
        )

    store = InMemoryTaskStore()
    client = TestClient(
        create_app(
            store=store,
            executor=executor,
            auto_start=False,
            artifact_root=data_root,
            data_root=data_root,
            patch_generator=generator,
        )
    )
    task_id = client.post(
        "/api/v1/tasks", json={"repo": "https://github.com/acme/project", "issue": 1}
    ).json()["data"]["task_id"]
    assert client.post(f"/api/v1/tasks/{task_id}/resume").json()["data"]["status"] == (
        "awaiting_plan_approval"
    )
    plan_approval = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["data"][0]

    approved = client.post(
        f"/api/v1/tasks/{task_id}/approvals/{plan_approval['approval_id']}",
        json={
            "decision": "approve",
            "artifact_hash": plan_approval["artifact_hash"],
            "base_sha": plan_approval["base_sha"],
        },
    )

    assert approved.status_code == 200
    assert client.get(f"/api/v1/tasks/{task_id}").json()["data"]["status"] == (
        "awaiting_patch_approval"
    )
    artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["data"]
    assert any(item["kind"] == "patch" for item in artifacts)


def test_task_api_normalizes_validation_and_state_errors(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    invalid = client.post(
        "/api/v1/tasks",
        json={"repo": "https://gitlab.com/acme/project", "issue": 1},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_task"

    malformed = client.post(
        "/api/v1/tasks",
        json={"repo": "https://github.com/acme/project", "issue": 0},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "validation_error"


def test_task_api_creates_lists_downloads_and_approves_patch(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    class FakeRunner:
        def verify_red_green(
            self, baseline_root, patched_root, *, test_paths=("tests",)
        ):
            del baseline_root, patched_root, test_paths
            baseline = TestRunResult(
                status="failed",
                command=("docker", "run"),
                exit_code=1,
                stdout="failed",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
                worktree_digest_before="a",
                worktree_digest_after="a",
            )
            patched = TestRunResult(
                status="passed",
                command=baseline.command,
                exit_code=0,
                stdout="passed",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
                worktree_digest_before="b",
                worktree_digest_after="b",
            )
            return VerificationResult(
                status="red_green",
                baseline=baseline,
                patched=patched,
                message="baseline failed and patched copy passed",
            )

    class FakePublisher:
        def publish(self, request):
            return PublishResult(
                status="completed",
                branch=request.branch,
                commit_sha="f" * 40,
                pr_url="https://github.com/acme/project/pull/2",
                error=None,
                steps=(PublishStep("draft_pr", "completed", "created"),),
            )

    def executor(task: TaskRecord) -> TaskExecution:
        task_root = data_root / "tasks" / task.task_id
        snapshot = task_root / "snapshot"
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "src" / "slugger.py").write_text(
            "old\n", encoding="utf-8", newline="\n"
        )
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Fix slug",
            base_sha="a" * 40,
            problem="old behavior",
            relevant_files=[
                FileReference(
                    path="src/slugger.py",
                    start_line=1,
                    end_line=1,
                    reason="evidence",
                )
            ],
        )
        artifacts = save_plan(plan, task_root / "artifacts")
        payload = artifacts.json_path.read_bytes()
        artifact = ArtifactRecord(
            artifact_id="plan-artifact",
            task_id=task.task_id,
            kind="plan_json",
            version=1,
            path=str(artifacts.json_path),
            sha256=hashlib.sha256(payload).hexdigest(),
            base_sha=plan.base_sha,
            created_at=utc_now(),
        )
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=plan.base_sha,
            artifacts=(artifact,),
        )

    store = InMemoryTaskStore()
    client = TestClient(
        create_app(
            store=store,
            executor=executor,
            auto_start=False,
            artifact_root=data_root,
            data_root=data_root,
            test_runner=FakeRunner(),
            publisher=FakePublisher(),
        )
    )
    task_id = client.post(
        "/api/v1/tasks", json={"repo": "https://github.com/acme/project", "issue": 1}
    ).json()["data"]["task_id"]
    assert client.post(f"/api/v1/tasks/{task_id}/resume").json()["data"]["status"] == (
        "awaiting_plan_approval"
    )
    plan_approval = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["data"][0]
    approved = client.post(
        f"/api/v1/tasks/{task_id}/approvals/{plan_approval['approval_id']}",
        json={
            "decision": "approve",
            "artifact_hash": plan_approval["artifact_hash"],
            "base_sha": plan_approval["base_sha"],
        },
    )
    assert approved.json()["data"]["status"] == "drafting_patch"

    diff = """--- a/src/slugger.py
+++ b/src/slugger.py
@@ -1 +1 @@
-old
+new
"""
    created = client.post(f"/api/v1/tasks/{task_id}/patches", json={"diff": diff})
    assert created.status_code == 200
    assert created.json()["data"]["status"] == "awaiting_patch_approval"
    listed = client.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert listed.status_code == 200
    patch_artifact = next(
        item for item in listed.json()["data"] if item["kind"] == "patch"
    )
    assert "path" not in patch_artifact
    downloaded = client.get(
        f"/api/v1/tasks/{task_id}/artifacts/{patch_artifact['artifact_id']}"
    )
    assert downloaded.headers["content-type"].startswith("text/x-diff")
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.text == diff

    patch_approval = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["data"][-1]
    applied = client.post(
        f"/api/v1/tasks/{task_id}/approvals/{patch_approval['approval_id']}",
        json={
            "decision": "approve",
            "artifact_hash": patch_approval["artifact_hash"],
            "base_sha": patch_approval["base_sha"],
        },
    )
    assert applied.json()["data"]["status"] == "testing"
    assert (data_root / "tasks" / task_id / "working" / "src" / "slugger.py").read_text(
        encoding="utf-8"
    ) == "new\n"
    tested = client.post(f"/api/v1/tasks/{task_id}/tests")
    assert tested.status_code == 200
    assert tested.json()["data"]["status"] == "reviewing"
    reviewed = client.post(f"/api/v1/tasks/{task_id}/reviews")
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["status"] == "ready_to_publish"
    review_artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["data"]
    assert any(item["kind"] == "review_report" for item in review_artifacts)
    prepared = client.post(
        f"/api/v1/tasks/{task_id}/publish",
        json={
            "fork_owner": "contributor",
            "title": "Fix slug",
            "body": "The approved patch passed isolated tests and review.",
            "base_branch": "main",
        },
    )
    assert prepared.status_code == 200
    assert prepared.json()["data"]["status"] == "awaiting_publish_approval"
    publish_approval = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["data"][
        -1
    ]
    published = client.post(
        f"/api/v1/tasks/{task_id}/approvals/{publish_approval['approval_id']}",
        json={
            "decision": "approve",
            "artifact_hash": publish_approval["artifact_hash"],
            "base_sha": publish_approval["base_sha"],
        },
    )
    assert published.status_code == 200
    assert published.json()["data"]["status"] == "completed"


def test_memory_api_requires_explicit_remember_and_keeps_repo_namespaces_separate(
    tmp_path: Path,
) -> None:
    client, _store = _client(tmp_path)

    rejected = client.post(
        "/api/v1/memories",
        json={
            "scope": "preferences",
            "key": "format",
            "value": "concise",
            "remember": False,
        },
    )
    assert rejected.status_code == 422

    saved = client.post(
        "/api/v1/memories",
        json={
            "scope": "preferences",
            "key": "format",
            "value": "concise",
            "remember": True,
        },
    )
    assert saved.status_code == 201
    memory_id = saved.json()["data"]["memory_id"]
    listed = client.get("/api/v1/memories", params={"scope": "preferences"})
    assert listed.status_code == 200
    assert listed.json()["data"][0]["namespace"] == "preferences"

    repo_saved = client.post(
        "/api/v1/memories",
        json={
            "scope": "repository",
            "repo": "https://github.com/acme/project",
            "key": "test_command",
            "value": "pytest -q",
            "remember": True,
        },
    )
    assert repo_saved.status_code == 201
    other_repo = client.get(
        "/api/v1/memories",
        params={"scope": "repository", "repo": "https://github.com/acme/other"},
    )
    assert other_repo.json()["data"] == []

    deleted = client.delete(f"/api/v1/memories/{memory_id}")
    assert deleted.status_code == 204
    assert (
        client.get("/api/v1/memories", params={"scope": "preferences"}).json()["data"]
        == []
    )
