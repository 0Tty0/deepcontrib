from pathlib import Path

import pytest

from deepcontrib.plan import (
    FileReference,
    ImplementationPlan,
    PlanError,
    load_plan,
    save_plan,
)


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        repo="acme/project",
        issue_number=12,
        issue_title="Fix parser",
        base_sha="f" * 40,
        problem="Parser drops escaped values.",
        non_goals=["No API redesign"],
        relevant_files=[
            FileReference(
                path="src/parser.py",
                start_line=10,
                end_line=18,
                reason="Current parser branch.",
            )
        ],
        approach=["Add a regression test", "Make the smallest fix"],
        tests=["pytest tests/test_parser.py"],
        uncertainties=["Need maintainer confirmation for compatibility."],
    )


def test_plan_round_trips_json_and_markdown(tmp_path: Path) -> None:
    artifact = save_plan(_plan(), tmp_path)

    assert artifact.json_path.is_file()
    assert artifact.markdown_path.is_file()
    assert "src/parser.py:10-18" in artifact.markdown_path.read_text(encoding="utf-8")
    assert load_plan(artifact.json_path) == _plan()


def test_plan_rejects_invalid_reference_and_sha() -> None:
    with pytest.raises(ValueError):
        FileReference(path="../secret", start_line=1, end_line=1, reason="bad")
    with pytest.raises(ValueError):
        ImplementationPlan(
            repo="acme/project",
            issue_number=1,
            issue_title="x",
            base_sha="bad",
            problem="x",
        )


def test_load_plan_reports_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(PlanError, match="plan"):
        load_plan(path)
