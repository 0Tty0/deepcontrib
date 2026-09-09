from pathlib import Path

import pytest

from deepcontrib.publish import PublishError, PublishRequest, build_publish_request


def test_publish_request_has_safe_branch_and_explicit_side_effects(
    tmp_path: Path,
) -> None:
    request = build_publish_request(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=42,
        fork_owner="contributor",
        base_sha="a" * 40,
        working_root=tmp_path,
        working_tree_digest="b" * 64,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="The regression test now passes.",
        base_branch="main",
    )

    assert isinstance(request, PublishRequest)
    assert request.branch == "deepcontrib/task-123"
    assert request.fork_repo == "contributor/project"
    assert request.issue_number == 42
    assert request.body.endswith("Fixes #42")
    assert request.as_dict()["issue_number"] == 42
    assert request.side_effects == (
        "create_fork_if_missing",
        "commit",
        "push_branch",
        "create_draft_pr",
    )


@pytest.mark.parametrize("owner", ["", "bad owner", "-owner", "owner/"])
def test_publish_request_rejects_unsafe_fork_owner(tmp_path: Path, owner: str) -> None:
    with pytest.raises(PublishError, match="fork owner"):
        build_publish_request(
            task_id="task-123",
            upstream_repo="acme/project",
            issue_number=1,
            fork_owner=owner,
            base_sha="a" * 40,
            working_root=tmp_path,
            working_tree_digest="b" * 64,
            patch_sha256="c" * 64,
            test_report_sha256="d" * 64,
            review_report_sha256="e" * 64,
            title="Fix parser",
            body="Body",
            base_branch="main",
        )


def test_publish_request_rejects_missing_workspace_and_invalid_hash(
    tmp_path: Path,
) -> None:
    with pytest.raises(PublishError, match="working"):
        build_publish_request(
            task_id="task-123",
            upstream_repo="acme/project",
            issue_number=1,
            fork_owner="contributor",
            base_sha="a" * 40,
            working_root=tmp_path / "missing",
            working_tree_digest="b" * 64,
            patch_sha256="c" * 64,
            test_report_sha256="d" * 64,
            review_report_sha256="e" * 64,
            title="Fix parser",
            body="Body",
            base_branch="main",
        )


@pytest.mark.parametrize("issue_number", [0, -1, True])
def test_publish_request_rejects_invalid_issue_number(
    tmp_path: Path, issue_number: int
) -> None:
    with pytest.raises(PublishError, match="issue number"):
        build_publish_request(
            task_id="task-123",
            upstream_repo="acme/project",
            issue_number=issue_number,
            fork_owner="contributor",
            base_sha="a" * 40,
            working_root=tmp_path,
            working_tree_digest="b" * 64,
            patch_sha256="c" * 64,
            test_report_sha256="d" * 64,
            review_report_sha256="e" * 64,
            title="Fix parser",
            body="Body",
            base_branch="main",
        )


def test_publish_request_does_not_duplicate_issue_closer(tmp_path: Path) -> None:
    request = build_publish_request(
        task_id="task-123",
        upstream_repo="acme/project",
        issue_number=42,
        fork_owner="contributor",
        base_sha="a" * 40,
        working_root=tmp_path,
        working_tree_digest="b" * 64,
        patch_sha256="c" * 64,
        test_report_sha256="d" * 64,
        review_report_sha256="e" * 64,
        title="Fix parser",
        body="Body\n\nCloses #42",
        base_branch="main",
    )

    assert request.body == "Body\n\nCloses #42"
