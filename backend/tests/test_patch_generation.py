from pathlib import Path

import pytest

from deepcontrib.patch_generation import (
    PatchFile,
    PatchGenerationError,
    PatchProposal,
    run_model_patch,
)
from deepcontrib.plan import FileReference, ImplementationPlan


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        repo="example/project",
        issue_number=1,
        issue_title="Fix parser",
        base_sha="a" * 40,
        problem="Trim parser input.",
        relevant_files=[
            FileReference(
                path="src/parser.py", start_line=1, end_line=2, reason="parser"
            )
        ],
    )


def test_run_model_patch_requires_structured_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "deepcontrib.patch_generation.build_patch_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.patch_generation.invoke_with_thread",
        lambda *args, **kwargs: {"messages": []},
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    with pytest.raises(PatchGenerationError, match="structured"):
        run_model_patch(
            _plan(),
            snapshot,
            model="fake",
            thread_id="thread",
        )


def test_run_model_patch_validates_and_returns_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proposal = PatchProposal(
        diff=("--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n")
    )
    monkeypatch.setattr(
        "deepcontrib.patch_generation.build_patch_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.patch_generation.invoke_with_thread",
        lambda *args, **kwargs: {"structured_response": proposal},
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    result = run_model_patch(
        _plan(),
        snapshot,
        model="fake",
        thread_id="thread",
    )

    assert result == proposal


def test_patch_proposal_normalizes_a_missing_final_newline() -> None:
    proposal = PatchProposal(
        diff="--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new"
    )

    assert proposal.diff.endswith("\n")


def test_run_model_patch_builds_diff_from_complete_file_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "parser.py").write_bytes(b"old\n")
    proposal = PatchProposal(files=[PatchFile(path="src/parser.py", content="new\n")])
    monkeypatch.setattr(
        "deepcontrib.patch_generation.build_patch_agent",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "deepcontrib.patch_generation.invoke_with_thread",
        lambda *args, **kwargs: {"structured_response": proposal},
    )

    result = run_model_patch(
        _plan(),
        snapshot,
        model="fake",
        thread_id="thread",
    )

    assert result.diff == (
        "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
