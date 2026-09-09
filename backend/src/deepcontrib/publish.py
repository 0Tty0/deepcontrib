"""Approved, structured Draft PR publishing with recovery-aware GitHub calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from deepcontrib.patches import working_tree_digest
from deepcontrib.repository import parse_repository_url


class PublishError(RuntimeError):
    """Raised when a publish request is invalid or a step cannot complete."""


PublishStatus = Literal["completed", "failed", "unsupported_environment"]


@dataclass(frozen=True)
class PublishRequest:
    task_id: str
    upstream_repo: str
    issue_number: int
    fork_owner: str
    fork_repo: str
    branch: str
    base_sha: str
    base_branch: str
    working_root: Path
    working_tree_digest: str | None
    patch_sha256: str
    test_report_sha256: str
    review_report_sha256: str
    title: str
    body: str
    patch_diff: str = ""

    @property
    def side_effects(self) -> tuple[str, ...]:
        return ("create_fork_if_missing", "commit", "push_branch", "create_draft_pr")

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "upstream_repo": self.upstream_repo,
            "issue_number": self.issue_number,
            "fork_owner": self.fork_owner,
            "fork_repo": self.fork_repo,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "working_tree_digest": self.working_tree_digest,
            "patch_sha256": self.patch_sha256,
            "test_report_sha256": self.test_report_sha256,
            "review_report_sha256": self.review_report_sha256,
            "title": self.title,
            "body": self.body,
            "side_effects": list(self.side_effects),
        }


@dataclass(frozen=True)
class PublishStep:
    name: str
    status: Literal["completed", "failed", "skipped"]
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    branch: str
    commit_sha: str | None
    pr_url: str | None
    error: str | None
    steps: tuple[PublishStep, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pr_url": self.pr_url,
            "error": self.error,
            "steps": [item.as_dict() for item in self.steps],
        }


class Publisher(Protocol):
    def publish(self, request: PublishRequest) -> PublishResult: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,119}$")
_COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "DeepContrib Agent",
    "GIT_AUTHOR_EMAIL": "deepcontrib-agent@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "DeepContrib Agent",
    "GIT_COMMITTER_EMAIL": "deepcontrib-agent@users.noreply.github.com",
}


def _linked_issue_body(body: str, issue_number: int) -> str:
    """Return a PR body that GitHub associates with the source Issue."""
    normalized = body.strip()
    if re.search(
        rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#{issue_number}\b",
        normalized,
    ):
        return normalized
    return f"{normalized}\n\nFixes #{issue_number}"


def build_publish_request(
    *,
    task_id: str,
    upstream_repo: str,
    issue_number: int,
    fork_owner: str,
    base_sha: str,
    working_root: Path,
    working_tree_digest: str,
    patch_sha256: str,
    test_report_sha256: str,
    review_report_sha256: str,
    title: str,
    body: str,
    base_branch: str,
    patch_diff: str = "",
) -> PublishRequest:
    """Build the exact publish card that the user will approve."""
    try:
        upstream = parse_repository_url(f"https://github.com/{upstream_repo}")
    except ValueError as exc:
        raise PublishError("upstream repository is invalid") from exc
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
    ):
        raise PublishError("issue number is invalid")
    if not _COMPONENT.fullmatch(fork_owner):
        raise PublishError("fork owner is invalid")
    if not _SHA.fullmatch(base_sha):
        raise PublishError("base SHA is invalid")
    for label, value in (
        ("working tree digest", working_tree_digest),
        ("Patch hash", patch_sha256),
        ("test report hash", test_report_sha256),
        ("review report hash", review_report_sha256),
    ):
        if not _HASH.fullmatch(value):
            raise PublishError(f"{label} is invalid")
    root = Path(working_root).resolve()
    if not root.is_dir():
        raise PublishError("working directory does not exist")
    if not _COMPONENT.fullmatch(base_branch):
        raise PublishError("base branch is invalid")
    if not title.strip() or len(title) > 200:
        raise PublishError("publish title must be non-empty and at most 200 characters")
    normalized_body = _linked_issue_body(body, issue_number)
    if not body.strip() or len(normalized_body) > 20_000:
        raise PublishError(
            "publish body must be non-empty and at most 20000 characters"
        )
    branch = f"deepcontrib/{task_id}"
    if not _BRANCH.fullmatch(branch):
        raise PublishError("publish branch is invalid")
    return PublishRequest(
        task_id=task_id,
        upstream_repo=upstream.full_name,
        issue_number=issue_number,
        fork_owner=fork_owner,
        fork_repo=f"{fork_owner}/{upstream.name}",
        branch=branch,
        base_sha=base_sha.lower(),
        base_branch=base_branch,
        working_root=root,
        working_tree_digest=working_tree_digest,
        patch_sha256=patch_sha256,
        test_report_sha256=test_report_sha256,
        review_report_sha256=review_report_sha256,
        title=title.strip(),
        body=normalized_body,
        patch_diff=patch_diff,
    )


class GhPublisher:
    """Use only structured ``gh`` and ``git`` argument lists after approval."""

    def __init__(
        self, *, runner: Runner = subprocess.run, timeout_seconds: int = 60
    ) -> None:
        if timeout_seconds <= 0:
            raise PublishError("publish timeout must be positive")
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def publish(self, request: PublishRequest) -> PublishResult:
        _validate_request(request)
        steps: list[PublishStep] = []
        try:
            self._run(["gh", "auth", "status", "--hostname", "github.com"])
            steps.append(
                PublishStep("auth", "completed", "GitHub CLI credentials available")
            )
            self._assert_base_branch_unchanged(request)
            fork = self._run(
                ["gh", "api", f"repos/{request.fork_repo}"], allow_failure=True
            )
            if fork.returncode != 0:
                self._run(
                    [
                        "gh",
                        "api",
                        "--method",
                        "POST",
                        f"repos/{request.upstream_repo}/forks",
                    ]
                )
                steps.append(PublishStep("fork", "completed", "created user fork"))
            else:
                steps.append(
                    PublishStep("fork", "completed", "user fork already exists")
                )

            publish_root = request.working_root.parent / f".publish-{request.task_id}"
            if publish_root.exists():
                if publish_root.is_symlink() or not publish_root.is_dir():
                    raise PublishError("publish workspace is not a directory")
            else:
                self._run(
                    [
                        "git",
                        "clone",
                        "--filter=blob:none",
                        "--no-checkout",
                        f"https://github.com/{request.upstream_repo}.git",
                        str(publish_root),
                    ]
                )
            branch_ref = self._run(
                [
                    "git",
                    "-C",
                    str(publish_root),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{request.branch}",
                ],
                allow_failure=True,
            )
            reused_branch = branch_ref.returncode == 0
            if reused_branch:
                self._run(
                    [
                        "git",
                        "-C",
                        str(publish_root),
                        "checkout",
                        request.branch,
                    ]
                )
                status = self._run(
                    [
                        "git",
                        "-C",
                        str(publish_root),
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ]
                )
                if status.stdout.strip():
                    raise PublishError("publish workspace has uncommitted changes")
            else:
                self._run(
                    [
                        "git",
                        "-C",
                        str(publish_root),
                        "checkout",
                        "--detach",
                        request.base_sha,
                    ]
                )
                self._run(
                    ["git", "-C", str(publish_root), "checkout", "-b", request.branch]
                )
                if request.patch_diff:
                    self._run(
                        [
                            "git",
                            "-C",
                            str(publish_root),
                            "apply",
                            "--whitespace=error",
                            "-",
                        ],
                        input=request.patch_diff,
                    )
            if request.working_tree_digest:
                actual = working_tree_digest(publish_root, ignored_directories={".git"})
                if actual != request.working_tree_digest:
                    raise PublishError(
                        "publish workspace does not match the approved working tree"
                    )
            steps.append(PublishStep("branch", "completed", request.branch))

            if not reused_branch:
                self._run(["git", "-C", str(publish_root), "add", "--all", "--"])
                self._run(
                    ["git", "-C", str(publish_root), "commit", "-m", request.title]
                )
            commit = self._run(["git", "-C", str(publish_root), "rev-parse", "HEAD"])
            commit_sha = _first_line(commit.stdout)
            if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha):
                raise PublishError("git did not return a commit SHA")
            steps.append(PublishStep("commit", "completed", commit_sha))

            fork_remote = f"https://github.com/{request.fork_repo}.git"
            remote = self._run(
                ["git", "-C", str(publish_root), "remote", "get-url", "fork"],
                allow_failure=True,
            )
            configured_remote = _first_line(remote.stdout)
            if remote.returncode == 0 and configured_remote:
                if configured_remote.rstrip("/").removesuffix(
                    ".git"
                ) != fork_remote.removesuffix(".git"):
                    raise PublishError(
                        "publish workspace has an unexpected fork remote"
                    )
            else:
                self._run(
                    [
                        "git",
                        "-C",
                        str(publish_root),
                        "remote",
                        "add",
                        "fork",
                        fork_remote,
                    ]
                )
            self._run(
                [
                    "git",
                    "-C",
                    str(publish_root),
                    "config",
                    "--local",
                    "credential.helper",
                    "!gh auth git-credential",
                ]
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(publish_root),
                    "push",
                    "--set-upstream",
                    "fork",
                    request.branch,
                ]
            )
            steps.append(
                PublishStep(
                    "push", "completed", f"{request.fork_repo}:{request.branch}"
                )
            )

            existing = self._find_open_pr(request)
            if existing is not None:
                steps.append(
                    PublishStep("draft_pr", "completed", "recovered existing Draft PR")
                )
                return PublishResult(
                    "completed",
                    request.branch,
                    commit_sha,
                    existing,
                    None,
                    tuple(steps),
                )
            try:
                created = self._run(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--repo",
                        request.upstream_repo,
                        "--head",
                        f"{request.fork_owner}:{request.branch}",
                        "--base",
                        request.base_branch,
                        "--title",
                        request.title,
                        "--body",
                        _linked_issue_body(request.body, request.issue_number),
                        "--draft",
                    ]
                )
                pr_url = _pr_url(created.stdout)
            except subprocess.TimeoutExpired:
                recovered = self._find_open_pr(request)
                if recovered is None:
                    raise PublishError(
                        "Draft PR creation timed out and no existing PR was found"
                    )
                pr_url = recovered
            steps.append(PublishStep("draft_pr", "completed", pr_url))
            return PublishResult(
                "completed", request.branch, commit_sha, pr_url, None, tuple(steps)
            )
        except FileNotFoundError:
            return PublishResult(
                "unsupported_environment",
                request.branch,
                None,
                None,
                "GitHub CLI or Git is not installed",
                tuple(steps),
            )
        except (
            PublishError,
            subprocess.CalledProcessError,
            OSError,
            subprocess.TimeoutExpired,
        ) as exc:
            detail = str(exc).strip()[:500] or "publish step failed"
            steps.append(PublishStep("failed", "failed", detail))
            return PublishResult(
                "failed", request.branch, None, None, detail, tuple(steps)
            )

    def _assert_base_branch_unchanged(self, request: PublishRequest) -> None:
        """Refuse to publish when the upstream branch moved since analysis."""
        result = self._run(
            [
                "gh",
                "api",
                f"repos/{request.upstream_repo}/git/ref/heads/{request.base_branch}",
                "--jq",
                ".object.sha",
            ]
        )
        observed = _first_line(result.stdout)
        if not _SHA.fullmatch(observed):
            raise PublishError("could not verify the upstream base branch")
        if observed.lower() != request.base_sha.lower():
            raise PublishError(
                "upstream base branch changed; re-plan and re-approve the Patch"
            )

    def _run(
        self,
        command: list[str],
        *,
        allow_failure: bool = False,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        try:
            supplied_input = kwargs.pop("input", None)
            binary_input = bool(
                command
                and command[0] == "git"
                and "apply" in command
                and supplied_input is not None
            )
            if supplied_input is not None:
                kwargs["input"] = (
                    supplied_input.encode("utf-8")
                    if binary_input and isinstance(supplied_input, str)
                    else supplied_input
                )
            if command and command[0] == "git":
                environment = dict(os.environ)
                supplied_environment = kwargs.pop("env", None)
                if supplied_environment is not None:
                    environment.update(supplied_environment)
                environment.update(
                    {
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull,
                    }
                )
                if "commit" in command:
                    environment.update(_COMMIT_IDENTITY)
                kwargs["env"] = environment
            completed = self.runner(
                command,
                capture_output=True,
                text=not binary_input,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
                **kwargs,
            )
            if binary_input:
                stdout = completed.stdout
                stderr = completed.stderr
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                completed = subprocess.CompletedProcess(
                    command, completed.returncode, stdout, stderr
                )
        except subprocess.TimeoutExpired:
            raise
        if completed.returncode != 0 and not allow_failure:
            detail = (completed.stderr or "").strip().replace("\n", " ")[:500]
            raise PublishError(f"publish command failed: {detail or command[0]}")
        return completed

    def _find_open_pr(self, request: PublishRequest) -> str | None:
        result = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                request.upstream_repo,
                "--head",
                f"{request.fork_owner}:{request.branch}",
                "--state",
                "open",
                "--json",
                "url",
            ]
        )
        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PublishError("GitHub CLI returned invalid pull request JSON") from exc
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        url = first.get("url") if isinstance(first, dict) else None
        return (
            str(url)
            if isinstance(url, str) and url.startswith("https://github.com/")
            else None
        )


def _validate_request(request: PublishRequest) -> None:
    if not _COMPONENT.fullmatch(request.task_id):
        raise PublishError("task id is invalid")
    if (
        isinstance(request.issue_number, bool)
        or not isinstance(request.issue_number, int)
        or request.issue_number <= 0
    ):
        raise PublishError("issue number is invalid")
    if not _SHA.fullmatch(request.base_sha):
        raise PublishError("base SHA is invalid")
    if not _COMPONENT.fullmatch(request.fork_owner):
        raise PublishError("fork owner is invalid")
    if not _BRANCH.fullmatch(request.branch) or request.branch.startswith("-"):
        raise PublishError("publish branch is invalid")
    if not request.working_root.is_dir():
        raise PublishError("working directory does not exist")


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _pr_url(value: str) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("url"), str):
        url = cast(str, payload["url"])
    else:
        url = _first_line(value)
    if not url.startswith("https://github.com/"):
        raise PublishError("GitHub CLI did not return a pull request URL")
    return url
