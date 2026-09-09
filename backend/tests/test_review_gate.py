from deepcontrib.patches import PatchSummary
from deepcontrib.plan import FileReference, ImplementationPlan
from deepcontrib.review import Reviewer
from deepcontrib.subagents import ExplorerResult
from deepcontrib.test_runner import TestResult, VerificationResult


def _verification(status: str = "red_green") -> VerificationResult:
    baseline = TestResult(
        status="failed",
        command=("docker", "run"),
        exit_code=1,
        stdout="",
        stderr="failure",
        duration_seconds=0.1,
        timed_out=False,
        worktree_digest_before="a",
        worktree_digest_after="a",
    )
    patched = TestResult(
        status="passed",
        command=baseline.command,
        exit_code=0,
        stdout="passed",
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
        worktree_digest_before="b",
        worktree_digest_after="b",
    )
    return VerificationResult(
        status=status,  # type: ignore[arg-type]
        baseline=baseline,
        patched=patched,
        message="verification",
    )


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        repo="acme/project",
        issue_number=1,
        issue_title="Handle invalid input",
        base_sha="a" * 40,
        problem="Add a regression test for invalid input handling.",
        relevant_files=[
            FileReference(
                path="src/parser.py",
                start_line=1,
                end_line=10,
                reason="parser implementation",
            ),
            FileReference(
                path="tests/test_parser.py",
                start_line=1,
                end_line=20,
                reason="regression test",
            ),
        ],
        tests=["pytest -q"],
    )


def _explorer() -> ExplorerResult:
    return ExplorerResult(
        base_sha="a" * 40,
        files=("src/parser.py", "tests/test_parser.py"),
        symbols=(),
        evidence=(),
        uncertainties=(),
    )


def test_reviewer_blocks_a_patch_that_omits_requested_regression_test() -> None:
    result = Reviewer().review(
        issue_text="Add a regression test for invalid input handling.",
        plan=_plan(),
        patch_summary=PatchSummary(("src/parser.py",), 1, 1),
        verification=_verification(),
        explorer=_explorer(),
        diff="--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n",
    )

    assert result.status == "failed"
    assert any(issue.code == "missing_regression_test" for issue in result.issues)
    assert result.issues[0].path == "tests/test_parser.py"


def test_reviewer_passes_a_scoped_red_green_patch_with_test_evidence() -> None:
    result = Reviewer().review(
        issue_text="Add a regression test for invalid input handling.",
        plan=_plan(),
        patch_summary=PatchSummary(("src/parser.py", "tests/test_parser.py"), 2, 1),
        verification=_verification(),
        explorer=_explorer(),
        diff=(
            "--- a/src/parser.py\n+++ b/src/parser.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "--- a/tests/test_parser.py\n+++ b/tests/test_parser.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        ),
    )

    assert result.status == "passed"
    assert result.issues == ()


def test_reviewer_blocks_non_red_green_results() -> None:
    result = Reviewer().review(
        issue_text="Fix parser.",
        plan=_plan(),
        patch_summary=PatchSummary(("src/parser.py",), 1, 1),
        verification=_verification("baseline_passed"),
        explorer=_explorer(),
        diff="--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n",
    )

    assert result.status == "failed"
    assert any(issue.code == "verification_not_red_green" for issue in result.issues)
