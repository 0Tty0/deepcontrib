"""Command-line entry points for the first DeepContrib milestone."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NoReturn

import typer

from deepcontrib.agent import build_agent, invoke_with_thread, last_message_text
from deepcontrib.analysis import (
    AnalysisError,
    prepare_analysis,
    run_model_analysis,
)
from deepcontrib.checkpoint import (
    CheckpointError,
    ensure_postgres_schema,
    postgres_checkpointer,
)
from deepcontrib.config import ConfigError, load_settings
from deepcontrib.github import GitHubClient, GitHubError
from deepcontrib.plan import PlanError, load_plan
from deepcontrib.repository import RepositoryError

app = typer.Typer(add_completion=False, no_args_is_help=True)
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=2)


@app.command()
def check() -> None:
    """Validate model credentials and initialize PostgreSQL checkpoint tables."""
    settings = load_settings()
    try:
        settings.validate_model_credentials()
        settings.validate_database_url()
        settings.validate_limits()
        ensure_postgres_schema(settings.database_url)
    except (ConfigError, CheckpointError) as exc:
        _fail(str(exc))
    typer.echo("DeepContrib runtime configuration is ready.")


@app.command()
def smoke(
    prompt: str = typer.Option(
        "Call probe_tool with the value hello and report its result.",
        help="Prompt used for the real model tool-call smoke test.",
    ),
    thread_id: str = typer.Option("smoke-thread", help="Durable LangGraph thread id."),
) -> None:
    """Run one real model/tool call using a PostgreSQL-backed thread."""
    settings = load_settings()
    try:
        settings.validate_model_credentials()
        settings.validate_database_url()
        settings.validate_limits()
        ensure_postgres_schema(settings.database_url)
        with postgres_checkpointer(settings.database_url) as checkpointer:
            agent = build_agent(settings.model, checkpointer=checkpointer)
            result = invoke_with_thread(agent, prompt, thread_id)
    except (ConfigError, CheckpointError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(last_message_text(result))


@app.command()
def analyze(
    repo: str = typer.Argument(..., help="Public HTTPS github.com/owner/repo URL."),
    issue: str = typer.Argument(..., help="Positive GitHub Issue number."),
    thread_id: str = typer.Option(
        "analysis-thread", help="Stable LangGraph thread and local task identifier."
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        help="Optional task directory for the snapshot and Plan artifacts.",
    ),
) -> None:
    """Read a public Issue and snapshot, then produce a structured Plan."""
    if not _TASK_ID_PATTERN.fullmatch(thread_id):
        _fail("thread_id must contain only letters, numbers, '.', '_' or '-'")
    settings = load_settings()
    task_dir = output_dir or settings.data_dir / "tasks" / thread_id
    snapshot_dir = task_dir / "snapshot"
    artifact_dir = task_dir / "artifacts"
    try:
        settings.validate_model_credentials()
        settings.validate_database_url()
        settings.validate_limits()
        ensure_postgres_schema(settings.database_url)
        context = prepare_analysis(
            repo,
            issue,
            client=GitHubClient(),
            snapshot_root=snapshot_dir,
        )
        with postgres_checkpointer(settings.database_url) as checkpointer:
            _plan, artifact = run_model_analysis(
                context,
                model=settings.model,
                thread_id=thread_id,
                checkpointer=checkpointer,
                artifact_directory=artifact_dir,
            )
    except (
        AnalysisError,
        CheckpointError,
        ConfigError,
        GitHubError,
        RepositoryError,
        ValueError,
    ) as exc:
        _fail(str(exc))
    typer.echo(f"Plan JSON: {artifact.json_path}")
    typer.echo(f"Plan Markdown: {artifact.markdown_path}")


@app.command("show-plan")
def show_plan(
    path: Path = typer.Argument(..., help="Path to plan-v1.json."),
    format: str = typer.Option("markdown", "--format", help="markdown or json"),
) -> None:
    """Display a previously saved Plan artifact."""
    if format not in {"markdown", "json"}:
        _fail("format must be either markdown or json")
    try:
        plan = load_plan(path)
    except PlanError as exc:
        _fail(str(exc))
    if format == "json":
        typer.echo(plan.model_dump_json(indent=2))
    else:
        typer.echo(plan.to_markdown(), nl=False)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Loopback host for the local API."),
    port: int = typer.Option(8000, min=1, max=65535, help="Local API port."),
) -> None:
    """Start the local FastAPI service."""
    import uvicorn

    uvicorn.run("deepcontrib.api:app", host=host, port=port, reload=False)


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("Cancelled.", err=True)
        sys.exit(130)
