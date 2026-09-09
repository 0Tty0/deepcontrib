import hashlib
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from deepcontrib.models import (
    ApprovalDecision,
    ArtifactRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from deepcontrib.patch_generation import PatchProposal
from deepcontrib.plan import FileReference, ImplementationPlan, save_plan
from deepcontrib.publish import PublishResult, PublishStep
from deepcontrib.review import ReviewResult
from deepcontrib.service import (
    ServiceError,
    StateConflictError,
    TaskBusyError,
    TaskExecution,
    TaskService,
)
from deepcontrib.store import InMemoryTaskStore
from deepcontrib.test_runner import TestResult as TestRunResult
from deepcontrib.test_runner import VerificationResult


def _artifact(task_id: str, path: Path) -> ArtifactRecord:
    payload = path.read_bytes()
    return ArtifactRecord(
        artifact_id=f"artifact-{task_id}",
        task_id=task_id,
        kind="plan_json",
        version=1,
        path=str(path),
        sha256=hashlib.sha256(payload).hexdigest(),
        base_sha="a" * 40,
        created_at=utc_now(),
    )


def _service(tmp_path: Path, executor=None) -> TaskService:
    store = InMemoryTaskStore()
    if executor is None:

        def executor(task):
            path = tmp_path / f"{task.task_id}.json"
            path.write_text('{"plan": true}\n', encoding="utf-8")
            return TaskExecution(
                status=TaskStatus.AWAITING_PLAN_APPROVAL,
                base_sha="a" * 40,
                artifacts=(_artifact(task.task_id, path),),
            )

    return TaskService(store, executor=executor)


def test_task_service_runs_analysis_creates_approval_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    task = service.create_task("https://github.com/acme/project", 4)

    waiting = service.start_task(task.task_id)
    assert waiting.status == TaskStatus.AWAITING_PLAN_APPROVAL
    events = service.list_events(task.task_id)
    stage_event = next(
        item for item in events if item.event_type == "task.stage_completed"
    )
    assert isinstance(stage_event.payload["duration_seconds"], float)
    approval_event = next(
        item for item in events if item.event_type == "approval.required"
    )
    approval_id = str(approval_event.payload["approval_id"])
    approval = service.store.get_approval(approval_id)
    assert approval is not None

    approved = service.submit_approval(
        task.task_id,
        approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=approval.artifact_hash,
        base_sha=approval.base_sha,
    )
    again = service.submit_approval(
        task.task_id,
        approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=approval.artifact_hash,
        base_sha=approval.base_sha,
    )

    assert approved.status == TaskStatus.DRAFTING_PATCH
    assert again == approved
    assert service.get_task(task.task_id).status == TaskStatus.DRAFTING_PATCH


def test_task_service_resumes_an_already_approved_plan(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task("https://github.com/acme/project", 4)
    service.start_task(task.task_id)
    approval = service.list_approvals(task.task_id)[0]
    service.store.save_approval(
        replace(
            approval,
            decision=ApprovalDecision.APPROVE,
            consumed_at=utc_now(),
        )
    )

    resumed = service.resume_task(task.task_id)

    assert resumed.status == TaskStatus.DRAFTING_PATCH


def test_task_service_generates_patch_after_plan_approval(tmp_path: Path) -> None:
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

    calls: list[tuple[str, str, Path]] = []

    def generator(
        task: TaskRecord, plan: ImplementationPlan, snapshot_root: Path
    ) -> PatchProposal:
        calls.append((task.task_id, plan.base_sha, snapshot_root))
        return PatchProposal(
            diff=("--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n")
        )

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
        patch_generator=generator,
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=approval.artifact_hash,
        base_sha=approval.base_sha,
    )

    generated = service.generate_patch(task.task_id)

    assert generated.status == TaskStatus.AWAITING_PATCH_APPROVAL
    assert calls == [
        (
            task.task_id,
            "a" * 40,
            data_root / "tasks" / task.task_id / "snapshot",
        )
    ]
    assert any(item.kind == "patch" for item in service.list_artifacts(task.task_id))


def test_task_service_binds_approval_to_task_artifact_and_base(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task("https://github.com/acme/project", 4)
    waiting = service.start_task(task.task_id)
    assert waiting.base_sha == "a" * 40
    approval_id = next(
        item.payload["approval_id"]
        for item in service.list_events(task.task_id)
        if item.event_type == "approval.required"
    )
    approval = service.store.get_approval(str(approval_id))
    assert approval is not None

    with pytest.raises(StateConflictError, match="artifact"):
        service.submit_approval(
            task.task_id,
            approval.approval_id,
            ApprovalDecision.APPROVE,
            artifact_hash="b" * 64,
            base_sha=approval.base_sha,
        )
    with pytest.raises(StateConflictError, match="base SHA"):
        service.submit_approval(
            task.task_id,
            approval.approval_id,
            ApprovalDecision.APPROVE,
            artifact_hash=approval.artifact_hash,
            base_sha="b" * 40,
        )


def test_task_service_failures_are_persisted_and_cancel_is_idempotent(
    tmp_path: Path,
) -> None:
    def failing(_task):
        raise RuntimeError("controlled failure")

    service = _service(tmp_path, executor=failing)
    task = service.create_task("https://github.com/acme/project", 1)
    failed = service.start_task(task.task_id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error == "controlled failure"
    assert service.cancel_task(task.task_id) == failed


def test_task_service_rejects_invalid_inputs_and_resume_states(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ServiceError, match="HTTPS github.com"):
        service.create_task("https://gitlab.com/acme/project", 1)
    task = service.create_task(
        "https://github.com/acme/project", 1, thread_id="safe-thread"
    )
    waiting = service.start_task(task.task_id)
    with pytest.raises(StateConflictError, match="approval"):
        service.resume_task(task.task_id)
    assert service.cancel_task(task.task_id).status == TaskStatus.CANCELLED
    assert service.resume_task(task.task_id).status == TaskStatus.CANCELLED
    assert waiting.status == TaskStatus.AWAITING_PLAN_APPROVAL


def test_task_service_rejects_corrupt_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    artifact = service.store.get_artifact(task.task_id, f"artifact-{task.task_id}")
    assert artifact is not None
    Path(artifact.path).write_text("tampered", encoding="utf-8")

    with pytest.raises(ServiceError, match="integrity"):
        service.read_artifact(task.task_id, artifact.artifact_id)


def test_task_service_rejects_artifact_path_outside_configured_root(
    tmp_path: Path,
) -> None:
    service = TaskService(
        InMemoryTaskStore(),
        executor=lambda _task: TaskExecution(status=TaskStatus.AWAITING_PLAN_APPROVAL),
        artifact_root=tmp_path / "allowed",
    )
    task = service.create_task("https://github.com/acme/project", 1)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    payload = outside.read_bytes()
    artifact = ArtifactRecord(
        artifact_id="outside-artifact",
        task_id=task.task_id,
        kind="plan_json",
        version=1,
        path=str(outside),
        sha256=hashlib.sha256(payload).hexdigest(),
        base_sha=None,
        created_at=utc_now(),
    )
    service.store.add_artifact(artifact)

    with pytest.raises(ServiceError, match="artifact"):
        service.read_artifact(task.task_id, artifact.artifact_id)


def test_task_service_allows_only_one_active_task(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(task):
        entered.set()
        release.wait(timeout=2)
        path = tmp_path / f"{task.task_id}.json"
        path.write_text("{}", encoding="utf-8")
        return TaskExecution(status=TaskStatus.AWAITING_PLAN_APPROVAL)

    service = _service(tmp_path, executor=blocking)
    first = service.create_task("https://github.com/acme/project", 1)
    second = service.create_task("https://github.com/acme/project", 2)
    worker = threading.Thread(target=service.start_task, args=(first.task_id,))
    worker.start()
    assert entered.wait(timeout=1)
    with pytest.raises(TaskBusyError):
        service.start_task(second.task_id)
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_task_service_cancels_an_active_analysis_after_executor_returns(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(_task: TaskRecord) -> TaskExecution:
        entered.set()
        release.wait(timeout=2)
        return TaskExecution(status=TaskStatus.AWAITING_PLAN_APPROVAL)

    service = _service(tmp_path, executor=blocking)
    task = service.create_task("https://github.com/acme/project", 1)
    worker = threading.Thread(target=service.start_task, args=(task.task_id,))
    worker.start()
    assert entered.wait(timeout=1)

    requested = service.cancel_task(task.task_id)

    assert requested.status == TaskStatus.ANALYZING
    assert any(
        event.event_type == "task.cancel_requested"
        for event in service.list_events(task.task_id)
    )
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert service.get_task(task.task_id).status == TaskStatus.CANCELLED


def test_task_service_creates_and_applies_approved_patch_in_isolated_copy(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"

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
                    path="src/slugger.py", start_line=1, end_line=1, reason="evidence"
                )
            ],
        )
        artifacts_dir = task_root / "artifacts"
        plan_artifacts = save_plan(plan, artifacts_dir)
        payload = plan_artifacts.json_path.read_bytes()
        artifact = ArtifactRecord(
            artifact_id="plan-artifact",
            task_id=task.task_id,
            kind="plan_json",
            version=1,
            path=str(plan_artifacts.json_path),
            sha256=hashlib.sha256(payload).hexdigest(),
            base_sha=plan.base_sha,
            created_at=utc_now(),
        )
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=plan.base_sha,
            artifacts=(artifact,),
        )

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
    )
    task = service.create_task("https://github.com/acme/project", 1)
    waiting = service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    diff = """--- a/src/slugger.py
+++ b/src/slugger.py
@@ -1 +1 @@
-old
+new
"""

    patch_task, summary = service.create_patch(task.task_id, diff)
    patch_approval = service.list_approvals(task.task_id)[-1]
    service.store.save_approval(
        replace(
            patch_approval,
            decision=ApprovalDecision.APPROVE,
            consumed_at=utc_now(),
        )
    )
    # Simulate a process crash after approval persistence but before the apply
    # stage. Resume must consume the already approved action exactly once.
    applied = service.resume_task(task.task_id)

    assert waiting.status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert patch_task.status == TaskStatus.AWAITING_PATCH_APPROVAL
    assert summary.changed_paths == ("src/slugger.py",)
    assert applied.status == TaskStatus.TESTING
    assert (
        data_root / "tasks" / task.task_id / "working" / "src" / "slugger.py"
    ).read_text(encoding="utf-8") == "new\n"

    assert patch_task.current_artifact_id is not None
    patch_record = service.store.get_artifact(
        task.task_id, patch_task.current_artifact_id
    )
    assert patch_record is not None
    Path(patch_record.path).write_text("tampered\n", encoding="utf-8")
    service.store.save_task(
        replace(
            service.get_task(task.task_id),
            status=TaskStatus.AWAITING_PATCH_APPROVAL,
            error=None,
        )
    )

    failed = service.resume_task(task.task_id)

    assert failed.status == TaskStatus.FAILED
    assert failed.current_artifact_id is not None
    failure_record = service.store.get_artifact(
        task.task_id, failed.current_artifact_id
    )
    assert failure_record is not None
    assert failure_record.kind == "patch_apply_failure"
    assert failed.error is not None
    assert "integrity" in failed.error


def test_task_service_reanalyzes_after_edit_decision(tmp_path: Path) -> None:
    calls = 0

    def executor(task: TaskRecord) -> TaskExecution:
        nonlocal calls
        calls += 1
        path = tmp_path / f"plan-{calls}.json"
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Fix",
            base_sha="a" * 40,
            problem=f"iteration {calls}",
        )
        path.write_text(plan.model_dump_json(), encoding="utf-8")
        payload = path.read_bytes()
        artifact = ArtifactRecord(
            artifact_id=f"plan-{calls}",
            task_id=task.task_id,
            kind="plan_json",
            version=calls,
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

    service = TaskService(InMemoryTaskStore(), executor=executor)
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    approval = service.list_approvals(task.task_id)[0]
    edited = service.submit_approval(
        task.task_id,
        approval.approval_id,
        ApprovalDecision.EDIT,
        artifact_hash=approval.artifact_hash,
        base_sha=approval.base_sha,
        feedback="Please clarify the test.",
    )
    resumed = service.resume_task(task.task_id)

    assert edited.status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert resumed.status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert calls == 2
    assert len(service.list_approvals(task.task_id)) == 2


def test_task_service_patch_edit_requires_a_new_approval(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

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

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    diff = """--- a/src/slugger.py
+++ b/src/slugger.py
@@ -1 +1 @@
-old
+new
"""
    service.create_patch(task.task_id, diff)
    first_patch_approval = service.list_approvals(task.task_id)[-1]
    edited = service.submit_approval(
        task.task_id,
        first_patch_approval.approval_id,
        ApprovalDecision.EDIT,
        artifact_hash=first_patch_approval.artifact_hash,
        base_sha=first_patch_approval.base_sha,
        feedback="Please revise the implementation.",
    )
    assert edited.status == TaskStatus.DRAFTING_PATCH

    second_task, _summary = service.create_patch(
        task.task_id, diff.replace("new", "newer")
    )
    assert second_task.status == TaskStatus.AWAITING_PATCH_APPROVAL
    second_approval = service.list_approvals(task.task_id)[-1]
    assert second_approval.approval_id != first_patch_approval.approval_id
    with pytest.raises(StateConflictError, match="different artifact"):
        service.submit_approval(
            task.task_id,
            first_patch_approval.approval_id,
            ApprovalDecision.APPROVE,
            artifact_hash=second_approval.artifact_hash,
            base_sha=second_approval.base_sha,
        )


def test_task_service_records_red_green_test_report_and_enters_reviewing(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"

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

    class FakeRunner:
        def verify_red_green(
            self, baseline_root, patched_root, *, test_paths=("tests",)
        ):
            del baseline_root, patched_root, test_paths
            result = TestRunResult(
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
                command=result.command,
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
                baseline=result,
                patched=patched,
                message="baseline failed and patched copy passed",
            )

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
        test_runner=FakeRunner(),
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    diff = """--- a/src/slugger.py
+++ b/src/slugger.py
@@ -1 +1 @@
-old
+new
"""
    service.create_patch(task.task_id, diff)
    patch_approval = service.list_approvals(task.task_id)[-1]
    service.submit_approval(
        task.task_id,
        patch_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=patch_approval.artifact_hash,
        base_sha=patch_approval.base_sha,
    )

    reviewed = service.run_tests(task.task_id)

    assert reviewed.status == TaskStatus.REVIEWING
    report = next(
        item
        for item in service.list_artifacts(task.task_id)
        if item.kind == "test_report"
    )
    _record, content = service.read_artifact(task.task_id, report.artifact_id)
    assert '"status": "red_green"' in content


def test_task_service_marks_unconfigured_tests_failed(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        executor=lambda _task: TaskExecution(status=TaskStatus.TESTING),
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    failed = service.run_tests(task.task_id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error and "unsupported_environment" in failed.error


def test_task_service_runs_explorer_and_review_gate_after_tests(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    def executor(task: TaskRecord) -> TaskExecution:
        task_root = data_root / "tasks" / task.task_id
        snapshot = task_root / "snapshot"
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "tests").mkdir()
        (snapshot / "src" / "parser.py").write_text(
            "def parse(value):\n    return value\n", encoding="utf-8", newline="\n"
        )
        (snapshot / "tests" / "test_parser.py").write_text(
            "def test_parse():\n    assert True\n", encoding="utf-8", newline="\n"
        )
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Handle invalid input",
            base_sha="a" * 40,
            problem="Add a regression test for invalid input handling.",
            relevant_files=[
                FileReference(
                    path="src/parser.py",
                    start_line=1,
                    end_line=2,
                    reason="implementation",
                ),
                FileReference(
                    path="tests/test_parser.py",
                    start_line=1,
                    end_line=2,
                    reason="regression test",
                ),
            ],
            tests=["pytest -q"],
        )
        plan_files = save_plan(plan, task_root / "artifacts")
        payload = plan_files.json_path.read_bytes()
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=plan.base_sha,
            artifacts=(
                ArtifactRecord(
                    artifact_id="plan-artifact",
                    task_id=task.task_id,
                    kind="plan_json",
                    version=1,
                    path=str(plan_files.json_path),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    base_sha=plan.base_sha,
                    created_at=utc_now(),
                ),
            ),
        )

    class FakeRunner:
        def verify_red_green(
            self, baseline_root, patched_root, *, test_paths=("tests",)
        ):
            del baseline_root, patched_root, test_paths
            baseline = TestRunResult(
                status="failed",
                command=("docker", "run"),
                exit_code=1,
                stdout="red",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
                worktree_digest_before="base",
                worktree_digest_after="base",
            )
            patched = TestRunResult(
                status="passed",
                command=baseline.command,
                exit_code=0,
                stdout="green",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
                worktree_digest_before="patched",
                worktree_digest_after="patched",
            )
            return VerificationResult("red_green", baseline, patched, "red green")

    published_requests = []

    class FakePublisher:
        def publish(self, request):
            published_requests.append(request)
            return PublishResult(
                status="completed",
                branch=request.branch,
                commit_sha="f" * 40,
                pr_url="https://github.com/acme/project/pull/1",
                error=None,
                steps=(PublishStep("draft_pr", "completed", "created"),),
            )

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
        test_runner=FakeRunner(),
        publisher=FakePublisher(),
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    diff = (
        "--- a/src/parser.py\n+++ b/src/parser.py\n"
        "@@ -1,2 +1,2 @@\n def parse(value):\n"
        "-    return value\n+    return value.strip()\n"
        "--- a/tests/test_parser.py\n+++ b/tests/test_parser.py\n"
        "@@ -1,2 +1,2 @@\n def test_parse():\n"
        "-    assert True\n+    assert parse(' x ') == 'x'\n"
    )
    service.create_patch(task.task_id, diff)
    patch_approval = service.list_approvals(task.task_id)[-1]
    service.submit_approval(
        task.task_id,
        patch_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=patch_approval.artifact_hash,
        base_sha=patch_approval.base_sha,
    )

    assert service.run_tests(task.task_id).status == TaskStatus.REVIEWING
    reviewed = service.run_review(task.task_id)

    assert reviewed.status == TaskStatus.READY_TO_PUBLISH
    assert any(
        item.kind == "review_report" for item in service.list_artifacts(task.task_id)
    )
    waiting_publish, publish_request = service.prepare_publish(
        task.task_id,
        fork_owner="contributor",
        title="Fix parser",
        body="The approved patch passes isolated tests and review.",
        base_branch="main",
    )
    assert waiting_publish.status == TaskStatus.AWAITING_PUBLISH_APPROVAL
    assert publish_request.branch == f"deepcontrib/{task.task_id}"
    assert publish_request.issue_number == task.issue_number
    assert publish_request.body.endswith(f"Fixes #{task.issue_number}")
    publish_approval = service.list_approvals(task.task_id)[-1]
    completed = service.submit_approval(
        task.task_id,
        publish_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=publish_approval.artifact_hash,
        base_sha=publish_approval.base_sha,
    )
    assert completed.status == TaskStatus.COMPLETED
    assert published_requests[0].issue_number == task.issue_number
    assert published_requests[0].body.endswith(f"Fixes #{task.issue_number}")
    assert any(
        item.kind == "publish_result" for item in service.list_artifacts(task.task_id)
    )


def test_review_failure_after_two_repair_rounds_stops_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"

    def executor(task: TaskRecord) -> TaskExecution:
        task_root = data_root / "tasks" / task.task_id
        snapshot = task_root / "snapshot"
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "tests").mkdir()
        (snapshot / "src" / "value.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8", newline="\n"
        )
        (snapshot / "tests" / "test_value.py").write_text(
            "def test_value():\n    assert value() == 1\n",
            encoding="utf-8",
            newline="\n",
        )
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Fix value",
            base_sha="a" * 40,
            problem="Fix value",
            relevant_files=[
                FileReference(
                    path="src/value.py", start_line=1, end_line=2, reason="code"
                )
            ],
        )
        saved = save_plan(plan, task_root / "artifacts")
        payload = saved.json_path.read_bytes()
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=plan.base_sha,
            artifacts=(
                ArtifactRecord(
                    artifact_id="plan-artifact",
                    task_id=task.task_id,
                    kind="plan_json",
                    version=1,
                    path=str(saved.json_path),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    base_sha=plan.base_sha,
                    created_at=utc_now(),
                ),
            ),
        )

    class FakeRunner:
        def verify_red_green(
            self, baseline_root, patched_root, *, test_paths=("tests",)
        ):
            del baseline_root, patched_root, test_paths
            return VerificationResult(
                "red_green",
                TestRunResult(
                    "failed", ("docker", "run"), 1, "red", "", 0.1, False, "a", "a"
                ),
                TestRunResult(
                    "passed", ("docker", "run"), 0, "green", "", 0.1, False, "b", "b"
                ),
                "red green",
            )

    monkeypatch.setattr(
        "deepcontrib.service.Reviewer.review",
        lambda self, **kwargs: ReviewResult(
            "failed", "review still fails", (), kwargs["explorer"]
        ),
    )
    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
        test_runner=FakeRunner(),
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    diff = """--- a/src/value.py
+++ b/src/value.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""

    statuses = []
    for _ in range(3):
        service.create_patch(task.task_id, diff)
        patch_approval = service.list_approvals(task.task_id)[-1]
        service.submit_approval(
            task.task_id,
            patch_approval.approval_id,
            ApprovalDecision.APPROVE,
            artifact_hash=patch_approval.artifact_hash,
            base_sha=patch_approval.base_sha,
        )
        assert service.run_tests(task.task_id).status == TaskStatus.REVIEWING
        statuses.append(service.run_review(task.task_id).status)

    assert statuses == [
        TaskStatus.DRAFTING_PATCH,
        TaskStatus.DRAFTING_PATCH,
        TaskStatus.FAILED,
    ]
    assert "two repair rounds" in (service.get_task(task.task_id).error or "")


def test_task_service_applies_reproduction_patch_to_independent_baseline(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"

    def executor(task: TaskRecord) -> TaskExecution:
        task_root = data_root / "tasks" / task.task_id
        snapshot = task_root / "snapshot"
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "tests").mkdir()
        (snapshot / "src" / "bug.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8", newline="\n"
        )
        (snapshot / "tests" / "test_bug.py").write_text(
            "def test_value():\n    assert value() == 2\n",
            encoding="utf-8",
            newline="\n",
        )
        plan = ImplementationPlan(
            repo=task.repo,
            issue_number=task.issue_number,
            issue_title="Fix value",
            base_sha="a" * 40,
            problem="Fix value",
            relevant_files=[
                FileReference(
                    path="src/bug.py", start_line=1, end_line=2, reason="code"
                ),
                FileReference(
                    path="tests/test_bug.py", start_line=1, end_line=2, reason="test"
                ),
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

    class FakeRunner:
        def verify_red_green(
            self, baseline_root, patched_root, *, test_paths=("tests",)
        ):
            del test_paths
            assert baseline_root.name == "baseline-repro"
            assert (
                (baseline_root / "tests" / "test_bug.py")
                .read_text(encoding="utf-8")
                .endswith("assert value() == 1\n")
            )
            assert (
                (patched_root / "tests" / "test_bug.py")
                .read_text(encoding="utf-8")
                .endswith("assert value() == 1\n")
            )
            baseline = TestRunResult(
                status="failed",
                command=("docker", "run"),
                exit_code=1,
                stdout="red",
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
                stdout="green",
                stderr="",
                duration_seconds=0.1,
                timed_out=False,
                worktree_digest_before="b",
                worktree_digest_after="b",
            )
            return VerificationResult("red_green", baseline, patched, "red green")

    service = TaskService(
        InMemoryTaskStore(),
        executor=executor,
        artifact_root=data_root,
        data_root=data_root,
        test_runner=FakeRunner(),
    )
    task = service.create_task("https://github.com/acme/project", 1)
    service.start_task(task.task_id)
    plan_approval = service.list_approvals(task.task_id)[0]
    service.submit_approval(
        task.task_id,
        plan_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=plan_approval.artifact_hash,
        base_sha=plan_approval.base_sha,
    )
    primary = (
        "--- a/src/bug.py\n+++ b/src/bug.py\n@@ -1,2 +1,2 @@\n def value():\n"
        "-    return 1\n+    return 2\n"
    )
    reproduction = (
        "--- a/tests/test_bug.py\n+++ b/tests/test_bug.py\n"
        "@@ -1,2 +1,2 @@\n def test_value():\n"
        "-    assert value() == 2\n+    assert value() == 1\n"
    )
    service.create_patch(task.task_id, primary, reproduction_diff=reproduction)
    patch_approval = service.list_approvals(task.task_id)[-1]
    service.submit_approval(
        task.task_id,
        patch_approval.approval_id,
        ApprovalDecision.APPROVE,
        artifact_hash=patch_approval.artifact_hash,
        base_sha=patch_approval.base_sha,
    )

    assert service.run_tests(task.task_id).status == TaskStatus.REVIEWING


def test_task_service_marks_analysis_interrupted_and_resume_runs_checkpoint_stage(
    tmp_path: Path,
) -> None:
    calls = 0

    def executor(task: TaskRecord) -> TaskExecution:
        nonlocal calls
        calls += 1
        path = tmp_path / f"plan-{calls}.json"
        path.write_text('{"plan": true}\n', encoding="utf-8")
        return TaskExecution(status=TaskStatus.AWAITING_PLAN_APPROVAL)

    store = InMemoryTaskStore()
    service = TaskService(store, executor=executor)
    task = service.create_task("https://github.com/acme/project", 1)
    store.save_task(
        replace(
            task,
            status=TaskStatus.ANALYZING,
            error=None,
            updated_at=utc_now(),
        )
    )

    recovered = service.recover_interrupted_tasks()

    assert recovered == 1
    assert service.get_task(task.task_id).status == TaskStatus.INTERRUPTED
    resumed = service.resume_task(task.task_id)
    assert resumed.status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert calls == 1
