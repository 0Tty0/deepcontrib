import os
import subprocess
from pathlib import Path
from typing import Any

from deepcontrib.publish import GhPublisher, PublishRequest


def test_gh_publisher_queries_existing_pr_after_create_timeout(
    tmp_path: Path,
) -> None:
    (tmp_path / "file.py").write_text("print('ok')\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "api", "repos/acme/project"]:
            return subprocess.CompletedProcess(
                command, 0, '{"default_branch":"main"}', ""
            )
        if command[:3] == [
            "gh",
            "api",
            "repos/acme/project/git/ref/heads/main",
        ]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["gh", "api", "repos/contributor/project"]:
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["git", "-C"] and "show-ref" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:2] == ["git", "-C"] and command[2].endswith(".publish-task-123"):
            if "get-url" in command:
                return subprocess.CompletedProcess(
                    command, 0, "https://github.com/contributor/project.git\n", ""
                )
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["git", "-C"] and "commit" in command:
            return subprocess.CompletedProcess(command, 0, "[main abc1234] Fix\n", "")
        if command[:2] == ["git", "-C"] and "push" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "pr", "create"]:
            raise subprocess.TimeoutExpired(command, 5)
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                command, 0, '[{"url":"https://github.com/acme/project/pull/7"}]', ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    request = PublishRequest(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=1,
        fork_owner="contributor",
        fork_repo="contributor/project",
        branch="deepcontrib/task-123",
        base_sha="a" * 40,
        base_branch="main",
        working_root=tmp_path,
        working_tree_digest=None,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="Body",
    )

    result = GhPublisher(runner=runner, timeout_seconds=5).publish(request)
    assert result.status == "completed", result.error
    assert result.pr_url == "https://github.com/acme/project/pull/7"
    assert any(command[:3] == ["gh", "pr", "list"] for command in calls)
    assert not any("remote" in command and "add" in command for command in calls)


def test_gh_publisher_disables_global_git_line_ending_conversion(
    tmp_path: Path,
) -> None:
    (tmp_path / "file.py").write_text("print('ok')\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    patch_diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "api", "repos/acme/project"]:
            return subprocess.CompletedProcess(
                command, 0, '{"default_branch":"main"}', ""
            )
        if command[:3] == [
            "gh",
            "api",
            "repos/acme/project/git/ref/heads/main",
        ]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["gh", "api", "repos/contributor/project"]:
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir()
        if command[:2] == ["git", "-C"] and "show-ref" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:2] == ["git", "-C"] and "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/acme/project/pull/1\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    request = PublishRequest(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=1,
        fork_owner="contributor",
        fork_repo="contributor/project",
        branch="deepcontrib/task-123",
        base_sha="a" * 40,
        base_branch="main",
        working_root=tmp_path,
        working_tree_digest=None,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="Body",
        patch_diff=patch_diff,
    )

    result = GhPublisher(runner=runner, timeout_seconds=5).publish(request)

    assert result.status == "completed", result.error
    git_envs = [
        kwargs["env"]
        for command, kwargs in calls
        if command and command[0] == "git"
    ]
    assert git_envs
    assert all(env["GIT_CONFIG_NOSYSTEM"] == "1" for env in git_envs)
    assert all(env["GIT_CONFIG_GLOBAL"] == os.devnull for env in git_envs)
    apply_call = next(
        kwargs
        for command, kwargs in calls
        if command[:2] == ["git", "-C"] and "apply" in command
    )
    assert apply_call["text"] is False
    assert apply_call["input"] == patch_diff.encode("utf-8")
    commit_env = next(
        kwargs["env"]
        for command, kwargs in calls
        if command[:2] == ["git", "-C"] and "commit" in command
    )
    assert commit_env["GIT_AUTHOR_NAME"] == "DeepContrib Agent"
    assert (
        commit_env["GIT_AUTHOR_EMAIL"]
        == "deepcontrib-agent@users.noreply.github.com"
    )
    assert commit_env["GIT_COMMITTER_NAME"] == "DeepContrib Agent"
    assert (
        commit_env["GIT_COMMITTER_EMAIL"]
        == "deepcontrib-agent@users.noreply.github.com"
    )
    credential_call = next(
        command
        for command, _kwargs in calls
        if command[:2] == ["git", "-C"] and "config" in command
    )
    assert credential_call[-2:] == [
        "credential.helper",
        "!gh auth git-credential",
    ]
    create_call = next(
        command
        for command, _kwargs in calls
        if command[:3] == ["gh", "pr", "create"]
    )
    assert "--json" not in create_call
    assert create_call[create_call.index("--body") + 1].endswith("Fixes #1")


def test_gh_publisher_reuses_existing_local_branch_after_push(tmp_path: Path) -> None:
    publish_root = tmp_path / ".publish-task-123"
    (publish_root / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == [
            "gh",
            "api",
            "repos/acme/project/git/ref/heads/main",
        ]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["gh", "api", "repos/contributor/project"]:
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if command[:2] == ["git", "-C"] and "show-ref" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["git", "-C"] and "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/acme/project/pull/1\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    request = PublishRequest(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=1,
        fork_owner="contributor",
        fork_repo="contributor/project",
        branch="deepcontrib/task-123",
        base_sha="a" * 40,
        base_branch="main",
        working_root=tmp_path / "working",
        working_tree_digest=None,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="Body",
        patch_diff="--- a/file.py\n+++ b/file.py\n",
    )
    request.working_root.mkdir()

    result = GhPublisher(runner=runner, timeout_seconds=5).publish(request)

    assert result.status == "completed"
    assert not any("apply" in command for command in calls)
    assert not any("commit" in command for command in calls)
    assert any("checkout" in command and request.branch in command for command in calls)


def test_gh_publisher_blocks_when_upstream_base_branch_moved(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == [
            "gh",
            "api",
            "repos/acme/project/git/ref/heads/main",
        ]:
            return subprocess.CompletedProcess(command, 0, "b" * 40 + "\n", "")
        raise AssertionError(f"unexpected command after base check: {command}")

    request = PublishRequest(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=1,
        fork_owner="contributor",
        fork_repo="contributor/project",
        branch="deepcontrib/task-123",
        base_sha="a" * 40,
        base_branch="main",
        working_root=tmp_path,
        working_tree_digest=None,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="Body",
    )

    result = GhPublisher(runner=runner, timeout_seconds=5).publish(request)

    assert result.status == "failed"
    assert result.error is not None
    assert "base branch changed" in result.error
    assert not any(
        command[:3] == ["gh", "api", "repos/acme/project/forks"] for command in calls
    )
