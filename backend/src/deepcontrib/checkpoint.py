"""PostgreSQL checkpoint lifecycle for durable Deep Agent threads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langgraph.checkpoint.postgres import PostgresSaver


class CheckpointError(RuntimeError):
    """Raised when the PostgreSQL checkpointer cannot be prepared."""


def _validate_database_url(database_url: str) -> None:
    if not database_url.strip():
        raise CheckpointError(
            "DATABASE_URL is required to initialize the PostgreSQL checkpointer"
        )
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise CheckpointError(
            "DATABASE_URL must be a PostgreSQL URL beginning with postgresql://"
        )


def _with_connection_timeout(database_url: str, timeout_seconds: int = 5) -> str:
    """Keep prerequisite checks from hanging when PostgreSQL is unavailable."""
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("connect_timeout", str(timeout_seconds))
    return urlunsplit(parts._replace(query=urlencode(query)))


def ensure_postgres_schema(database_url: str) -> None:
    """Create LangGraph checkpoint tables, translating failures for the CLI."""
    _validate_database_url(database_url)
    try:
        connection_url = _with_connection_timeout(database_url)
        with PostgresSaver.from_conn_string(connection_url) as checkpointer:
            checkpointer.setup()
    except Exception as exc:  # pragma: no cover - concrete driver errors vary
        raise CheckpointError(
            "Could not initialize PostgreSQL checkpoint tables. "
            "Start the database and verify DATABASE_URL."
        ) from exc


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """Keep a PostgresSaver open for the complete graph invocation."""
    _validate_database_url(database_url)
    try:
        connection_url = _with_connection_timeout(database_url)
        manager = PostgresSaver.from_conn_string(connection_url)
        checkpointer = manager.__enter__()
    except Exception as exc:  # pragma: no cover - concrete driver errors vary
        raise CheckpointError(
            "Could not connect to PostgreSQL. Verify DATABASE_URL and that "
            "the database is running."
        ) from exc

    try:
        yield checkpointer
    except BaseException as body_error:
        # Do not translate an exception raised by the graph or model into a
        # misleading database connection error.  Best-effort cleanup also
        # preserves the original failure if the driver close path misbehaves.
        try:
            manager.__exit__(type(body_error), body_error, body_error.__traceback__)
        except BaseException:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover - concrete driver errors vary
            raise CheckpointError(
                "Could not close the PostgreSQL checkpointer cleanly."
            ) from exc
