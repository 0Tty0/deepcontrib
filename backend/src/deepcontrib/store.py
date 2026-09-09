"""Durable task records with an in-memory implementation for offline tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Any, Protocol, cast

import psycopg
from psycopg.rows import dict_row

from deepcontrib.checkpoint import _with_connection_timeout
from deepcontrib.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    ArtifactRecord,
    EventRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)


class StoreError(RuntimeError):
    """Raised when task records cannot be read or written."""


class TaskStore(Protocol):
    """Persistence operations required by the task service."""

    def initialize(self) -> None: ...

    def create_task(self, task: TaskRecord) -> TaskRecord: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def save_task(self, task: TaskRecord) -> TaskRecord: ...

    def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord: ...

    def get_artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord | None: ...

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]: ...

    def add_approval(self, approval: ApprovalRecord) -> ApprovalRecord: ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]: ...

    def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord: ...

    def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> EventRecord: ...

    def list_events(self, task_id: str, *, after_id: int = 0) -> list[EventRecord]: ...

    def recover_interrupted_tasks(self) -> list[TaskRecord]: ...


class InMemoryTaskStore:
    """Thread-safe store used for deterministic API and service tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._events: dict[str, list[EventRecord]] = {}
        self._next_event_id = 1

    def initialize(self) -> None:
        return None

    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            if task.task_id in self._tasks:
                raise StoreError("task already exists")
            if any(item.thread_id == task.thread_id for item in self._tasks.values()):
                raise StoreError("thread_id is already in use")
            self._tasks[task.task_id] = task
            self._events[task.task_id] = []
            return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def save_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            if task.task_id not in self._tasks:
                raise StoreError("task does not exist")
            self._tasks[task.task_id] = task
            return task

    def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self._lock:
            if artifact.task_id not in self._tasks:
                raise StoreError("task does not exist")
            if artifact.artifact_id in self._artifacts:
                raise StoreError("artifact already exists")
            self._artifacts[artifact.artifact_id] = artifact
            return artifact

    def get_artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            return artifact if artifact and artifact.task_id == task_id else None

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        with self._lock:
            return sorted(
                (
                    artifact
                    for artifact in self._artifacts.values()
                    if artifact.task_id == task_id
                ),
                key=lambda item: (item.version, item.created_at, item.artifact_id),
            )

    def add_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._lock:
            if approval.task_id not in self._tasks:
                raise StoreError("task does not exist")
            if approval.approval_id in self._approvals:
                raise StoreError("approval already exists")
            self._approvals[approval.approval_id] = approval
            return approval

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return sorted(
                (
                    approval
                    for approval in self._approvals.values()
                    if approval.task_id == task_id
                ),
                key=lambda item: item.created_at,
            )

    def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._lock:
            if approval.approval_id not in self._approvals:
                raise StoreError("approval does not exist")
            self._approvals[approval.approval_id] = approval
            return approval

    def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> EventRecord:
        with self._lock:
            if task_id not in self._tasks:
                raise StoreError("task does not exist")
            event = EventRecord(
                event_id=self._next_event_id,
                task_id=task_id,
                event_type=event_type,
                payload=dict(payload),
                created_at=utc_now(),
            )
            self._next_event_id += 1
            self._events[task_id].append(event)
            return event

    def list_events(self, task_id: str, *, after_id: int = 0) -> list[EventRecord]:
        if after_id < 0:
            raise StoreError("after_id must not be negative")
        with self._lock:
            if task_id not in self._tasks:
                return []
            return [
                event for event in self._events[task_id] if event.event_id > after_id
            ]

    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        with self._lock:
            active = [
                task
                for task in self._tasks.values()
                if task.status
                in {
                    TaskStatus.ANALYZING,
                    TaskStatus.TESTING,
                    TaskStatus.PUBLISHING,
                }
            ]
            recovered: list[TaskRecord] = []
            for task in active:
                updated = replace(
                    task,
                    status=TaskStatus.INTERRUPTED,
                    error=f"process restarted during {task.status.value}",
                    updated_at=utc_now(),
                )
                self._tasks[task.task_id] = updated
                recovered.append(updated)
            return recovered


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deepcontrib_tasks (
    task_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL CHECK (issue_number > 0),
    status TEXT NOT NULL,
    base_sha TEXT,
    current_artifact_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS deepcontrib_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES deepcontrib_tasks(task_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    base_sha TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS deepcontrib_approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES deepcontrib_tasks(task_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    base_sha TEXT,
    decision TEXT,
    feedback TEXT,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS deepcontrib_events (
    event_id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES deepcontrib_tasks(task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS deepcontrib_events_task_id_event_id
    ON deepcontrib_events(task_id, event_id);
"""


def initialize_business_schema(database_url: str) -> None:
    """Create the small business schema used beside LangGraph checkpoints."""
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise StoreError("DATABASE_URL must be a PostgreSQL URL")
    try:
        with psycopg.connect(
            _with_connection_timeout(database_url), row_factory=dict_row
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SCHEMA)
    except Exception as exc:  # pragma: no cover - concrete driver errors vary
        raise StoreError(
            "Could not initialize PostgreSQL task tables. Verify DATABASE_URL."
        ) from exc


class PostgresTaskStore:
    """PostgreSQL-backed store; each operation uses a short local transaction."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise StoreError("DATABASE_URL must be a PostgreSQL URL")
        self.database_url = database_url

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with psycopg.connect(
                _with_connection_timeout(self.database_url), row_factory=dict_row
            ) as connection:
                yield connection
        except StoreError:
            raise
        except Exception as exc:  # pragma: no cover - concrete driver errors vary
            raise StoreError(
                "Could not connect to PostgreSQL task storage. Verify DATABASE_URL."
            ) from exc

    def initialize(self) -> None:
        initialize_business_schema(self.database_url)

    def create_task(self, task: TaskRecord) -> TaskRecord:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO deepcontrib_tasks
                        (task_id, thread_id, repo, issue_number, status, base_sha,
                         current_artifact_id, error, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            task.task_id,
                            task.thread_id,
                            task.repo,
                            task.issue_number,
                            task.status.value,
                            task.base_sha,
                            task.current_artifact_id,
                            task.error,
                            task.created_at,
                            task.updated_at,
                        ),
                    )
            return task
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError("could not create task") from exc

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT task_id, thread_id, repo, issue_number, status, base_sha,
                           current_artifact_id, error, created_at, updated_at
                    FROM deepcontrib_tasks WHERE task_id = %s
                    """,
                    (task_id,),
                )
                row = cursor.fetchone()
        return _task_from_row(cast(dict[str, Any] | None, row))

    def save_task(self, task: TaskRecord) -> TaskRecord:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE deepcontrib_tasks SET thread_id = %s, repo = %s,
                        issue_number = %s, status = %s, base_sha = %s,
                        current_artifact_id = %s, error = %s, updated_at = %s
                    WHERE task_id = %s
                    """,
                    (
                        task.thread_id,
                        task.repo,
                        task.issue_number,
                        task.status.value,
                        task.base_sha,
                        task.current_artifact_id,
                        task.error,
                        task.updated_at,
                        task.task_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreError("task does not exist")
        return task

    def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO deepcontrib_artifacts
                    (artifact_id, task_id, kind, version, path, sha256, base_sha,
                     created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.task_id,
                        artifact.kind,
                        artifact.version,
                        artifact.path,
                        artifact.sha256,
                        artifact.base_sha,
                        artifact.created_at,
                    ),
                )
        return artifact

    def get_artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT artifact_id, task_id, kind, version, path, sha256,
                           base_sha, created_at
                    FROM deepcontrib_artifacts
                    WHERE task_id = %s AND artifact_id = %s
                    """,
                    (task_id, artifact_id),
                )
                row = cursor.fetchone()
        return _artifact_from_row(cast(dict[str, Any] | None, row))

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT artifact_id, task_id, kind, version, path, sha256,
                           base_sha, created_at
                    FROM deepcontrib_artifacts
                    WHERE task_id = %s ORDER BY version ASC, created_at ASC
                    """,
                    (task_id,),
                )
                rows = cursor.fetchall()
        artifacts: list[ArtifactRecord] = []
        for row in rows:
            artifact = _artifact_from_row(cast(dict[str, Any], row))
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def add_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO deepcontrib_approvals
                    (approval_id, task_id, kind, artifact_hash, base_sha, decision,
                     feedback, consumed_at, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        approval.approval_id,
                        approval.task_id,
                        approval.kind.value,
                        approval.artifact_hash,
                        approval.base_sha,
                        approval.decision.value if approval.decision else None,
                        approval.feedback,
                        approval.consumed_at,
                        approval.created_at,
                    ),
                )
        return approval

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT approval_id, task_id, kind, artifact_hash, base_sha,
                           decision, feedback, consumed_at, created_at
                    FROM deepcontrib_approvals WHERE approval_id = %s
                    """,
                    (approval_id,),
                )
                row = cursor.fetchone()
        return _approval_from_row(cast(dict[str, Any] | None, row))

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT approval_id, task_id, kind, artifact_hash, base_sha,
                           decision, feedback, consumed_at, created_at
                    FROM deepcontrib_approvals
                    WHERE task_id = %s ORDER BY created_at ASC
                    """,
                    (task_id,),
                )
                rows = cursor.fetchall()
        approvals: list[ApprovalRecord] = []
        for row in rows:
            approval = _approval_from_row(cast(dict[str, Any], row))
            if approval is not None:
                approvals.append(approval)
        return approvals

    def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE deepcontrib_approvals SET decision = %s,
                        feedback = %s, consumed_at = %s
                    WHERE approval_id = %s
                    """,
                    (
                        approval.decision.value if approval.decision else None,
                        approval.feedback,
                        approval.consumed_at,
                        approval.approval_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreError("approval does not exist")
        return approval

    def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> EventRecord:
        created_at = utc_now()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO deepcontrib_events
                    (task_id, event_type, payload, created_at)
                    VALUES (%s, %s, %s::jsonb, %s)
                    RETURNING event_id
                    """,
                    (task_id, event_type, json.dumps(payload), created_at),
                )
                row = cursor.fetchone()
        if not row:
            raise StoreError("could not persist task event")
        event_id = int(cast(dict[str, Any], row)["event_id"])
        return EventRecord(event_id, task_id, event_type, dict(payload), created_at)

    def list_events(self, task_id: str, *, after_id: int = 0) -> list[EventRecord]:
        if after_id < 0:
            raise StoreError("after_id must not be negative")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, task_id, event_type, payload, created_at
                    FROM deepcontrib_events
                    WHERE task_id = %s AND event_id > %s
                    ORDER BY event_id ASC LIMIT 1000
                    """,
                    (task_id, after_id),
                )
                rows = cursor.fetchall()
        return [_event_from_row(cast(dict[str, Any], row)) for row in rows]

    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        active_statuses = [
            TaskStatus.ANALYZING.value,
            TaskStatus.TESTING.value,
            TaskStatus.PUBLISHING.value,
        ]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT task_id, status
                    FROM deepcontrib_tasks
                    WHERE status = ANY(%s)
                    FOR UPDATE
                    """,
                    (active_statuses,),
                )
                active_rows = [cast(dict[str, Any], row) for row in cursor.fetchall()]
                updated_at = utc_now()
                for row in active_rows:
                    task_id = str(row["task_id"])
                    previous_status = str(row["status"])
                    cursor.execute(
                        """
                        UPDATE deepcontrib_tasks
                        SET status = %s,
                            error = %s,
                            updated_at = %s
                        WHERE task_id = %s AND status = %s
                        """,
                        (
                            TaskStatus.INTERRUPTED.value,
                            f"process restarted during {previous_status}",
                            updated_at,
                            task_id,
                            previous_status,
                        ),
                    )
        recovered: list[TaskRecord] = []
        for row in active_rows:
            task = self.get_task(str(row["task_id"]))
            if task is not None:
                recovered.append(task)
        return recovered


def _task_from_row(row: dict[str, Any] | None) -> TaskRecord | None:
    if row is None:
        return None
    return TaskRecord(
        task_id=str(row["task_id"]),
        thread_id=str(row["thread_id"]),
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        status=TaskStatus(str(row["status"])),
        base_sha=row["base_sha"],
        current_artifact_id=row["current_artifact_id"],
        error=row["error"],
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def _artifact_from_row(row: dict[str, Any] | None) -> ArtifactRecord | None:
    if row is None:
        return None
    return ArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        task_id=str(row["task_id"]),
        kind=str(row["kind"]),
        version=int(row["version"]),
        path=str(row["path"]),
        sha256=str(row["sha256"]),
        base_sha=row["base_sha"],
        created_at=cast(datetime, row["created_at"]),
    )


def _approval_from_row(row: dict[str, Any] | None) -> ApprovalRecord | None:
    if row is None:
        return None
    decision = row["decision"]
    return ApprovalRecord(
        approval_id=str(row["approval_id"]),
        task_id=str(row["task_id"]),
        kind=ApprovalKind(str(row["kind"])),
        artifact_hash=str(row["artifact_hash"]),
        base_sha=row["base_sha"],
        decision=ApprovalDecision(str(decision)) if decision else None,
        feedback=row["feedback"],
        consumed_at=cast(datetime | None, row["consumed_at"]),
        created_at=cast(datetime, row["created_at"]),
    )


def _event_from_row(row: dict[str, Any]) -> EventRecord:
    payload = row["payload"]
    return EventRecord(
        event_id=int(row["event_id"]),
        task_id=str(row["task_id"]),
        event_type=str(row["event_type"]),
        payload=dict(payload) if isinstance(payload, dict) else json.loads(payload),
        created_at=cast(datetime, row["created_at"]),
    )
