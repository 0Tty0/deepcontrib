"""Model-backed, read-only generation of an approval-bound unified diff."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from deepcontrib.agent import invoke_with_thread, resolve_configured_model
from deepcontrib.analysis import AnalysisContext, build_analysis_tools
from deepcontrib.checkpoint import postgres_checkpointer
from deepcontrib.config import Settings
from deepcontrib.github import IssueSnapshot
from deepcontrib.models import TaskRecord
from deepcontrib.patches import PatchError, build_unified_diff_from_contents
from deepcontrib.plan import ImplementationPlan
from deepcontrib.repository import (
    RepositorySnapshot,
    parse_repository_url,
)


class PatchGenerationError(RuntimeError):
    """Raised when a model cannot produce a valid structured patch proposal."""


class PatchFile(BaseModel):
    """One complete text file after the approved change."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str


class ModelPatchProposal(BaseModel):
    """Required structured response shape sent to the model provider."""

    model_config = ConfigDict(extra="forbid")

    files: list[PatchFile] = Field(min_length=1)
    reproduction_diff: str | None = None


class PatchProposal(BaseModel):
    """The model response used to create an approval-bound Patch artifact."""

    model_config = ConfigDict(extra="forbid")

    diff: str | None = None
    files: list[PatchFile] | None = Field(default=None, min_length=1)
    reproduction_diff: str | None = None

    @model_validator(mode="after")
    def validate_patch_source(self) -> PatchProposal:
        if self.diff is None and self.files is None:
            raise ValueError("PatchProposal requires diff or files")
        if self.diff is not None:
            if not self.diff.strip():
                raise ValueError("PatchProposal diff must not be blank")
            normalized = self.diff.replace("\r\n", "\n").replace("\r", "\n")
            self.diff = normalized if normalized.endswith("\n") else normalized + "\n"
        return self


def _build_patch_tools(context: AnalysisContext) -> tuple[Callable[..., Any], ...]:
    """Make read-only tools report bad model paths instead of aborting a run."""

    guarded: list[Callable[..., Any]] = []
    for tool in build_analysis_tools(context):

        @wraps(tool)
        def call(*args: Any, _tool: Callable[..., Any] = tool, **kwargs: Any) -> Any:
            try:
                return _tool(*args, **kwargs)
            except ValueError as exc:
                return (
                    f"Tool input error: {exc}. Use repository-relative paths only; "
                    "never pass an absolute snapshot filesystem path."
                )

        guarded.append(call)
    return tuple(guarded)


def _patch_context(
    plan: ImplementationPlan, snapshot: RepositorySnapshot
) -> AnalysisContext:
    repo = parse_repository_url(f"https://github.com/{plan.repo}")
    return AnalysisContext(
        repo=repo,
        issue=IssueSnapshot(
            number=plan.issue_number,
            title=plan.issue_title,
            body=plan.problem,
            state="unknown",
            html_url=f"https://github.com/{plan.repo}/issues/{plan.issue_number}",
            comments=(),
            comments_truncated=False,
        ),
        base_sha=plan.base_sha,
        snapshot=snapshot,
    )


def build_patch_agent(
    model: Any,
    plan: ImplementationPlan,
    snapshot: RepositorySnapshot,
    *,
    checkpointer: Any | None = None,
) -> Any:
    """Build a read-only agent that proposes, but never applies, a patch."""
    context = _patch_context(plan, snapshot)
    kwargs: dict[str, Any] = {
        "model": resolve_configured_model(model),
        "tools": list(_build_patch_tools(context)),
        "middleware": [TodoListMiddleware()],
        "response_format": ModelPatchProposal,
        "system_prompt": """You are the DeepContrib patch author.

Inspect the fixed repository snapshot with the read-only tools and implement
the approved ImplementationPlan. Return a PatchProposal whose `files` field
contains one entry for each changed text file. Each entry must contain the
repository-relative path and the complete final UTF-8 file content, including
the final newline when the file uses one. Do not include unchanged files.
Change only paths listed in the ImplementationPlan. Do not write files or
execute commands. Keep the change focused on the approved problem and include
a regression test when the plan calls for one. Leave `diff` null; the service
will build and validate the unified diff locally. Set `reproduction_diff` only
when a separate test-only reproduction patch is required.
All tool `path` and `directory` arguments must be repository-relative, such as
`.` or `src`; never pass the snapshot's absolute filesystem path.

Approved ImplementationPlan:
"""
        + plan.model_dump_json(indent=2),
    }
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return create_deep_agent(**kwargs)


def run_model_patch(
    plan: ImplementationPlan,
    snapshot_root: Path,
    *,
    model: Any,
    thread_id: str,
    checkpointer: Any | None = None,
) -> PatchProposal:
    """Generate and validate one structured PatchProposal from a fixed snapshot."""
    snapshot = RepositorySnapshot(snapshot_root, plan.base_sha)
    agent = build_patch_agent(
        model,
        plan,
        snapshot,
        checkpointer=checkpointer,
    )
    prompt = (
        f"Create the complete changed files for the approved plan in {plan.repo} "
        f"at base SHA {plan.base_sha}. Return only the PatchProposal."
    )
    result = invoke_with_thread(agent, prompt, thread_id)
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if structured is None:
        raise PatchGenerationError(
            "patch model did not return a structured PatchProposal"
        )
    if isinstance(structured, PatchProposal):
        proposal = PatchProposal.model_validate(structured)
        model_files = proposal.files
        reproduction_diff = proposal.reproduction_diff
        if model_files is None:
            if not proposal.diff:
                raise PatchGenerationError("patch model did not provide a diff")
            return proposal
    else:
        try:
            model_proposal = ModelPatchProposal.model_validate(structured)
        except ValueError as exc:
            raise PatchGenerationError(
                "patch model returned an invalid PatchProposal"
            ) from exc
        model_files = model_proposal.files
        reproduction_diff = model_proposal.reproduction_diff
    if model_files is not None:
        paths: dict[str, str | None] = {}
        for file in model_files:
            if file.path in paths:
                raise PatchGenerationError(
                    f"patch model returned duplicate file path: {file.path}"
                )
            paths[file.path] = file.content
        try:
            diff = build_unified_diff_from_contents(snapshot_root, paths)
        except PatchError as exc:
            raise PatchGenerationError(str(exc)) from exc
        return PatchProposal(diff=diff, reproduction_diff=reproduction_diff)
    raise PatchGenerationError("patch model did not provide changed files")


def build_patch_generator(settings: Settings) -> Any:
    """Build the production generator with a bounded model-call budget."""
    model_calls = 0

    def generate(
        task: TaskRecord,
        plan: ImplementationPlan,
        snapshot_root: Path,
    ) -> PatchProposal:
        nonlocal model_calls
        if model_calls >= settings.max_model_calls:
            raise PatchGenerationError("model call limit reached")
        model_calls += 1
        settings.validate_model_credentials()
        settings.validate_database_url()
        with postgres_checkpointer(settings.database_url) as checkpointer:
            return run_model_patch(
                plan,
                snapshot_root,
                model=settings.model,
                thread_id=f"patch-{task.task_id}",
                checkpointer=checkpointer,
            )

    return generate
