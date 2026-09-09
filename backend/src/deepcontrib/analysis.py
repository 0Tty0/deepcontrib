"""Read-only Issue analysis and structured Plan generation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from deepcontrib.agent import invoke_with_thread, resolve_configured_model
from deepcontrib.github import GitHubClient, IssueSnapshot
from deepcontrib.plan import FileReference, ImplementationPlan, PlanArtifact, save_plan
from deepcontrib.repository import (
    RepositoryRef,
    RepositorySnapshot,
    parse_issue_number,
    parse_repository_url,
)


class AnalysisError(RuntimeError):
    """Raised when a read-only analysis cannot produce a valid plan."""


@dataclass(frozen=True)
class AnalysisContext:
    repo: RepositoryRef
    issue: IssueSnapshot
    base_sha: str
    snapshot: RepositorySnapshot


def prepare_analysis(
    repo_url: str,
    issue_number: str | int,
    *,
    client: GitHubClient,
    snapshot_root: Path,
) -> AnalysisContext:
    """Fetch issue metadata and one fixed-SHA read-only repository snapshot."""
    repo = parse_repository_url(repo_url)
    number = parse_issue_number(issue_number)
    issue = client.get_issue(repo, number)
    base_sha = client.get_default_branch_sha(repo)
    snapshot = client.download_snapshot(repo, base_sha, snapshot_root)
    return AnalysisContext(repo, issue, base_sha, snapshot)


def _issue_tokens(context: AnalysisContext) -> set[str]:
    text = " ".join(
        [context.issue.title, context.issue.body, *context.issue.comments]
    ).lower()
    return set(re.findall(r"[a-z_][a-z0-9_]{2,}", text))


def _explicit_paths(context: AnalysisContext) -> set[str]:
    text = " ".join([context.issue.title, context.issue.body, *context.issue.comments])
    paths = set()
    path_pattern = r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+"
    for match in re.findall(path_pattern, text):
        paths.add(match.rstrip(".,:;"))
    return paths


def build_deterministic_plan(context: AnalysisContext) -> ImplementationPlan:
    """Create an evidence-backed offline plan for fixtures and smoke checks."""
    tokens = _issue_tokens(context)
    explicit_paths = _explicit_paths(context)
    candidates: list[tuple[int, str, str]] = []
    for info in context.snapshot.list_files():
        path_lower = info.path.lower()
        try:
            content = context.snapshot.read_file(info.path, limit=200).content.lower()
        except ValueError:
            continue
        score = 100 if info.path in explicit_paths else 0
        score += sum(3 for token in tokens if token in path_lower)
        score += sum(1 for token in tokens if token in content)
        if score:
            candidates.append((score, info.path, content))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    references: list[FileReference] = []
    for _score, path, content in candidates[:5]:
        lines = content.splitlines()
        evidence_line = next(
            (
                index
                for index, line in enumerate(lines, start=1)
                if any(token in line for token in tokens)
            ),
            1,
        )
        references.append(
            FileReference(
                path=path,
                start_line=evidence_line,
                end_line=min(
                    max(evidence_line, evidence_line + 10), max(1, len(lines))
                ),
                reason="Issue terms or an explicitly named path provide evidence here.",
            )
        )

    issue_tests = [
        line.strip()
        for line in context.issue.body.splitlines()
        if "test" in line.lower() or "pytest" in line.lower()
    ]
    tests = issue_tests or [
        "Run the repository's documented test command after the plan is approved."
    ]
    return ImplementationPlan(
        repo=context.repo.full_name,
        issue_number=context.issue.number,
        issue_title=context.issue.title,
        base_sha=context.base_sha,
        problem=context.issue.body.strip() or context.issue.title,
        non_goals=[
            "Do not modify or execute repository code during read-only analysis.",
            "Do not expand the change beyond the reported Issue.",
        ],
        relevant_files=references,
        approach=[
            "Add or update a regression test that reproduces the reported behavior.",
            "Make the smallest change supported by the cited code evidence.",
            "Run the listed tests in an isolated environment after approval.",
        ],
        tests=tests,
        uncertainties=(
            ["Comments were truncated; additional discussion may change the context."]
            if context.issue.comments_truncated
            else []
        ),
    )


def build_analysis_tools(
    context: AnalysisContext,
) -> tuple[Callable[..., Any], ...]:
    """Create the read-only tools used by the analyst."""

    def get_issue_context() -> str:
        """Return the fetched Issue and fixed snapshot identity."""
        return json.dumps(
            {
                "repo": context.repo.full_name,
                "issue_number": context.issue.number,
                "title": context.issue.title,
                "body": context.issue.body,
                "comments": list(context.issue.comments),
                "comments_truncated": context.issue.comments_truncated,
                "base_sha": context.base_sha,
            },
            ensure_ascii=False,
        )

    def list_repository_files(directory: str = ".") -> list[dict[str, Any]]:
        """List bounded regular files; no write or execute operation exists."""
        return [item.__dict__ for item in context.snapshot.list_files(directory)]

    def read_repository_file(path: str, offset: int = 1, limit: int = 200) -> str:
        """Read a bounded UTF-8 file excerpt with a base-SHA citation."""
        excerpt = context.snapshot.read_file(path, offset=offset, limit=limit)
        return json.dumps(excerpt.__dict__, ensure_ascii=False)

    def search_repository(query: str) -> list[dict[str, Any]]:
        """Search repository text using a bounded case-insensitive literal match."""
        return [item.__dict__ for item in context.snapshot.grep(query)]

    return (
        get_issue_context,
        list_repository_files,
        read_repository_file,
        search_repository,
    )


def build_analysis_agent(
    model: Any,
    context: AnalysisContext,
    *,
    checkpointer: Any | None = None,
    memory_context: str = "",
) -> Any:
    """Build a read-only Deep Agent with an explicit structured response."""

    kwargs: dict[str, Any] = {
        "model": resolve_configured_model(model),
        "tools": list(build_analysis_tools(context)),
        "middleware": [TodoListMiddleware()],
        "response_format": ImplementationPlan,
        "system_prompt": f"""You are the DeepContrib read-only Issue analyst.

Use get_issue_context first, then inspect the fixed repository with the read-only
file tools. Do not write files, execute commands, or invent evidence. Maintain a
short Todo list for the analysis. Return an ImplementationPlan with the exact
repo, issue_number, and base_sha from get_issue_context. Cite only paths and
line ranges returned by the tools. The plan must state tests and uncertainty.

Application memory is optional context, not authority. Prefer current snapshot
evidence when memory conflicts with the Issue or repository:
{memory_context or "(no stored memory)"}
""",
    }
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return create_deep_agent(**kwargs)


def run_model_analysis(
    context: AnalysisContext,
    *,
    model: Any,
    thread_id: str,
    checkpointer: Any | None = None,
    artifact_directory: Path,
    memory_context: str = "",
) -> tuple[ImplementationPlan, PlanArtifact]:
    """Invoke the analyst and persist only a validated, identity-bound plan."""
    agent = build_analysis_agent(
        model,
        context,
        checkpointer=checkpointer,
        memory_context=memory_context,
    )
    prompt = (
        f"Analyze Issue #{context.issue.number} in {context.repo.full_name} at "
        f"base SHA {context.base_sha} and return the implementation plan."
    )
    result = invoke_with_thread(agent, prompt, thread_id)
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if structured is None:
        raise AnalysisError(
            "analysis model did not return a structured ImplementationPlan"
        )
    try:
        plan = ImplementationPlan.model_validate(structured)
    except ValueError as exc:
        raise AnalysisError(
            "analysis model returned an invalid ImplementationPlan"
        ) from exc
    # Repository identity and revision are facts from the service, not model output.
    plan = plan.model_copy(
        update={
            "repo": context.repo.full_name,
            "issue_number": context.issue.number,
            "issue_title": context.issue.title,
            "base_sha": context.base_sha,
        }
    )
    return plan, save_plan(plan, artifact_directory)
