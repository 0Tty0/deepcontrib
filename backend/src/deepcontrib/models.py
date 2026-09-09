"""Small business records shared by the task service and HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    DRAFTING_PATCH = "drafting_patch"
    AWAITING_PATCH_APPROVAL = "awaiting_patch_approval"
    TESTING = "testing"
    REVIEWING = "reviewing"
    READY_TO_PUBLISH = "ready_to_publish"
    AWAITING_PUBLISH_APPROVAL = "awaiting_publish_approval"
    PUBLISHING = "publishing"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalKind(StrEnum):
    PLAN = "plan"
    PATCH = "patch"
    PUBLISH = "publish"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for persisted records."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    thread_id: str
    repo: str
    issue_number: int
    status: TaskStatus
    base_sha: str | None
    current_artifact_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "status": self.status.value,
            "base_sha": self.base_sha,
            "current_artifact_id": self.current_artifact_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    task_id: str
    kind: str
    version: int
    path: str
    sha256: str
    base_sha: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "version": self.version,
            "path": self.path,
            "sha256": self.sha256,
            "base_sha": self.base_sha,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    task_id: str
    kind: ApprovalKind
    artifact_hash: str
    base_sha: str | None
    decision: ApprovalDecision | None
    feedback: str | None
    consumed_at: datetime | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "task_id": self.task_id,
            "kind": self.kind.value,
            "artifact_hash": self.artifact_hash,
            "base_sha": self.base_sha,
            "decision": self.decision.value if self.decision else None,
            "feedback": self.feedback,
            "consumed_at": self.consumed_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    task_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }
