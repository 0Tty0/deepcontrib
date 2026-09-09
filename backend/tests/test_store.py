import os
from uuid import uuid4

import psycopg
import pytest

from deepcontrib.models import (
    ApprovalKind,
    ApprovalRecord,
    ArtifactRecord,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from deepcontrib.store import InMemoryTaskStore, PostgresTaskStore, StoreError


def _task(task_id: str = "task-1") -> TaskRecord:
    now = utc_now()
    return TaskRecord(
        task_id=task_id,
        thread_id=f"thread-{task_id}",
        repo="acme/project",
        issue_number=1,
        status=TaskStatus.QUEUED,
        base_sha=None,
        current_artifact_id=None,
        error=None,
        created_at=now,
        updated_at=now,
    )


def test_in_memory_store_round_trips_records_and_events(tmp_path) -> None:
    store = InMemoryTaskStore()
    task = store.create_task(_task())
    artifact_path = tmp_path / "plan.json"
    artifact_path.write_text('{"ok": true}\n', encoding="utf-8")
    artifact = ArtifactRecord(
        artifact_id="artifact-1",
        task_id=task.task_id,
        kind="plan_json",
        version=1,
        path=str(artifact_path),
        sha256="a" * 64,
        base_sha="b" * 40,
        created_at=utc_now(),
    )
    approval = ApprovalRecord(
        approval_id="approval-1",
        task_id=task.task_id,
        kind=ApprovalKind.PLAN,
        artifact_hash=artifact.sha256,
        base_sha=artifact.base_sha,
        decision=None,
        feedback=None,
        consumed_at=None,
        created_at=utc_now(),
    )

    store.add_artifact(artifact)
    store.add_approval(approval)
    event = store.append_event(task.task_id, "task.created", {"ok": True})
    saved = store.save_task(
        task.__class__(
            **{
                **task.as_dict(),
                "status": TaskStatus.ANALYZING,
                "created_at": task.created_at,
                "updated_at": utc_now(),
            }
        )
    )

    assert saved.status == TaskStatus.ANALYZING
    assert store.get_artifact(task.task_id, artifact.artifact_id) == artifact
    assert store.get_approval(approval.approval_id) == approval
    assert store.list_events(task.task_id, after_id=event.event_id) == []
    assert store.list_events(task.task_id)[0].payload == {"ok": True}


def test_in_memory_store_rejects_duplicates_and_foreign_records() -> None:
    store = InMemoryTaskStore()
    task = store.create_task(_task())
    with pytest.raises(StoreError, match="already exists"):
        store.create_task(task)
    with pytest.raises(StoreError, match="thread_id"):
        other = _task("task-2")
        store.create_task(
            other.__class__(**{**other.as_dict(), "thread_id": task.thread_id})
        )
    with pytest.raises(StoreError, match="task does not exist"):
        store.append_event("missing", "x", {})
    with pytest.raises(StoreError, match="negative"):
        store.list_events(task.task_id, after_id=-1)


def test_in_memory_store_marks_each_interrupted_stage_for_resume() -> None:
    store = InMemoryTaskStore()
    tasks = []
    for index, status in enumerate(
        (TaskStatus.ANALYZING, TaskStatus.TESTING, TaskStatus.PUBLISHING),
        start=1,
    ):
        task = _task(f"task-{index}")
        tasks.append(
            store.create_task(task.__class__(**{**task.as_dict(), "status": status}))
        )

    recovered = store.recover_interrupted_tasks()

    assert [item.status for item in recovered] == [TaskStatus.INTERRUPTED] * 3
    assert [item.error for item in recovered] == [
        "process restarted during analyzing",
        "process restarted during testing",
        "process restarted during publishing",
    ]


def test_postgres_store_validates_database_url() -> None:
    with pytest.raises(StoreError, match="PostgreSQL"):
        PostgresTaskStore("sqlite:///tmp/deepcontrib.db")


@pytest.mark.integration
def test_postgres_store_marks_each_interrupted_stage_for_resume() -> None:
    database_url = os.environ.get("DEEPCONTRIB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DEEPCONTRIB_TEST_DATABASE_URL is not configured")

    store = PostgresTaskStore(database_url)
    store.initialize()
    active_statuses = [
        TaskStatus.ANALYZING.value,
        TaskStatus.TESTING.value,
        TaskStatus.PUBLISHING.value,
    ]
    task_ids: list[str] = []
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM deepcontrib_tasks
                    WHERE status = ANY(%s)
                    """,
                    (active_statuses,),
                )
                existing_active = int(cursor.fetchone()[0])
        if existing_active:
            pytest.skip("test database contains unrelated active tasks")

        for index, status in enumerate(
            (TaskStatus.ANALYZING, TaskStatus.TESTING, TaskStatus.PUBLISHING),
            start=1,
        ):
            task_id = f"integration-recovery-{uuid4().hex}-{index}"
            task_ids.append(task_id)
            task = _task(task_id)
            store.create_task(task.__class__(**{**task.as_dict(), "status": status}))

        recovered = store.recover_interrupted_tasks()

        assert {task.task_id for task in recovered} == set(task_ids)
        assert {task.status for task in recovered} == {TaskStatus.INTERRUPTED}
        assert {task.error for task in recovered} == {
            "process restarted during analyzing",
            "process restarted during testing",
            "process restarted during publishing",
        }
    finally:
        if task_ids:
            with psycopg.connect(database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM deepcontrib_tasks WHERE task_id = ANY(%s)",
                        (task_ids,),
                    )
