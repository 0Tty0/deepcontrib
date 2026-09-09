import json
import subprocess
from typing import Any

import pytest

from deepcontrib.github import GitHubClient, GitHubError
from deepcontrib.repository import parse_issue_number, parse_repository_url


class _FakeRunner:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        endpoint = args[-1]
        response = self.responses.get(endpoint)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return subprocess.CompletedProcess(args, 1, "", "missing fake response")
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "")


def test_github_client_reads_issue_comments_and_truncates() -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    issue = parse_issue_number("7")
    runner = _FakeRunner(
        {
            "repos/acme/project/issues/7": {
                "title": "Fix parser",
                "body": "The parser fails.",
                "state": "open",
                "html_url": "https://github.com/acme/project/issues/7",
            },
            "repos/acme/project/issues/7/comments?per_page=3": [
                {"body": "first"},
                {"body": "second"},
                {"body": "third"},
            ],
        }
    )
    client = GitHubClient(runner=runner, max_comments=2)

    snapshot = client.get_issue(repo, issue)

    assert snapshot.title == "Fix parser"
    assert snapshot.comments == ("first", "second")
    assert snapshot.comments_truncated is True
    assert all(call[1]["shell"] is False for call in runner.calls)
    assert all("--hostname" in call[0] for call in runner.calls)


def test_github_client_reads_default_branch_sha() -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    runner = _FakeRunner(
        {
            "repos/acme/project": {"default_branch": "main"},
            "repos/acme/project/commits/main": {"sha": "e" * 40},
        }
    )

    result = GitHubClient(runner=runner).get_default_branch_sha(repo)

    assert result == "e" * 40


def test_github_client_translates_command_failures() -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    runner = _FakeRunner({})

    with pytest.raises(GitHubError, match="GitHub CLI failed"):
        GitHubClient(runner=runner).get_issue(repo, 1)


def test_github_client_rejects_invalid_sha() -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    runner = _FakeRunner(
        {
            "repos/acme/project": {"default_branch": "main"},
            "repos/acme/project/commits/main": {"sha": "bad"},
        }
    )

    with pytest.raises(GitHubError, match="SHA"):
        GitHubClient(runner=runner).get_default_branch_sha(repo)


def test_github_client_rejects_invalid_archive_limit() -> None:
    with pytest.raises(GitHubError, match="max_archive_bytes"):
        GitHubClient(max_archive_bytes=0)


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("gh", 1)]
)
def test_github_client_reports_missing_or_timed_out_cli(error: Exception) -> None:
    repo = parse_repository_url("https://github.com/acme/project")

    def failing_runner(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    with pytest.raises(GitHubError, match="(not installed|timed out)"):
        GitHubClient(runner=failing_runner).get_issue(repo, 1)


def test_github_client_reports_invalid_json_and_invalid_comments() -> None:
    repo = parse_repository_url("https://github.com/acme/project")

    class InvalidJsonRunner(_FakeRunner):
        def __call__(self, args: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(args, 0, "not-json", "")

    with pytest.raises(GitHubError, match="invalid JSON"):
        GitHubClient(runner=InvalidJsonRunner({})).get_issue(repo, 1)

    runner = _FakeRunner(
        {
            "repos/acme/project/issues/1": {"title": "x"},
            "repos/acme/project/issues/1/comments?per_page=21": {"bad": True},
        }
    )
    with pytest.raises(GitHubError, match="comments"):
        GitHubClient(runner=runner).get_issue(repo, 1)


def test_github_client_reports_missing_branch() -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    runner = _FakeRunner({"repos/acme/project": {"default_branch": ""}})

    with pytest.raises(GitHubError, match="default branch"):
        GitHubClient(runner=runner).get_default_branch_sha(repo)


def test_github_client_downloads_and_extracts_snapshot(tmp_path: Any) -> None:
    import io
    import tarfile

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"print('ok')\n"
        info = tarfile.TarInfo("acme-project-sha/main.py")
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))

    class ArchiveRunner:
        def __call__(
            self, args: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            assert "--output" not in args
            output = kwargs["stdout"]
            output.write(archive.read_bytes())
            return subprocess.CompletedProcess(args, 0, b"", b"")

    repo = parse_repository_url("https://github.com/acme/project")
    snapshot = GitHubClient(runner=ArchiveRunner()).download_snapshot(
        repo,
        "a" * 40,
        tmp_path / "snapshot",
    )

    assert snapshot.read_file("main.py").content == "print('ok')"
    assert not list(tmp_path.glob("deepcontrib-snapshot-*.tar.gz"))


def test_github_client_download_rejects_invalid_destination_and_sha(
    tmp_path: Any,
) -> None:
    repo = parse_repository_url("https://github.com/acme/project")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "existing").write_text("x", encoding="utf-8")

    with pytest.raises(GitHubError, match="SHA"):
        GitHubClient().download_snapshot(repo, "bad", tmp_path / "other")
    with pytest.raises(GitHubError, match="empty"):
        GitHubClient().download_snapshot(repo, "a" * 40, destination)

    file_destination = tmp_path / "file-destination"
    file_destination.write_text("x", encoding="utf-8")
    with pytest.raises(GitHubError, match="directory"):
        GitHubClient().download_snapshot(repo, "a" * 40, file_destination)
