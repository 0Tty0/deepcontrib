"""Small, explicit long-term memory store for user preferences and repo facts."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Any, Protocol, cast

import psycopg
from psycopg.rows import dict_row

from deepcontrib.checkpoint import _with_connection_timeout
from deepcontrib.repository import parse_repository_url


class MemoryStoreError(ValueError):
    """Raised when memory input or persistence is invalid."""


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    namespace: str
    key: str
    value: str
    source: str
    repo: str | None
    base_sha: str | None
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "namespace": self.namespace,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "repo": self.repo,
            "base_sha": self.base_sha,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MemoryStore(Protocol):
    def initialize(self) -> None: ...

    def upsert(self, memory: MemoryRecord) -> MemoryRecord: ...

    def get(self, memory_id: str) -> MemoryRecord | None: ...

    def list(self, namespace: str) -> list[MemoryRecord]: ...

    def delete(self, memory_id: str) -> bool: ...


def relevant_memories(store: MemoryStore, repo: str) -> list[MemoryRecord]:
    """Return a small, namespace-isolated context for one repository."""
    try:
        repo_namespace = memory_namespace(scope="repository", repo=repo)
    except MemoryStoreError:
        return []
    records = [*store.list("preferences"), *store.list(repo_namespace)]
    return sorted(records, key=lambda item: (item.namespace, item.key))[:40]


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|token|password|secret|private[_-]?key)", re.I
)
_SENSITIVE_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9\-]{8,}|BEGIN\s+[^\n]*PRIVATE KEY)",
    re.I,
)


def memory_namespace(*, scope: str, repo: str | None = None) -> str:
    """Derive one of the two application-owned namespaces."""
    if scope == "preferences":
        if repo is not None:
            raise MemoryStoreError(
                "preferences namespace must not include a repository"
            )
        return "preferences"
    if scope != "repository" or not repo:
        raise MemoryStoreError("repository memory requires a repository")
    raw = repo.strip()
    try:
        parsed = (
            parse_repository_url(raw)
            if raw.startswith("https://")
            else parse_repository_url(f"https://github.com/{raw}")
        )
    except ValueError as exc:
        raise MemoryStoreError(
            "repository memory requires a valid GitHub repository"
        ) from exc
    return f"repo:{parsed.full_name}"


def _validate_memory(memory: MemoryRecord) -> None:
    if not _ID_PATTERN.fullmatch(memory.memory_id):
        raise MemoryStoreError("memory_id is invalid")
    if memory.namespace != "preferences" and not memory.namespace.startswith("repo:"):
        raise MemoryStoreError("memory namespace is not application-owned")
    if not memory.key.strip() or len(memory.key) > 120:
        raise MemoryStoreError(
            "memory key must be non-empty and at most 120 characters"
        )
    if not memory.value.strip() or len(memory.value) > 20_000:
        raise MemoryStoreError(
            "memory value must be non-empty and at most 20000 characters"
        )
    if not memory.source.strip() or len(memory.source) > 120:
        raise MemoryStoreError(
            "memory source must be non-empty and at most 120 characters"
        )
    if _SENSITIVE_KEY.search(memory.key) or _SENSITIVE_VALUE.search(memory.value):
        raise MemoryStoreError("sensitive credentials cannot be stored in memory")
    if memory.base_sha is not None and not _SHA_PATTERN.fullmatch(memory.base_sha):
        raise MemoryStoreError("memory base_sha must be a 40-character SHA")
    if memory.namespace.startswith("repo:"):
        canonical = memory.namespace.removeprefix("repo:")
        try:
            parse_repository_url(f"https://github.com/{canonical}")
        except ValueError as exc:
            raise MemoryStoreError("memory repository namespace is invalid") from exc
        if memory.repo != canonical:
            raise MemoryStoreError("memory repo must match its namespace")
    elif memory.repo is not None or memory.base_sha is not None:
        raise MemoryStoreError("preference memory must not include repository fields")


class InMemoryMemoryStore:
    """Thread-safe implementation used by API tests and offline development."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, MemoryRecord] = {}

    def initialize(self) -> None:
        return None

    def upsert(self, memory: MemoryRecord) -> MemoryRecord:
        _validate_memory(memory)
        with self._lock:
            existing = next(
                (
                    item
                    for item in self._items.values()
                    if item.namespace == memory.namespace and item.key == memory.key
                ),
                None,
            )
            if existing is not None and existing.memory_id != memory.memory_id:
                memory = replace(
                    memory, memory_id=existing.memory_id, created_at=existing.created_at
                )
            self._items[memory.memory_id] = memory
            return memory

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            return self._items.get(memory_id)

    def list(self, namespace: str) -> list[MemoryRecord]:
        if not namespace or (
            namespace != "preferences" and not namespace.startswith("repo:")
        ):
            raise MemoryStoreError("memory namespace is not application-owned")
        with self._lock:
            return sorted(
                (item for item in self._items.values() if item.namespace == namespace),
                key=lambda item: (item.key, item.updated_at, item.memory_id),
            )

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            return self._items.pop(memory_id, None) is not None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deepcontrib_memories (
    memory_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    repo TEXT,
    base_sha TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(namespace, key)
);
CREATE INDEX IF NOT EXISTS deepcontrib_memories_namespace
    ON deepcontrib_memories(namespace, updated_at DESC);
"""


class PostgresMemoryStore:
    """PostgreSQL-backed memory store sharing the task database."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise MemoryStoreError("DATABASE_URL must be a PostgreSQL URL")
        self.database_url = database_url

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with psycopg.connect(
                _with_connection_timeout(self.database_url), row_factory=dict_row
            ) as connection:
                yield connection
        except MemoryStoreError:
            raise
        except Exception as exc:  # pragma: no cover - concrete driver errors vary
            raise MemoryStoreError(
                "could not connect to PostgreSQL memory storage"
            ) from exc

    def initialize(self) -> None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_SCHEMA)
        except MemoryStoreError:
            raise
        except Exception as exc:  # pragma: no cover
            raise MemoryStoreError(
                "could not initialize PostgreSQL memory storage"
            ) from exc

    def upsert(self, memory: MemoryRecord) -> MemoryRecord:
        _validate_memory(memory)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO deepcontrib_memories
                        (memory_id, namespace, key, value, source, repo, base_sha,
                         created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (namespace, key) DO UPDATE SET
                            value = EXCLUDED.value,
                            source = EXCLUDED.source,
                            repo = EXCLUDED.repo,
                            base_sha = EXCLUDED.base_sha,
                            updated_at = EXCLUDED.updated_at
                        RETURNING memory_id, namespace, key, value, source, repo,
                                  base_sha, created_at, updated_at
                        """,
                        (
                            memory.memory_id,
                            memory.namespace,
                            memory.key,
                            memory.value,
                            memory.source,
                            memory.repo,
                            memory.base_sha,
                            memory.created_at,
                            memory.updated_at,
                        ),
                    )
                    row = cursor.fetchone()
            if row is None:
                raise MemoryStoreError("could not persist memory")
            restored = _memory_from_row(cast(dict[str, Any], row))
            if restored is None:
                raise MemoryStoreError("could not decode persisted memory")
            return restored
        except MemoryStoreError:
            raise
        except Exception as exc:
            raise MemoryStoreError("could not save memory") from exc

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT memory_id, namespace, key, value, source, repo, base_sha,
                           created_at, updated_at
                    FROM deepcontrib_memories WHERE memory_id = %s
                    """,
                    (memory_id,),
                )
                row = cursor.fetchone()
        return _memory_from_row(cast(dict[str, Any] | None, row))

    def list(self, namespace: str) -> list[MemoryRecord]:
        if not namespace or (
            namespace != "preferences" and not namespace.startswith("repo:")
        ):
            raise MemoryStoreError("memory namespace is not application-owned")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT memory_id, namespace, key, value, source, repo, base_sha,
                           created_at, updated_at
                    FROM deepcontrib_memories WHERE namespace = %s
                    ORDER BY key ASC, updated_at ASC, memory_id ASC
                    """,
                    (namespace,),
                )
                rows = cursor.fetchall()
        return [
            item
            for row in rows
            if (item := _memory_from_row(cast(dict[str, Any], row))) is not None
        ]

    def delete(self, memory_id: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM deepcontrib_memories WHERE memory_id = %s",
                    (memory_id,),
                )
                return bool(cast(int, cursor.rowcount) == 1)


def _memory_from_row(row: dict[str, Any] | None) -> MemoryRecord | None:
    if row is None:
        return None
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        namespace=str(row["namespace"]),
        key=str(row["key"]),
        value=str(row["value"]),
        source=str(row["source"]),
        repo=str(row["repo"]) if row["repo"] is not None else None,
        base_sha=str(row["base_sha"]) if row["base_sha"] is not None else None,
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )
