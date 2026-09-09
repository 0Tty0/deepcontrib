import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from deepcontrib import cli
from deepcontrib.config import ConfigError
from deepcontrib.plan import FileReference, ImplementationPlan, PlanArtifact, save_plan

runner = CliRunner()


class _Settings:
    model = "openai:gpt-5.5"
    database_url = "postgresql://test"

    def validate_model_credentials(self) -> None:
        return None

    def validate_database_url(self) -> None:
        return None

    def validate_limits(self) -> None:
        return None


def test_check_command_reports_ready(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "load_settings", lambda: _Settings())
    monkeypatch.setattr(
        cli,
        "ensure_postgres_schema",
        lambda _url: calls.append("setup"),
    )

    result = runner.invoke(cli.app, ["check"])

    assert result.exit_code == 0
    assert "ready" in result.stdout
    assert calls == ["setup"]


def test_check_command_exposes_configuration_error(monkeypatch: Any) -> None:
    class InvalidSettings(_Settings):
        def validate_model_credentials(self) -> None:
            raise ConfigError("OPENAI_API_KEY is required")

    monkeypatch.setattr(cli, "load_settings", lambda: InvalidSettings())

    result = runner.invoke(cli.app, ["check"])

    assert result.exit_code == 2
    assert "OPENAI_API_KEY" in result.stderr


def test_smoke_command_uses_checkpointer_and_thread(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeAgent:
        pass

    @contextmanager
    def fake_checkpointer(_url: str) -> Any:
        yield "checkpointer"

    monkeypatch.setattr(cli, "load_settings", lambda: _Settings())
    monkeypatch.setattr(cli, "ensure_postgres_schema", lambda _url: None)
    monkeypatch.setattr(cli, "postgres_checkpointer", fake_checkpointer)
    monkeypatch.setattr(
        cli,
        "build_agent",
        lambda model, checkpointer: (
            calls.append(("build", (model, checkpointer))) or FakeAgent()
        ),
    )
    monkeypatch.setattr(
        cli,
        "invoke_with_thread",
        lambda agent, prompt, thread_id: (
            calls.append(("invoke", (agent.__class__.__name__, prompt, thread_id)))
            or {"messages": [{"content": "smoke ok"}]}
        ),
    )

    result = runner.invoke(cli.app, ["smoke", "--thread-id", "cli-thread"])

    assert result.exit_code == 0
    assert "smoke ok" in result.stdout
    assert calls[0] == ("build", ("openai:gpt-5.5", "checkpointer"))
    assert calls[1][0] == "invoke"
    assert calls[1][1][2] == "cli-thread"


def test_analyze_command_connects_read_only_analysis(
    monkeypatch: Any, tmp_path: Path
) -> None:
    artifact = PlanArtifact(tmp_path / "plan-v1.json", tmp_path / "plan-v1.md")
    calls: list[str] = []

    @contextmanager
    def fake_checkpointer(_url: str) -> Any:
        yield "checkpointer"

    monkeypatch.setattr(cli, "load_settings", lambda: _Settings())
    monkeypatch.setattr(
        cli, "ensure_postgres_schema", lambda _url: calls.append("setup")
    )
    monkeypatch.setattr(cli, "postgres_checkpointer", fake_checkpointer)
    monkeypatch.setattr(cli, "prepare_analysis", lambda *args, **kwargs: "context")
    monkeypatch.setattr(
        cli,
        "run_model_analysis",
        lambda *args, **kwargs: (None, artifact),
    )

    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "https://github.com/acme/project",
            "12",
            "--thread-id",
            "analysis-12",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Plan JSON" in result.stdout
    assert calls == ["setup"]


def test_analyze_command_rejects_path_like_thread_id() -> None:
    result = runner.invoke(
        cli.app,
        ["analyze", "https://github.com/acme/project", "1", "--thread-id", "../bad"],
    )

    assert result.exit_code == 2
    assert "thread_id" in result.stderr


def test_show_plan_command_renders_json_and_rejects_other_formats(
    tmp_path: Path,
) -> None:
    plan = ImplementationPlan(
        repo="acme/project",
        issue_number=1,
        issue_title="Fix parser",
        base_sha="a" * 40,
        problem="Parser fails.",
        relevant_files=[
            FileReference(
                path="src/parser.py", start_line=1, end_line=2, reason="evidence"
            )
        ],
    )
    artifact = save_plan(plan, tmp_path)

    json_result = runner.invoke(
        cli.app, ["show-plan", str(artifact.json_path), "--format", "json"]
    )
    bad_result = runner.invoke(
        cli.app, ["show-plan", str(artifact.json_path), "--format", "xml"]
    )

    assert json_result.exit_code == 0
    assert '"repo": "acme/project"' in json_result.stdout
    assert bad_result.exit_code == 2
    assert "format" in bad_result.stderr


def test_serve_command_passes_loopback_options_to_uvicorn(monkeypatch: Any) -> None:
    calls: list[tuple[str, str, int, bool]] = []

    class FakeUvicorn:
        @staticmethod
        def run(app: str, *, host: str, port: int, reload: bool) -> None:
            calls.append((app, host, port, reload))

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    result = runner.invoke(cli.app, ["serve", "--host", "127.0.0.1", "--port", "8123"])

    assert result.exit_code == 0
    assert calls == [("deepcontrib.api:app", "127.0.0.1", 8123, False)]
