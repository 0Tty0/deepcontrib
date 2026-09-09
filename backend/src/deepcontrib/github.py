"""Read-only GitHub access through the installed GitHub CLI."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from deepcontrib.repository import RepositoryRef, RepositorySnapshot


class GitHubError(RuntimeError):
    """Raised when GitHub data cannot be read safely."""


@dataclass(frozen=True)
class IssueSnapshot:
    number: int
    title: str
    body: str
    state: str
    html_url: str
    comments: tuple[str, ...]
    comments_truncated: bool


Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass
class GitHubClient:
    """Small structured wrapper around ``gh api`` with no shell execution."""

    runner: Runner = subprocess.run
    max_comments: int = 20
    timeout_seconds: int = 30
    max_archive_bytes: int = 50 * 1024 * 1024
    _sha_pattern: re.Pattern[str] = field(
        default=re.compile(r"^[0-9a-fA-F]{40}$"), init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.max_comments < 0:
            raise GitHubError("max_comments must not be negative")
        if self.timeout_seconds <= 0:
            raise GitHubError("timeout_seconds must be positive")
        if self.max_archive_bytes <= 0:
            raise GitHubError("max_archive_bytes must be positive")

    def _run_json(self, endpoint: str) -> Any:
        args = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--header",
            "Accept: application/vnd.github+json",
            endpoint,
        ]
        try:
            completed = self.runner(
                args,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GitHubError("GitHub CLI (gh) is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError("GitHub CLI request timed out") from exc
        if completed.returncode != 0:
            raise GitHubError("GitHub CLI failed while reading repository data")
        try:
            return json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GitHubError("GitHub CLI returned invalid JSON") from exc

    def get_issue(self, repo: RepositoryRef, number: int) -> IssueSnapshot:
        issue_endpoint = f"repos/{repo.full_name}/issues/{number}"
        raw_issue = self._run_json(issue_endpoint)
        if not isinstance(raw_issue, dict) or "pull_request" in raw_issue:
            raise GitHubError("the requested resource is not a GitHub Issue")
        comments_endpoint = (
            f"repos/{repo.full_name}/issues/{number}/comments"
            f"?per_page={min(self.max_comments + 1, 100)}"
        )
        raw_comments = self._run_json(comments_endpoint)
        if not isinstance(raw_comments, list):
            raise GitHubError("GitHub returned an invalid comments response")
        comments = tuple(
            str(item.get("body", "")) for item in raw_comments if isinstance(item, dict)
        )
        return IssueSnapshot(
            number=number,
            title=str(raw_issue.get("title", "")).strip(),
            body=str(raw_issue.get("body") or ""),
            state=str(raw_issue.get("state", "unknown")),
            html_url=str(raw_issue.get("html_url", "")),
            comments=comments[: self.max_comments],
            comments_truncated=len(comments) > self.max_comments,
        )

    def get_default_branch_sha(self, repo: RepositoryRef) -> str:
        metadata = self._run_json(f"repos/{repo.full_name}")
        if not isinstance(metadata, dict):
            raise GitHubError("GitHub returned invalid repository metadata")
        branch = str(metadata.get("default_branch", "")).strip()
        if not branch:
            raise GitHubError("GitHub repository has no default branch")
        commit = self._run_json(
            f"repos/{repo.full_name}/commits/{quote(branch, safe='')}"
        )
        sha = str(commit.get("sha", "")) if isinstance(commit, dict) else ""
        if not self._sha_pattern.fullmatch(sha):
            raise GitHubError("GitHub returned an invalid base commit SHA")
        return sha.lower()

    def download_snapshot(
        self,
        repo: RepositoryRef,
        base_sha: str,
        destination: Path,
    ) -> RepositorySnapshot:
        """Download and safely extract one immutable base-SHA tarball."""
        if not self._sha_pattern.fullmatch(base_sha):
            raise GitHubError("base SHA must be a 40-character SHA")
        destination = Path(destination)
        if destination.exists():
            if not destination.is_dir():
                raise GitHubError("snapshot destination must be a directory")
            if any(destination.iterdir()):
                raise GitHubError("snapshot destination must be empty")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            prefix="deepcontrib-snapshot-",
            suffix=".tar.gz",
            dir=destination.parent,
            delete=False,
        )
        archive = Path(temporary.name)
        temporary.close()
        endpoint = f"repos/{repo.full_name}/tarball/{base_sha}"
        args = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            endpoint,
        ]
        try:
            with archive.open("wb") as output:
                try:
                    completed = self.runner(
                        args,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        text=False,
                        check=False,
                        shell=False,
                        timeout=self.timeout_seconds,
                    )
                except FileNotFoundError as exc:
                    raise GitHubError("GitHub CLI (gh) is not installed") from exc
                except subprocess.TimeoutExpired as exc:
                    raise GitHubError("GitHub CLI request timed out") from exc
            if completed.returncode != 0:
                raise GitHubError("GitHub CLI failed while downloading repository data")
            if not archive.is_file() or archive.stat().st_size > self.max_archive_bytes:
                raise GitHubError("repository archive is missing or too large")
            return RepositorySnapshot.extract_tarball(
                archive,
                destination,
                base_sha,
            )
        finally:
            archive.unlink(missing_ok=True)
