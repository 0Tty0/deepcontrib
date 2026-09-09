import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from deepcontrib.memory import (
    InMemoryMemoryStore,
    MemoryRecord,
    MemoryStoreError,
    PostgresMemoryStore,
    memory_namespace,
)


def _record(memory_id: str = "m1", namespace: str = "preferences") -> MemoryRecord:
    now = datetime.now(UTC)
    return MemoryRecord(
        memory_id=memory_id,
        namespace=namespace,
        key="format",
        value="Use concise Markdown.",
        source="user",
        repo=namespace.removeprefix("repo:") if namespace.startswith("repo:") else None,
        base_sha=None,
        created_at=now,
        updated_at=now,
    )


def test_memory_store_upserts_and_deletes_explicit_user_memory() -> None:
    store = InMemoryMemoryStore()
    created = store.upsert(_record())
    updated = store.upsert(
        MemoryRecord(**{**created.__dict__, "value": "Use short Markdown sections."})
    )

    assert updated.memory_id == created.memory_id
    assert store.list("preferences")[0].value == "Use short Markdown sections."
    assert store.delete(created.memory_id) is True
    assert store.list("preferences") == []
    assert store.delete(created.memory_id) is False


def test_memory_namespace_is_server_derived_and_sensitive_values_are_rejected() -> None:
    assert memory_namespace(scope="preferences") == "preferences"
    assert (
        memory_namespace(scope="repository", repo="acme/project") == "repo:acme/project"
    )
    with pytest.raises(MemoryStoreError, match="repository"):
        memory_namespace(scope="repository", repo=None)

    store = InMemoryMemoryStore()
    with pytest.raises(MemoryStoreError, match="sensitive"):
        store.upsert(
            MemoryRecord(
                **{
                    **_record().__dict__,
                    "key": "api_token",
                    "value": "ghp_secret",
                }
            )
        )


def test_memory_store_isolates_repository_namespaces() -> None:
    store = InMemoryMemoryStore()
    store.upsert(_record("a", "repo:acme/a"))
    store.upsert(_record("b", "repo:acme/b"))

    assert [item.memory_id for item in store.list("repo:acme/a")] == ["a"]
    assert [item.memory_id for item in store.list("repo:acme/b")] == ["b"]


@pytest.mark.integration
def test_postgres_memory_store_survives_a_new_store_instance() -> None:
    database_url = os.getenv("DEEPCONTRIB_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set DEEPCONTRIB_TEST_DATABASE_URL to run PostgreSQL memory check")
    namespace = f"repo:acme/memory-{uuid4().hex[:8]}"
    record = _record(uuid4().hex, namespace)
    store = PostgresMemoryStore(database_url)
    store.initialize()
    store.upsert(record)

    restored = PostgresMemoryStore(database_url).list(namespace)

    assert restored and restored[0].memory_id == record.memory_id
    assert PostgresMemoryStore(database_url).delete(record.memory_id) is True
