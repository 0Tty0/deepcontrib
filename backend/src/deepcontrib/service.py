"""Task lifecycle orchestration shared by the API and future UI clients."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from deepcontrib.analysis import prepare_analysis, run_model_analysis
from deepcontrib.checkpoint import ensure_postgres_schema, postgres_checkpointer
from deepcontrib.config import Settings
from deepcontrib.github import GitHubClient
from deepcontrib.memory import MemoryStore, relevant_memories
from deepcontrib.models import (
    TERMINAL_STATUSES,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    ArtifactRecord,
    EventRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from deepcontrib.patch_generation import PatchProposal
from deepcontrib.patches import (
    PatchError,
    PatchSummary,
    apply_patch,
    save_patch_artifact,
    summarize_patch,
    working_tree_digest,
)
from deepcontrib.plan import ImplementationPlan
from deepcontrib.publish import (
    Publisher,
    PublishError,
    PublishRequest,
    PublishResult,
    build_publish_request,
)
from deepcontrib.repository import RepositorySnapshot
from deepcontrib.review import Reviewer, ReviewResult
from deepcontrib.store import StoreError, TaskStore
from deepcontrib.subagents import RepoExplorer
from deepcontrib.test_runner import TestResult, TestRunner, VerificationResult


class ServiceError(RuntimeError):
    """A safe, user-facing task service error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "service_error",
        status_code: int = 400,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TaskNotFoundError(ServiceError):
    def __init__(self) -> None:
        super().__init__("task was not found", code="not_found", status_code=404)


class ArtifactNotFoundError(ServiceError):
    def __init__(self) -> None:
        super().__init__("artifact was not found", code="not_found", status_code=404)


class ApprovalNotFoundError(ServiceError):
    def __init__(self) -> None:
        super().__init__("approval was not found", code="not_found", status_code=404)


class TaskBusyError(ServiceError):
    def __init__(self) -> None:
        super().__init__(
            "another task is already running",
            code="task_busy",
            status_code=409,
        )


class StateConflictError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="state_conflict", status_code=409)


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class TaskExecution:
    """Result returned by one isolated stage executor."""

    status: TaskStatus
    base_sha: str | None = None
    artifacts: tuple[ArtifactRecord, ...] = ()


TaskExecutor = Callable[[TaskRecord], TaskExecution]
PatchGenerator = Callable[[TaskRecord, ImplementationPlan, Path], PatchProposal]


class TaskService:
    """Serialize local task runs and persist every externally visible change."""

    def __init__(
        self,
        store: TaskStore,
        *,
        executor: TaskExecutor,
        artifact_root: Path | None = None,
        data_root: Path | None = None,
        test_runner: TestRunner | None = None,
        memory_store: MemoryStore | None = None,
        publisher: Publisher | None = None,
        patch_generator: PatchGenerator | None = None,
        max_task_seconds: int = 1_800,
        max_model_calls: int = 40,
    ) -> None:
        self.store = store
        self.executor = executor
        self.artifact_root = artifact_root.resolve() if artifact_root else None
        self.data_root = data_root.resolve() if data_root else self.artifact_root
        self.test_runner = test_runner
        self.memory_store = memory_store
        self.publisher = publisher
        self.patch_generator = patch_generator
        if max_task_seconds <= 0 or max_model_calls <= 0:
            raise ValueError("task and model call limits must be positive")
        self.max_task_seconds = max_task_seconds
        self.max_model_calls = max_model_calls
        # The local product deliberately has one active task and no queue service.
        import threading

        self._run_lock = threading.RLock()
        self._active_task_id: str | None = None
        self._cancel_requested: set[str] = set()

    def create_task(
        self, repo_url: str, issue_number: str | int, *, thread_id: str | None = None
    ) -> TaskRecord:
        from deepcontrib.repository import parse_issue_number, parse_repository_url

        try:
            repo = parse_repository_url(repo_url)
            issue = parse_issue_number(issue_number)
        except ValueError as exc:
            raise ServiceError(str(exc), code="invalid_task", status_code=422) from exc
        task_id = uuid4().hex
        chosen_thread_id = thread_id or f"task-{task_id}"
        if not _TASK_ID_PATTERN.fullmatch(chosen_thread_id):
            raise ServiceError(
                "thread_id must contain only letters, numbers, '.', '_' or '-'",
                code="invalid_thread_id",
                status_code=422,
            )
        now = utc_now()
        task = TaskRecord(
            task_id=task_id,
            thread_id=chosen_thread_id,
            repo=repo.full_name,
            issue_number=issue,
            status=TaskStatus.QUEUED,
            base_sha=None,
            current_artifact_id=None,
            error=None,
            created_at=now,
            updated_at=now,
        )
        created = self.store.create_task(task)
        self.store.append_event(
            created.task_id,
            "task.created",
            {"status": created.status.value, "repo": created.repo, "issue": issue},
        )
        return created

    def get_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError()
        return task

    def list_events(self, task_id: str, *, after_id: int = 0) -> list[EventRecord]:
        self.get_task(task_id)
        return self.store.list_events(task_id, after_id=after_id)

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]:
        self.get_task(task_id)
        return self.store.list_approvals(task_id)

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        self.get_task(task_id)
        return self.store.list_artifacts(task_id)

    def export_task(self, task_id: str) -> bytes:
        """Export a bounded, self-contained task evidence archive."""
        task = self.get_task(task_id)
        artifacts = self.store.list_artifacts(task_id)
        approvals = self.store.list_approvals(task_id)
        events = self.store.list_events(task_id)
        buffer = io.BytesIO()
        total_bytes = 0
        try:
            with zipfile.ZipFile(
                buffer, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(
                    "task.json",
                    json.dumps(
                        task.as_dict(), ensure_ascii=False, indent=2, default=str
                    ),
                )
                archive.writestr(
                    "approvals.json",
                    json.dumps(
                        [item.as_dict() for item in approvals],
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                )
                archive.writestr(
                    "events.json",
                    json.dumps(
                        [item.as_dict() for item in events],
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                )
                for item in artifacts:
                    _record, content = self.read_artifact(task_id, item.artifact_id)
                    suffix = (
                        ".diff"
                        if item.kind in {"patch", "reproduction_patch"}
                        else ".json"
                    )
                    safe_name = (
                        f"artifacts/{item.kind}-v{item.version}-"
                        f"{item.artifact_id[:12]}"
                        f"{suffix}"
                    )
                    total_bytes += len(content.encode("utf-8"))
                    if total_bytes > 50 * 1024 * 1024:
                        raise ServiceError(
                            "task export exceeds the 50 MB limit",
                            code="export_too_large",
                            status_code=413,
                        )
                    archive.writestr(safe_name, content)
                if self.data_root is not None:
                    task_root = (self.data_root / "tasks" / task_id).resolve()
                    if task_root.is_dir():
                        for path in sorted(task_root.rglob("*")):
                            if not path.is_file() or path.is_symlink():
                                continue
                            relative = path.relative_to(task_root).as_posix()
                            if relative.startswith("artifacts/"):
                                continue
                            payload = path.read_bytes()
                            total_bytes += len(payload)
                            if total_bytes > 50 * 1024 * 1024:
                                raise ServiceError(
                                    "task export exceeds the 50 MB limit",
                                    code="export_too_large",
                                    status_code=413,
                                )
                            archive.writestr(f"workspace/{relative}", payload)
        except OSError as exc:
            raise ServiceError("could not create task export") from exc
        return buffer.getvalue()

    def start_task(self, task_id: str) -> TaskRecord:
        """Run one stage; terminal and approval states are idempotent."""
        if not self._run_lock.acquire(blocking=False):
            raise TaskBusyError()
        try:
            if self._active_task_id not in (None, task_id):
                raise TaskBusyError()
            self._active_task_id = task_id
            task = self.get_task(task_id)
            if task.status in TERMINAL_STATUSES:
                return task
            if task.status != TaskStatus.QUEUED:
                if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                    return task
                raise StateConflictError(
                    f"task cannot start from status {task.status.value}"
                )
            task = self._save_status(task, TaskStatus.ANALYZING)
            self._cancel_requested.discard(task_id)
            started = time.perf_counter()
            try:
                execution = self.executor(task)
                duration_seconds = round(time.perf_counter() - started, 3)
                if task_id in self._cancel_requested:
                    return self._record_cancelled(task, "analysis")
                if duration_seconds > self.max_task_seconds:
                    failed = self._save_status(
                        task,
                        TaskStatus.FAILED,
                        error="task execution exceeded the configured time limit",
                    )
                    self.store.append_event(
                        task_id,
                        "task.limit_exceeded",
                        {"limit_seconds": self.max_task_seconds},
                    )
                    return failed
                return self._record_execution(
                    task, execution, duration_seconds=duration_seconds
                )
            except Exception as exc:
                if task_id in self._cancel_requested:
                    return self._record_cancelled(task, "analysis")
                message = str(exc).strip() or "task execution failed"
                failed = self._save_status(
                    task,
                    TaskStatus.FAILED,
                    error=message[:500],
                )
                self.store.append_event(
                    task_id,
                    "task.failed",
                    {"error": failed.error or "task execution failed"},
                )
                return failed
        finally:
            self._cancel_requested.discard(task_id)
            self._active_task_id = None
            self._run_lock.release()

    def resume_task(self, task_id: str) -> TaskRecord:
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status == TaskStatus.QUEUED:
                return self.start_task(task_id)
            if task.status in TERMINAL_STATUSES:
                return task
            if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                approvals = self.store.list_approvals(task_id)
                latest = approvals[-1] if approvals else None
                if latest and latest.decision == ApprovalDecision.APPROVE:
                    return self._save_status(task, TaskStatus.DRAFTING_PATCH)
                if latest and latest.decision in {
                    ApprovalDecision.EDIT,
                    ApprovalDecision.REJECT,
                }:
                    queued = self._save_status(task, TaskStatus.QUEUED)
                    return self.start_task(queued.task_id)
                raise StateConflictError(
                    "plan approval is required before the task can resume"
                )
            if task.status == TaskStatus.AWAITING_PATCH_APPROVAL:
                approvals = self.store.list_approvals(task_id)
                latest = approvals[-1] if approvals else None
                if (
                    latest
                    and latest.kind == ApprovalKind.PATCH
                    and latest.decision == ApprovalDecision.APPROVE
                ):
                    return self._apply_approved_patch(task, latest)
                if (
                    latest
                    and latest.kind == ApprovalKind.PATCH
                    and latest.decision == ApprovalDecision.EDIT
                ):
                    return self._save_status(task, TaskStatus.DRAFTING_PATCH)
                raise StateConflictError(
                    "patch approval is required before the task can resume"
                )
            if task.status == TaskStatus.AWAITING_PUBLISH_APPROVAL:
                approvals = self.store.list_approvals(task_id)
                latest = approvals[-1] if approvals else None
                if (
                    latest
                    and latest.kind == ApprovalKind.PUBLISH
                    and latest.decision == ApprovalDecision.APPROVE
                ):
                    return self._publish_approved(task, latest)
                raise StateConflictError(
                    "publish approval is required before the task can resume"
                )
            if task.status == TaskStatus.DRAFTING_PATCH:
                if self.patch_generator is not None:
                    return self.generate_patch(task_id)
                raise StateConflictError("submit a Patch before resuming the task")
            if task.status == TaskStatus.INTERRUPTED:
                interrupted_stage = (
                    task.error.removeprefix("process restarted during ")
                    if task.error
                    else ""
                )
                if interrupted_stage == TaskStatus.ANALYZING.value:
                    queued = self._save_status(task, TaskStatus.QUEUED, error=None)
                    return self.start_task(queued.task_id)
                if interrupted_stage == TaskStatus.TESTING.value:
                    testing = self._save_status(task, TaskStatus.TESTING, error=None)
                    return self.run_tests(testing.task_id)
                raise StateConflictError(
                    "publishing was interrupted; inspect remote state before retrying"
                )
            raise StateConflictError(
                f"task cannot resume from status {task.status.value}"
            )

    def recover_interrupted_tasks(self) -> int:
        """Mark in-flight work recoverable after a process restart."""
        recovered = self.store.recover_interrupted_tasks()
        for task in recovered:
            self.store.append_event(
                task.task_id,
                "task.interrupted",
                {"error": task.error, "resume_required": True},
            )
        return len(recovered)

    def submit_approval(
        self,
        task_id: str,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        artifact_hash: str,
        base_sha: str | None,
        feedback: str | None = None,
    ) -> TaskRecord:
        with self._run_lock:
            task = self.get_task(task_id)
            approval = self.store.get_approval(approval_id)
            if approval is None or approval.task_id != task_id:
                raise ApprovalNotFoundError()
            if artifact_hash != approval.artifact_hash:
                raise StateConflictError("approval is bound to a different artifact")
            if approval.base_sha != base_sha:
                raise StateConflictError("approval is bound to a different base SHA")
            if feedback is not None and len(feedback) > 2_000:
                raise ServiceError(
                    "approval feedback is too long",
                    code="invalid_feedback",
                    status_code=422,
                )
            if approval.decision is not None:
                if approval.decision != decision:
                    raise StateConflictError(
                        "approval already has a different decision"
                    )
                if decision == ApprovalDecision.APPROVE:
                    if (
                        approval.kind == ApprovalKind.PLAN
                        and task.status == TaskStatus.AWAITING_PLAN_APPROVAL
                    ):
                        return self._save_status(task, TaskStatus.DRAFTING_PATCH)
                    if (
                        approval.kind == ApprovalKind.PATCH
                        and task.status == TaskStatus.AWAITING_PATCH_APPROVAL
                    ):
                        return self._apply_approved_patch(task, approval)
                    if (
                        approval.kind == ApprovalKind.PUBLISH
                        and task.status == TaskStatus.AWAITING_PUBLISH_APPROVAL
                    ):
                        return self._save_status(task, TaskStatus.PUBLISHING)
                return task
            expected_status = {
                ApprovalKind.PLAN: TaskStatus.AWAITING_PLAN_APPROVAL,
                ApprovalKind.PATCH: TaskStatus.AWAITING_PATCH_APPROVAL,
                ApprovalKind.PUBLISH: TaskStatus.AWAITING_PUBLISH_APPROVAL,
            }[approval.kind]
            if decision == ApprovalDecision.APPROVE and task.status != expected_status:
                raise StateConflictError(
                    f"{approval.kind.value} approval cannot be consumed from "
                    f"status {task.status.value}"
                )

            consumed_at = utc_now() if decision == ApprovalDecision.APPROVE else None
            saved_approval = self.store.save_approval(
                replace(
                    approval,
                    decision=decision,
                    feedback=feedback.strip() if feedback else None,
                    consumed_at=consumed_at,
                )
            )
            self.store.append_event(
                task_id,
                f"approval.{decision.value}",
                {
                    "approval_id": approval_id,
                    "kind": saved_approval.kind.value,
                    "artifact_hash": saved_approval.artifact_hash,
                },
            )
            if decision == ApprovalDecision.APPROVE:
                if approval.kind == ApprovalKind.PLAN:
                    return self._save_status(task, TaskStatus.DRAFTING_PATCH)
                if approval.kind == ApprovalKind.PATCH:
                    return self._apply_approved_patch(task, saved_approval)
                return self._publish_approved(task, saved_approval)
            if (
                decision == ApprovalDecision.EDIT
                and approval.kind == ApprovalKind.PATCH
            ):
                return self._save_status(task, TaskStatus.DRAFTING_PATCH)
            return task

    def _load_plan_for_task(self, task_id: str, task: TaskRecord) -> ImplementationPlan:
        if self.data_root is None or task.current_artifact_id is None:
            raise ServiceError(
                "task workspace is unavailable",
                code="workspace_unavailable",
                status_code=409,
            )
        plan_artifact = next(
            (
                artifact
                for artifact in reversed(self.store.list_artifacts(task_id))
                if artifact.kind == "plan_json"
            ),
            None,
        )
        if plan_artifact is None:
            raise StateConflictError("current task artifact is not a Plan")
        _artifact, plan_text = self.read_artifact(task_id, plan_artifact.artifact_id)
        try:
            plan = ImplementationPlan.model_validate(json.loads(plan_text))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ServiceError(
                "current Plan artifact is invalid",
                code="plan_invalid",
                status_code=409,
            ) from exc
        if plan.base_sha != task.base_sha:
            raise StateConflictError("Plan base SHA does not match the task")
        return plan

    def generate_patch(self, task_id: str) -> TaskRecord:
        """Generate, validate, and queue a model patch for human approval."""
        if self.patch_generator is None:
            raise StateConflictError("automatic Patch generation is not configured")
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status == TaskStatus.AWAITING_PATCH_APPROVAL:
                return task
            if task.status != TaskStatus.DRAFTING_PATCH:
                raise StateConflictError(
                    f"patch generation requires drafting_patch, got {task.status.value}"
                )
            try:
                plan = self._load_plan_for_task(task_id, task)
                data_root = self.data_root
                if data_root is None:
                    raise ServiceError(
                        "task workspace is unavailable",
                        code="workspace_unavailable",
                        status_code=409,
                    )
                snapshot_root = data_root / "tasks" / task_id / "snapshot"
                proposal = self.patch_generator(task, plan, snapshot_root)
                if not proposal.diff:
                    raise ServiceError(
                        "automatic Patch generation returned no diff",
                        code="invalid_patch",
                        status_code=422,
                    )
                updated, summary = self.create_patch(
                    task_id,
                    proposal.diff,
                    reproduction_diff=proposal.reproduction_diff,
                )
                self.store.append_event(
                    task_id,
                    "patch.generated",
                    {
                        "artifact_id": updated.current_artifact_id,
                        "changed_paths": list(summary.changed_paths),
                        "additions": summary.additions,
                        "deletions": summary.deletions,
                    },
                )
                return updated
            except Exception as exc:
                detail = str(exc).strip()[:500] or "automatic Patch generation failed"
                updated = self._save_status(
                    task,
                    TaskStatus.DRAFTING_PATCH,
                    error=detail,
                )
                self.store.append_event(
                    task_id,
                    "patch.generation_failed",
                    {"error": detail},
                )
                return updated

    def create_patch(
        self,
        task_id: str,
        diff: str,
        *,
        reproduction_diff: str | None = None,
    ) -> tuple[TaskRecord, PatchSummary]:
        """Validate and persist a patch after the Plan approval gate."""
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status != TaskStatus.DRAFTING_PATCH:
                raise StateConflictError(
                    f"patch generation requires drafting_patch, got {task.status.value}"
                )
            if self.data_root is None:
                raise ServiceError(
                    "task workspace is unavailable",
                    code="workspace_unavailable",
                    status_code=409,
                )
            plan = self._load_plan_for_task(task_id, task)
            snapshot_root = self.data_root / "tasks" / task_id / "snapshot"
            allowed_paths = [item.path for item in plan.relevant_files] or None
            try:
                patch_version = self._next_patch_version(task_id)
                patch_artifact = save_patch_artifact(
                    task_id=task_id,
                    plan=plan,
                    diff=diff,
                    directory=self.data_root / "tasks" / task_id / "artifacts",
                    version=patch_version,
                    base_root=snapshot_root,
                    allowed_paths=allowed_paths,
                )
                reproduction_artifact = (
                    save_patch_artifact(
                        task_id=task_id,
                        plan=plan,
                        diff=reproduction_diff,
                        directory=self.data_root / "tasks" / task_id / "artifacts",
                        version=patch_version,
                        base_root=snapshot_root,
                        allowed_paths=allowed_paths,
                        filename_prefix="repro",
                    )
                    if reproduction_diff
                    else None
                )
            except PatchError as exc:
                raise ServiceError(
                    str(exc), code="invalid_patch", status_code=422
                ) from exc
            record = ArtifactRecord(
                artifact_id=patch_artifact.patch_id,
                task_id=task_id,
                kind="patch",
                version=patch_artifact.version,
                path=str(patch_artifact.patch_path.resolve()),
                sha256=patch_artifact.sha256,
                base_sha=task.base_sha,
                created_at=utc_now(),
            )
            self.store.add_artifact(record)
            if reproduction_artifact is not None:
                self.store.add_artifact(
                    ArtifactRecord(
                        artifact_id=reproduction_artifact.patch_id,
                        task_id=task_id,
                        kind="reproduction_patch",
                        version=reproduction_artifact.version,
                        path=str(reproduction_artifact.patch_path.resolve()),
                        sha256=reproduction_artifact.sha256,
                        base_sha=task.base_sha,
                        created_at=utc_now(),
                    )
                )
            updated = self._save_status(
                task,
                TaskStatus.AWAITING_PATCH_APPROVAL,
                current_artifact_id=record.artifact_id,
            )
            approval = ApprovalRecord(
                approval_id=uuid4().hex,
                task_id=task_id,
                kind=ApprovalKind.PATCH,
                artifact_hash=record.sha256,
                base_sha=record.base_sha,
                decision=None,
                feedback=None,
                consumed_at=None,
                created_at=utc_now(),
            )
            self.store.add_approval(approval)
            self.store.append_event(
                task_id,
                "approval.required",
                {
                    "approval_id": approval.approval_id,
                    "kind": approval.kind.value,
                    "artifact_id": record.artifact_id,
                    "reproduction_artifact_id": (
                        reproduction_artifact.patch_id
                        if reproduction_artifact is not None
                        else None
                    ),
                },
            )
            return updated, patch_artifact.summary

    def cancel_task(self, task_id: str) -> TaskRecord:
        if self._active_task_id == task_id:
            self._cancel_requested.add(task_id)
            cancel = getattr(self.test_runner, "cancel", None)
            if callable(cancel):
                cancel()
            self.store.append_event(task_id, "task.cancel_requested", {})
            return self.get_task(task_id)
        if self._active_task_id is not None:
            raise TaskBusyError()
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status in TERMINAL_STATUSES:
                return task
            cancelled = self._save_status(task, TaskStatus.CANCELLED)
            self.store.append_event(task_id, "task.cancelled", {})
            return cancelled

    def run_tests(self, task_id: str) -> TaskRecord:
        """Verify the approved working copy in the configured sandbox."""
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status != TaskStatus.TESTING:
                raise StateConflictError(
                    f"tests can only run from status {task.status.value}"
                )
            if self.test_runner is None or self.data_root is None:
                return self._record_test_unavailable(task)
            task_root = self.data_root / "tasks" / task.task_id
            baseline_root = task_root / "snapshot"
            patched_root = task_root / "working"
            try:
                reproduction_artifact = next(
                    (
                        item
                        for item in reversed(self.store.list_artifacts(task_id))
                        if item.kind == "reproduction_patch"
                    ),
                    None,
                )
                if reproduction_artifact is not None:
                    baseline_root = task_root / "baseline-repro"
                    if baseline_root.is_symlink():
                        raise PatchError("baseline reproduction directory is not valid")
                    if baseline_root.exists():
                        shutil.rmtree(baseline_root)
                    shutil.copytree(task_root / "snapshot", baseline_root)
                    _repro_record, reproduction_diff = self.read_artifact(
                        task_id, reproduction_artifact.artifact_id
                    )
                    apply_patch(
                        reproduction_diff,
                        target_root=baseline_root,
                        base_sha=task.base_sha or "",
                    )
                if task_id in self._cancel_requested:
                    return self._record_cancelled(task, "testing")
                reset_cancel = getattr(self.test_runner, "reset_cancel", None)
                if callable(reset_cancel):
                    reset_cancel()
                self._active_task_id = task_id
                self._cancel_requested.discard(task_id)
                try:
                    verification = self.test_runner.verify_red_green(
                        baseline_root, patched_root
                    )
                finally:
                    self._active_task_id = None
                if task_id in self._cancel_requested:
                    return self._record_cancelled(task, "testing")
            except (OSError, PatchError, ServiceError) as exc:
                if task_id in self._cancel_requested:
                    return self._record_cancelled(task, "testing")
                return self._record_test_failure(task, str(exc))
            report = self._save_test_report(task, verification)
            if verification.status == "red_green":
                updated = self._save_status(
                    task,
                    TaskStatus.REVIEWING,
                    current_artifact_id=report.artifact_id,
                    error=None,
                )
            else:
                updated = self._save_status(
                    task,
                    TaskStatus.FAILED,
                    current_artifact_id=report.artifact_id,
                    error=verification.message[:500],
                )
            self.store.append_event(
                task.task_id,
                "test.completed",
                {
                    "artifact_id": report.artifact_id,
                    "status": verification.status,
                    "reproduction_artifact_id": (
                        reproduction_artifact.artifact_id
                        if reproduction_artifact is not None
                        else None
                    ),
                },
            )
            return updated

    def run_review(self, task_id: str) -> TaskRecord:
        """Run the read-only Explorer and Reviewer gate over test evidence."""
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status != TaskStatus.REVIEWING:
                raise StateConflictError(
                    f"review can only run from status {task.status.value}"
                )
            if self.data_root is None:
                raise ServiceError(
                    "task workspace is unavailable",
                    code="workspace_unavailable",
                    status_code=409,
                )
            artifacts = self.store.list_artifacts(task_id)
            plan_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "plan_json"),
                None,
            )
            patch_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "patch"),
                None,
            )
            report_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "test_report"),
                None,
            )
            if (
                plan_artifact is None
                or patch_artifact is None
                or report_artifact is None
            ):
                raise StateConflictError(
                    "review requires a Plan, Patch, and test report artifact"
                )
            _plan_record, plan_text = self.read_artifact(
                task_id, plan_artifact.artifact_id
            )
            _patch_record, diff = self.read_artifact(
                task_id, patch_artifact.artifact_id
            )
            _report_record, report_text = self.read_artifact(
                task_id, report_artifact.artifact_id
            )
            try:
                plan = ImplementationPlan.model_validate(json.loads(plan_text))
                verification = VerificationResult.from_dict(json.loads(report_text))
                patch_summary = summarize_patch(diff)
                snapshot = RepositorySnapshot(
                    self.data_root / "tasks" / task_id / "snapshot",
                    task.base_sha or plan.base_sha,
                )
            except (ValueError, json.JSONDecodeError, TypeError, PatchError) as exc:
                raise ServiceError(
                    "review evidence is invalid",
                    code="review_evidence_invalid",
                    status_code=409,
                ) from exc
            explorer_agent = RepoExplorer()
            explorer_started = time.perf_counter()
            self.store.append_event(
                task_id,
                "subagent.started",
                {
                    "agent": "repo-explorer",
                    "status": "running",
                    "tools": list(explorer_agent.tool_names),
                },
            )
            explorer = explorer_agent.explore(
                snapshot,
                relevant_paths=[item.path for item in plan.relevant_files],
            )
            self.store.append_event(
                task_id,
                "subagent.completed",
                {
                    "agent": "repo-explorer",
                    "status": "completed",
                    "duration_seconds": round(
                        time.perf_counter() - explorer_started, 3
                    ),
                    "summary": (
                        f"read {len(explorer.files)} file(s), "
                        f"found {len(explorer.symbols)} symbol(s)"
                    ),
                    "uncertainty_count": len(explorer.uncertainties),
                },
            )
            reviewer_agent = Reviewer()
            reviewer_started = time.perf_counter()
            self.store.append_event(
                task_id,
                "subagent.started",
                {
                    "agent": "reviewer",
                    "status": "running",
                    "tools": list(reviewer_agent.tool_names),
                },
            )
            result = reviewer_agent.review(
                issue_text=plan.problem,
                plan=plan,
                patch_summary=patch_summary,
                verification=verification,
                explorer=explorer,
                diff=diff,
            )
            self.store.append_event(
                task_id,
                "subagent.completed",
                {
                    "agent": "reviewer",
                    "status": "completed",
                    "duration_seconds": round(
                        time.perf_counter() - reviewer_started, 3
                    ),
                    "summary": result.summary,
                    "issue_count": len(result.issues),
                },
            )
            review_artifact = self._save_review_report(task, result)
            if result.status == "passed":
                updated = self._save_status(
                    task,
                    TaskStatus.READY_TO_PUBLISH,
                    current_artifact_id=review_artifact.artifact_id,
                    error=None,
                )
            else:
                patch_round = max(
                    (item.version for item in artifacts if item.kind == "patch"),
                    default=0,
                )
                exhausted = patch_round >= 3
                updated = self._save_status(
                    task,
                    TaskStatus.FAILED if exhausted else TaskStatus.DRAFTING_PATCH,
                    current_artifact_id=review_artifact.artifact_id,
                    error=(
                        "review failed after two repair rounds"
                        if exhausted
                        else result.summary[:500]
                    ),
                )
            self.store.append_event(
                task_id,
                "review.completed",
                {
                    "artifact_id": review_artifact.artifact_id,
                    "status": result.status,
                    "issue_count": len(result.issues),
                    "summary": result.summary,
                    "repair_rounds_exhausted": (
                        result.status == "failed"
                        and updated.status == TaskStatus.FAILED
                    ),
                },
            )
            return updated

    def prepare_publish(
        self,
        task_id: str,
        *,
        fork_owner: str,
        title: str,
        body: str,
        base_branch: str,
    ) -> tuple[TaskRecord, PublishRequest]:
        """Create an immutable publish card and its separate approval gate."""
        with self._run_lock:
            task = self.get_task(task_id)
            if task.status != TaskStatus.READY_TO_PUBLISH:
                raise StateConflictError(
                    "publish preparation requires ready_to_publish, got "
                    f"{task.status.value}"
                )
            if self.data_root is None:
                raise ServiceError(
                    "task workspace is unavailable",
                    code="workspace_unavailable",
                    status_code=409,
                )
            artifacts = self.store.list_artifacts(task_id)
            plan_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "plan_json"),
                None,
            )
            patch_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "patch"),
                None,
            )
            test_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "test_report"),
                None,
            )
            review_artifact = next(
                (item for item in reversed(artifacts) if item.kind == "review_report"),
                None,
            )
            if not all((plan_artifact, patch_artifact, test_artifact, review_artifact)):
                raise StateConflictError(
                    "publish requires Plan, Patch, test, and Review artifacts"
                )
            assert (
                plan_artifact is not None
                and patch_artifact is not None
                and test_artifact is not None
                and review_artifact is not None
            )
            _patch_record, diff = self.read_artifact(
                task_id, patch_artifact.artifact_id
            )
            working_root = self.data_root / "tasks" / task_id / "working"
            try:
                digest = working_tree_digest(working_root)
            except PatchError as exc:
                raise StateConflictError(
                    "working tree is no longer a valid approved copy"
                ) from exc
            try:
                request = build_publish_request(
                    task_id=task_id,
                    upstream_repo=task.repo,
                    issue_number=task.issue_number,
                    fork_owner=fork_owner,
                    base_sha=task.base_sha or patch_artifact.base_sha or "",
                    working_root=working_root,
                    working_tree_digest=digest,
                    patch_sha256=patch_artifact.sha256,
                    test_report_sha256=test_artifact.sha256,
                    review_report_sha256=review_artifact.sha256,
                    title=title,
                    body=body,
                    base_branch=base_branch,
                    patch_diff=diff,
                )
            except PublishError as exc:
                raise ServiceError(
                    str(exc), code="invalid_publish", status_code=422
                ) from exc
            directory = self.data_root / "tasks" / task_id / "artifacts"
            directory.mkdir(parents=True, exist_ok=True)
            version = (
                max(
                    (item.version for item in artifacts if item.kind == "publish_card"),
                    default=0,
                )
                + 1
            )
            payload = (
                json.dumps(
                    request.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n"
            )
            target = directory / f"publish-v{version}.json"
            try:
                target.write_text(payload, encoding="utf-8", newline="\n")
            except OSError as exc:
                raise ServiceError("could not save publish card") from exc
            card = ArtifactRecord(
                artifact_id=uuid4().hex,
                task_id=task_id,
                kind="publish_card",
                version=version,
                path=str(target.resolve()),
                sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                base_sha=task.base_sha,
                created_at=utc_now(),
            )
            self.store.add_artifact(card)
            updated = self._save_status(
                task,
                TaskStatus.AWAITING_PUBLISH_APPROVAL,
                current_artifact_id=card.artifact_id,
                error=None,
            )
            approval = ApprovalRecord(
                approval_id=uuid4().hex,
                task_id=task_id,
                kind=ApprovalKind.PUBLISH,
                artifact_hash=card.sha256,
                base_sha=task.base_sha,
                decision=None,
                feedback=None,
                consumed_at=None,
                created_at=utc_now(),
            )
            self.store.add_approval(approval)
            self.store.append_event(
                task_id,
                "approval.required",
                {
                    "approval_id": approval.approval_id,
                    "kind": approval.kind.value,
                    "artifact_id": card.artifact_id,
                    "side_effects": list(request.side_effects),
                },
            )
            return updated, request

    def _publish_approved(
        self, task: TaskRecord, approval: ApprovalRecord
    ) -> TaskRecord:
        card = next(
            (
                item
                for item in self.store.list_artifacts(task.task_id)
                if item.kind == "publish_card" and item.sha256 == approval.artifact_hash
            ),
            None,
        )
        if card is None:
            raise StateConflictError("approved publish card is unavailable")
        if self.data_root is None:
            raise StateConflictError("task workspace is unavailable")
        try:
            _record, card_text = self.read_artifact(task.task_id, card.artifact_id)
            payload = json.loads(card_text)
            patch_artifact = next(
                item
                for item in reversed(self.store.list_artifacts(task.task_id))
                if item.kind == "patch"
            )
            _patch_record, diff = self.read_artifact(
                task.task_id, patch_artifact.artifact_id
            )
            request = build_publish_request(
                task_id=task.task_id,
                upstream_repo=str(payload["upstream_repo"]),
                issue_number=int(payload["issue_number"]),
                fork_owner=str(payload["fork_owner"]),
                base_sha=str(payload["base_sha"]),
                working_root=self.data_root / "tasks" / task.task_id / "working",
                working_tree_digest=str(payload["working_tree_digest"]),
                patch_sha256=str(payload["patch_sha256"]),
                test_report_sha256=str(payload["test_report_sha256"]),
                review_report_sha256=str(payload["review_report_sha256"]),
                title=str(payload["title"]),
                body=str(payload["body"]),
                base_branch=str(payload["base_branch"]),
                patch_diff=diff,
            )
        except (KeyError, StopIteration, ValueError, PublishError, ServiceError) as exc:
            return self._record_publish_failure(task, str(exc))
        publishing = self._save_status(task, TaskStatus.PUBLISHING, error=None)
        if self.publisher is None:
            return self._record_publish_failure(
                publishing,
                "publish environment is not configured; result is "
                "unsupported_environment",
            )
        try:
            result = self.publisher.publish(request)
        except Exception as exc:
            return self._record_publish_failure(publishing, str(exc))
        artifact = self._save_publish_result(publishing, result)
        if result.status == "completed":
            completed = self._save_status(
                publishing,
                TaskStatus.COMPLETED,
                current_artifact_id=artifact.artifact_id,
                error=None,
            )
            self.store.append_event(
                task.task_id,
                "publish.completed",
                {
                    "artifact_id": artifact.artifact_id,
                    "branch": result.branch,
                    "commit_sha": result.commit_sha,
                    "pr_url": result.pr_url,
                },
            )
            return completed
        return self._record_publish_failure(
            publishing,
            result.error or "publish did not complete",
            artifact=artifact,
        )

    def _record_publish_failure(
        self,
        task: TaskRecord,
        message: str,
        *,
        artifact: ArtifactRecord | None = None,
    ) -> TaskRecord:
        failed = self._save_status(
            task,
            TaskStatus.FAILED,
            current_artifact_id=artifact.artifact_id
            if artifact
            else task.current_artifact_id,
            error=message[:500] or "publish failed",
        )
        self.store.append_event(
            task.task_id,
            "publish.failed",
            {
                "error": failed.error or "publish failed",
                "artifact_id": artifact.artifact_id if artifact else None,
            },
        )
        return failed

    def _save_publish_result(
        self, task: TaskRecord, result: PublishResult
    ) -> ArtifactRecord:
        if self.data_root is None:
            raise ServiceError("task workspace is unavailable")
        directory = self.data_root / "tasks" / task.task_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        version = (
            max(
                (
                    item.version
                    for item in self.store.list_artifacts(task.task_id)
                    if item.kind == "publish_result"
                ),
                default=0,
            )
            + 1
        )
        payload = (
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        target = directory / f"publish-result-v{version}.json"
        target.write_text(payload, encoding="utf-8", newline="\n")
        record = ArtifactRecord(
            artifact_id=uuid4().hex,
            task_id=task.task_id,
            kind="publish_result",
            version=version,
            path=str(target.resolve()),
            sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            base_sha=task.base_sha,
            created_at=utc_now(),
        )
        self.store.add_artifact(record)
        return record

    def _record_test_unavailable(self, task: TaskRecord) -> TaskRecord:
        message = "test sandbox is not configured; result is unsupported_environment"
        report: ArtifactRecord | None = None
        if self.data_root is not None:
            report = self._save_test_report(
                task,
                VerificationResult(
                    status="unsupported_environment",
                    baseline=TestResult(
                        status="unsupported_environment",
                        command=(),
                        exit_code=None,
                        stdout="",
                        stderr=message,
                        duration_seconds=0,
                        timed_out=False,
                        worktree_digest_before=None,
                        worktree_digest_after=None,
                    ),
                    patched=None,
                    message=message,
                ),
            )
        failed = self._save_status(
            task,
            TaskStatus.FAILED,
            current_artifact_id=report.artifact_id if report else None,
            error=message,
        )
        self.store.append_event(
            task.task_id,
            "test.unsupported_environment",
            {
                "message": message,
                "artifact_id": report.artifact_id if report else None,
            },
        )
        return failed

    def _record_test_failure(self, task: TaskRecord, message: str) -> TaskRecord:
        detail = message.strip()[:500] or "test runner failed"
        failed = self._save_status(task, TaskStatus.FAILED, error=detail)
        self.store.append_event(
            task.task_id,
            "test.failed",
            {"error": detail},
        )
        return failed

    def _record_cancelled(self, task: TaskRecord, stage: str) -> TaskRecord:
        cancelled = self._save_status(
            task,
            TaskStatus.CANCELLED,
            error="cancelled by user",
        )
        self.store.append_event(
            task.task_id,
            "task.cancelled",
            {"stage": stage, "requested": True},
        )
        self._cancel_requested.discard(task.task_id)
        return cancelled

    def _save_patch_apply_failure(
        self,
        task: TaskRecord,
        patch_artifact: ArtifactRecord,
        error: str,
        *,
        working_root: Path,
    ) -> ArtifactRecord | None:
        """Persist bounded apply diagnostics without masking the original error."""
        if self.data_root is None:
            return None
        directory = self.data_root / "tasks" / task.task_id / "artifacts"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            version = (
                max(
                    (
                        item.version
                        for item in self.store.list_artifacts(task.task_id)
                        if item.kind == "patch_apply_failure"
                    ),
                    default=0,
                )
                + 1
            )
            payload = (
                json.dumps(
                    {
                        "kind": "patch_apply_failure",
                        "task_id": task.task_id,
                        "patch_artifact_id": patch_artifact.artifact_id,
                        "patch_sha256": patch_artifact.sha256,
                        "base_sha": patch_artifact.base_sha,
                        "working_root": str(working_root),
                        "error": error[:500],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            target = directory / f"patch-apply-failure-v{version}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            temporary.replace(target)
            record = ArtifactRecord(
                artifact_id=uuid4().hex,
                task_id=task.task_id,
                kind="patch_apply_failure",
                version=version,
                path=str(target.resolve()),
                sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                base_sha=patch_artifact.base_sha,
                created_at=utc_now(),
            )
            self.store.add_artifact(record)
            return record
        except (OSError, StoreError):
            return None

    def _save_test_report(
        self, task: TaskRecord, verification: VerificationResult
    ) -> ArtifactRecord:
        if self.data_root is None:
            raise ServiceError("task workspace is unavailable")
        directory = self.data_root / "tasks" / task.task_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        version = (
            max(
                (
                    item.version
                    for item in self.store.list_artifacts(task.task_id)
                    if item.kind == "test_report"
                ),
                default=0,
            )
            + 1
        )
        payload = (
            json.dumps(
                verification.as_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=lambda value: (
                    value.isoformat() if hasattr(value, "isoformat") else str(value)
                ),
            )
            + "\n"
        )
        target = directory / f"test-v{version}.json"
        temporary = target.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            temporary.replace(target)
        except OSError as exc:
            raise ServiceError("could not save test report") from exc
        record = ArtifactRecord(
            artifact_id=uuid4().hex,
            task_id=task.task_id,
            kind="test_report",
            version=version,
            path=str(target.resolve()),
            sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            base_sha=task.base_sha,
            created_at=utc_now(),
        )
        self.store.add_artifact(record)
        return record

    def _save_review_report(
        self, task: TaskRecord, result: ReviewResult
    ) -> ArtifactRecord:
        if self.data_root is None:
            raise ServiceError("task workspace is unavailable")
        directory = self.data_root / "tasks" / task.task_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        version = (
            max(
                (
                    item.version
                    for item in self.store.list_artifacts(task.task_id)
                    if item.kind == "review_report"
                ),
                default=0,
            )
            + 1
        )
        payload = (
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        target = directory / f"review-v{version}.json"
        temporary = target.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            temporary.replace(target)
        except OSError as exc:
            raise ServiceError("could not save review report") from exc
        record = ArtifactRecord(
            artifact_id=uuid4().hex,
            task_id=task.task_id,
            kind="review_report",
            version=version,
            path=str(target.resolve()),
            sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            base_sha=task.base_sha,
            created_at=utc_now(),
        )
        self.store.add_artifact(record)
        return record

    def read_artifact(
        self, task_id: str, artifact_id: str
    ) -> tuple[ArtifactRecord, str]:
        self.get_task(task_id)
        artifact = self.store.get_artifact(task_id, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError()
        path = Path(artifact.path)
        try:
            resolved_path = path.resolve()
            if self.artifact_root is not None:
                resolved_path.relative_to(self.artifact_root)
            path = resolved_path
            if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                raise ArtifactNotFoundError()
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != artifact.sha256:
                raise ServiceError(
                    "artifact integrity check failed",
                    code="artifact_corrupt",
                    status_code=409,
                )
            return artifact, payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ServiceError(
                "artifact is not a UTF-8 text file",
                code="artifact_invalid",
                status_code=409,
            ) from exc
        except (OSError, ValueError) as exc:
            raise ArtifactNotFoundError() from exc

    def _record_execution(
        self,
        task: TaskRecord,
        execution: TaskExecution,
        *,
        duration_seconds: float | None = None,
    ) -> TaskRecord:
        if execution.status in {TaskStatus.QUEUED, TaskStatus.ANALYZING}:
            raise ServiceError("executor returned an incomplete status")
        for artifact in execution.artifacts:
            if artifact.task_id != task.task_id:
                raise ServiceError("executor returned an artifact for another task")
            self.store.add_artifact(artifact)
        current_artifact_id = (
            execution.artifacts[0].artifact_id if execution.artifacts else None
        )
        updated = self._save_status(
            task,
            execution.status,
            base_sha=execution.base_sha,
            current_artifact_id=current_artifact_id,
            error=None,
        )
        payload: dict[str, Any] = {
            "status": updated.status.value,
            "base_sha": updated.base_sha,
            "artifact_ids": [item.artifact_id for item in execution.artifacts],
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        self.store.append_event(
            task.task_id,
            "task.stage_completed",
            payload,
        )
        if execution.status == TaskStatus.AWAITING_PLAN_APPROVAL:
            plan_artifact = next(
                (item for item in execution.artifacts if item.kind == "plan_json"),
                None,
            )
            if plan_artifact is not None:
                approval = ApprovalRecord(
                    approval_id=uuid4().hex,
                    task_id=task.task_id,
                    kind=ApprovalKind.PLAN,
                    artifact_hash=plan_artifact.sha256,
                    base_sha=execution.base_sha,
                    decision=None,
                    feedback=None,
                    consumed_at=None,
                    created_at=utc_now(),
                )
                self.store.add_approval(approval)
                self.store.append_event(
                    task.task_id,
                    "approval.required",
                    {
                        "approval_id": approval.approval_id,
                        "kind": approval.kind.value,
                        "artifact_id": plan_artifact.artifact_id,
                    },
                )
        return updated

    def _next_patch_version(self, task_id: str) -> int:
        versions = [
            artifact.version
            for artifact in self.store.list_artifacts(task_id)
            if artifact.kind == "patch"
        ]
        return max(versions, default=0) + 1

    def _apply_approved_patch(
        self, task: TaskRecord, approval: ApprovalRecord
    ) -> TaskRecord:
        patch_artifact = next(
            (
                item
                for item in self.store.list_artifacts(task.task_id)
                if item.kind == "patch" and item.sha256 == approval.artifact_hash
            ),
            None,
        )
        if patch_artifact is None or self.data_root is None:
            raise StateConflictError("approved Patch artifact is unavailable")
        task_root = self.data_root / "tasks" / task.task_id
        snapshot_root = task_root / "snapshot"
        working_root = task_root / "working"
        try:
            if working_root.is_symlink():
                raise StateConflictError("task working copy is not a directory")
            if working_root.exists():
                shutil.rmtree(working_root)
            shutil.copytree(snapshot_root, working_root)
            _artifact, diff = self.read_artifact(
                task.task_id, patch_artifact.artifact_id
            )
            apply_patch(
                diff,
                target_root=working_root,
                base_sha=approval.base_sha or task.base_sha or "",
            )
            reproduction_artifact = next(
                (
                    item
                    for item in self.store.list_artifacts(task.task_id)
                    if item.kind == "reproduction_patch"
                    and item.version == patch_artifact.version
                ),
                None,
            )
            if reproduction_artifact is not None:
                _repro_record, reproduction_diff = self.read_artifact(
                    task.task_id, reproduction_artifact.artifact_id
                )
                apply_patch(
                    reproduction_diff,
                    target_root=working_root,
                    base_sha=approval.base_sha or task.base_sha or "",
                )
        except (OSError, UnicodeError, PatchError, ServiceError) as exc:
            detail = str(exc)[:500] or "patch application failed"
            failure_artifact = self._save_patch_apply_failure(
                task,
                patch_artifact,
                detail,
                working_root=working_root,
            )
            failed = self._save_status(
                task,
                TaskStatus.FAILED,
                current_artifact_id=(
                    failure_artifact.artifact_id
                    if failure_artifact is not None
                    else task.current_artifact_id
                ),
                error=detail,
            )
            self.store.append_event(
                task.task_id,
                "patch.apply_failed",
                {
                    "artifact_id": patch_artifact.artifact_id,
                    "failure_artifact_id": (
                        failure_artifact.artifact_id
                        if failure_artifact is not None
                        else None
                    ),
                    "error": detail,
                },
            )
            return failed
        applied = self._save_status(task, TaskStatus.TESTING)
        self.store.append_event(
            task.task_id,
            "patch.applied",
            {
                "artifact_id": patch_artifact.artifact_id,
                "working_root": str(working_root),
                "tested": False,
            },
        )
        return applied

    def _save_status(
        self,
        task: TaskRecord,
        status: TaskStatus,
        *,
        base_sha: str | None = None,
        current_artifact_id: str | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        updated = replace(
            task,
            status=status,
            base_sha=base_sha if base_sha is not None else task.base_sha,
            current_artifact_id=(
                current_artifact_id
                if current_artifact_id is not None
                else task.current_artifact_id
            ),
            error=error,
            updated_at=utc_now(),
        )
        saved = self.store.save_task(updated)
        self.store.append_event(
            task.task_id,
            "task.status_changed",
            {
                "status": saved.status.value,
                "base_sha": saved.base_sha,
                "current_artifact_id": saved.current_artifact_id,
                "error": saved.error,
            },
        )
        return saved


def build_analysis_executor(
    settings: Settings, *, memory_store: MemoryStore | None = None
) -> TaskExecutor:
    """Build the real read-only analysis stage without running repository code."""

    model_calls = 0

    def execute(task: TaskRecord) -> TaskExecution:
        nonlocal model_calls
        if model_calls >= settings.max_model_calls:
            raise ServiceError(
                "model call limit reached",
                code="model_call_limit",
                status_code=429,
            )
        model_calls += 1
        settings.validate_model_credentials()
        settings.validate_database_url()
        ensure_postgres_schema(settings.database_url)
        task_dir = settings.data_dir / "tasks" / task.task_id
        context = prepare_analysis(
            f"https://github.com/{task.repo}",
            task.issue_number,
            client=GitHubClient(),
            snapshot_root=task_dir / "snapshot",
        )
        with postgres_checkpointer(settings.database_url) as checkpointer:
            memory_context = (
                "\n".join(
                    f"- {item.key}: {item.value}"
                    for item in relevant_memories(memory_store, task.repo)
                )
                if memory_store is not None
                else ""
            )
            plan, artifact = run_model_analysis(
                context,
                model=settings.model,
                thread_id=task.thread_id,
                checkpointer=checkpointer,
                artifact_directory=task_dir / "artifacts",
                memory_context=memory_context[:8_000],
            )
        del plan
        artifacts = tuple(
            _artifact_from_path(
                task.task_id,
                path,
                kind,
                base_sha=context.base_sha,
            )
            for path, kind in (
                (artifact.json_path, "plan_json"),
                (artifact.markdown_path, "plan_markdown"),
            )
        )
        return TaskExecution(
            status=TaskStatus.AWAITING_PLAN_APPROVAL,
            base_sha=context.base_sha,
            artifacts=artifacts,
        )

    return execute


def _artifact_from_path(
    task_id: str, path: Path, kind: str, *, base_sha: str
) -> ArtifactRecord:
    payload = path.read_bytes()
    return ArtifactRecord(
        artifact_id=uuid4().hex,
        task_id=task_id,
        kind=kind,
        version=1,
        path=str(path.resolve()),
        sha256=hashlib.sha256(payload).hexdigest(),
        base_sha=base_sha,
        created_at=utc_now(),
    )
