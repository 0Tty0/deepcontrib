from typing import Any

import pytest

from deepcontrib.checkpoint import (
    CheckpointError,
    ensure_postgres_schema,
    postgres_checkpointer,
)


class _FakeSaver:
    setup_calls = 0

    def setup(self) -> None:
        self.setup_calls += 1

    def __enter__(self) -> "_FakeSaver":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakePostgresSaver:
    saver = _FakeSaver()

    @classmethod
    def from_conn_string(cls, value: str) -> _FakeSaver:
        assert value.startswith("postgresql://")
        return cls.saver


def test_ensure_postgres_schema_calls_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepcontrib.checkpoint.PostgresSaver", _FakePostgresSaver)

    ensure_postgres_schema("postgresql://user:pass@localhost:5432/db")

    assert _FakePostgresSaver.saver.setup_calls == 1


def test_ensure_postgres_schema_rejects_blank_url() -> None:
    with pytest.raises(CheckpointError, match="DATABASE_URL"):
        ensure_postgres_schema("")


def test_ensure_postgres_schema_translates_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPostgresSaver:
        @classmethod
        def from_conn_string(cls, _value: str) -> Any:
            raise RuntimeError("database is offline")

    monkeypatch.setattr("deepcontrib.checkpoint.PostgresSaver", FailingPostgresSaver)

    with pytest.raises(CheckpointError, match="Could not initialize"):
        ensure_postgres_schema("postgresql://user:pass@localhost:5432/db")


def test_postgres_checkpointer_rejects_non_postgres_url() -> None:
    with pytest.raises(CheckpointError, match="PostgreSQL URL"):
        with postgres_checkpointer("sqlite:///deepcontrib.db"):
            pass


def test_postgres_checkpointer_translates_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPostgresSaver:
        @classmethod
        def from_conn_string(cls, _value: str) -> Any:
            raise RuntimeError("database is offline")

    monkeypatch.setattr("deepcontrib.checkpoint.PostgresSaver", FailingPostgresSaver)

    with pytest.raises(CheckpointError, match="Could not connect"):
        with postgres_checkpointer("postgresql://user:pass@localhost:5432/db"):
            pass


def test_postgres_checkpointer_preserves_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deepcontrib.checkpoint.PostgresSaver", _FakePostgresSaver)

    with pytest.raises(RuntimeError, match="model failed"):
        with postgres_checkpointer("postgresql://user:pass@localhost:5432/db"):
            raise RuntimeError("model failed")
