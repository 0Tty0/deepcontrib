"""Deterministic review gate for approved patches and test evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from deepcontrib.patches import PatchSummary
from deepcontrib.plan import ImplementationPlan
from deepcontrib.subagents import ExplorerResult
from deepcontrib.test_runner import VerificationResult

ReviewSeverity = Literal["blocking", "warning"]


@dataclass(frozen=True)
class ReviewIssue:
    code: str
    message: str
    severity: ReviewSeverity = "blocking"
    path: str | None = None
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True)
class ReviewResult:
    status: Literal["passed", "failed"]
    summary: str
    issues: tuple[ReviewIssue, ...]
    explorer: ExplorerResult
    tool_names: tuple[str, ...] = ("read_artifact", "read_file")
    can_write: bool = False
    can_approve: bool = False
    can_publish: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "issues": [item.as_dict() for item in self.issues],
            "explorer": self.explorer.as_dict(),
            "tool_names": list(self.tool_names),
            "can_write": self.can_write,
            "can_approve": self.can_approve,
            "can_publish": self.can_publish,
        }


class Reviewer:
    """Review Issue intent, approved scope, and concrete test evidence."""

    tool_names = ("read_artifact", "read_file")
    can_write = False
    can_approve = False
    can_publish = False

    def review(
        self,
        *,
        issue_text: str,
        plan: ImplementationPlan,
        patch_summary: PatchSummary,
        verification: VerificationResult,
        explorer: ExplorerResult,
        diff: str,
    ) -> ReviewResult:
        del diff  # The parsed summary and immutable artifact are the source of truth.
        issues: list[ReviewIssue] = []
        if verification.status != "red_green":
            issues.append(
                ReviewIssue(
                    "verification_not_red_green",
                    "Review requires a reproducible baseline failure and a "
                    "passing patched copy.",
                )
            )
        if plan.base_sha.lower() != explorer.base_sha.lower():
            issues.append(
                ReviewIssue(
                    "base_sha_mismatch",
                    "Explorer evidence does not match the Plan base SHA.",
                )
            )
        if (
            verification.baseline.worktree_digest_before
            != verification.baseline.worktree_digest_after
        ):
            issues.append(
                ReviewIssue(
                    "baseline_mutated",
                    "The baseline worktree changed while tests ran.",
                )
            )
        if verification.patched is None and verification.status == "red_green":
            issues.append(
                ReviewIssue(
                    "patched_evidence_missing",
                    "A red/green result must include patched test evidence.",
                )
            )

        approved_paths = {item.path for item in plan.relevant_files}
        out_of_scope = sorted(set(patch_summary.changed_paths) - approved_paths)
        if out_of_scope:
            issues.append(
                ReviewIssue(
                    "patch_outside_plan",
                    "Patch changes paths outside the approved Plan scope: "
                    + ", ".join(out_of_scope),
                    path=out_of_scope[0],
                )
            )

        asks_for_regression = bool(
            re.search(
                r"\b(?:add|update|include|write)\b[^.\n]{0,80}\b(?:regression\s+)?test\b",
                issue_text,
                flags=re.IGNORECASE,
            )
        )
        changed_test = any(
            "test" in path.casefold() for path in patch_summary.changed_paths
        )
        expected_test = next(
            (
                item.path
                for item in plan.relevant_files
                if "test" in item.path.casefold()
            ),
            None,
        )
        if asks_for_regression and not changed_test:
            issues.append(
                ReviewIssue(
                    "missing_regression_test",
                    "The Issue requests a regression test, but the Patch does "
                    "not change a test file; the reported edge case remains "
                    "unverified.",
                    path=expected_test,
                    line=1 if expected_test else None,
                )
            )

        blocking = tuple(item for item in issues if item.severity == "blocking")
        if blocking:
            return ReviewResult(
                status="failed",
                summary=f"review failed with {len(blocking)} blocking issue(s)",
                issues=tuple(issues),
                explorer=explorer,
            )
        return ReviewResult(
            status="passed",
            summary="review passed: scope, evidence, and requested tests are satisfied",
            issues=tuple(issues),
            explorer=explorer,
        )
