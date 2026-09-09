import subprocess
from pathlib import Path
from shutil import copytree

import pytest

from deepcontrib.patches import (
    PatchError,
    apply_patch,
    build_unified_diff,
    check_patch,
    save_patch_artifact,
    summarize_patch,
    validate_patch,
    working_tree_digest,
)
from deepcontrib.plan import ImplementationPlan


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        repo="example/python-bug",
        issue_number=1,
        issue_title="Fix slug",
        base_sha="a" * 40,
        problem="Repeated spaces produce repeated separators.",
    )


def _diff() -> str:
    return """--- a/src/slugger.py
+++ b/src/slugger.py
@@ -1 +1 @@
-return title.lower().replace(" ", "-")
+return "-".join(title.lower().split())
"""


def test_patch_validation_and_git_apply_round_trip(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "src").mkdir()
    (base / "src" / "slugger.py").write_bytes(
        b'return title.lower().replace(" ", "-")\n'
    )
    diff = _diff()

    summary = check_patch(
        diff,
        base_root=base,
        base_sha="a" * 40,
        allowed_paths=["src/slugger.py"],
    )
    target = tmp_path / "target"
    copytree(base, target)
    before = working_tree_digest(target)
    applied = apply_patch(
        diff,
        target_root=target,
        base_sha="a" * 40,
        allowed_paths=["src/slugger.py"],
    )

    assert summary == applied
    assert summary.changed_paths == ("src/slugger.py",)
    assert summary.additions == 1
    assert summary.deletions == 1
    assert before != working_tree_digest(target)
    assert "split()" in (target / "src" / "slugger.py").read_text(encoding="utf-8")


def test_patch_apply_uses_an_lf_snapshot_inside_a_parent_git_repo(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent-repository"
    snapshot = parent / "data" / "snapshot"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src" / "parser.py").write_bytes(b"old\n")
    subprocess.run(
        ["git", "init", str(parent)],
        check=True,
        capture_output=True,
        text=True,
    )
    diff = """--- a/src/parser.py
+++ b/src/parser.py
@@ -1 +1 @@
-old
+new
"""

    summary = check_patch(
        diff,
        base_root=snapshot,
        base_sha="a" * 40,
        allowed_paths=["src/parser.py"],
    )
    target = parent / "data" / "working"
    copytree(snapshot, target)
    applied = apply_patch(
        diff,
        target_root=target,
        base_sha="a" * 40,
        allowed_paths=["src/parser.py"],
    )

    assert summary == applied
    assert (target / "src" / "parser.py").read_bytes() == b"new\n"


def test_build_unified_diff_is_deterministic(tmp_path: Path) -> None:
    base = tmp_path / "base"
    modified = tmp_path / "modified"
    for root in (base, modified):
        (root / "src").mkdir(parents=True)
        (root / "src" / "one.py").write_text("one\n", encoding="utf-8")
        (root / "src" / "two.py").write_text("two\n", encoding="utf-8")
    (modified / "src" / "two.py").write_text("changed\n", encoding="utf-8")

    diff = build_unified_diff(base, modified, ["src/two.py", "src/one.py"])

    assert diff.startswith("--- a/src/two.py\n")
    assert "-two" in diff and "+changed" in diff


def test_working_tree_digest_is_stable_for_nested_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for path, value in (
        (root / "z" / "file.txt", "z"),
        (root / "a" / "file.txt", "a"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    first = working_tree_digest(root)
    second = working_tree_digest(root)

    assert first == second


def test_working_tree_digest_ignores_text_newline_style(tmp_path: Path) -> None:
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "README.md").write_bytes(b"first\nsecond\n")
    (crlf / "README.md").write_bytes(b"first\r\nsecond\r\n")

    assert working_tree_digest(lf) == working_tree_digest(crlf)


def test_working_tree_digest_can_ignore_task_local_git_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "file.txt").write_text("same\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("first\n", encoding="utf-8")
    first = working_tree_digest(root, ignored_directories={".git"})
    (root / ".git" / "HEAD").write_text("second\n", encoding="utf-8")

    assert working_tree_digest(root, ignored_directories={".git"}) == first


def test_patch_artifact_is_bound_to_plan_and_saved_atomically(tmp_path: Path) -> None:
    artifact = save_patch_artifact(
        task_id="task-1",
        plan=_plan(),
        diff=_diff(),
        directory=tmp_path,
    )

    assert artifact.patch_path.read_text(encoding="utf-8") == _diff()
    assert artifact.metadata_path.is_file()
    assert artifact.base_sha == "a" * 40
    assert artifact.sha256
    assert artifact.summary.changed_paths == ("src/slugger.py",)

    with pytest.raises(PatchError, match="immutable"):
        save_patch_artifact(
            task_id="task-1",
            plan=_plan(),
            diff=_diff(),
            directory=tmp_path,
        )


@pytest.mark.parametrize(
    "diff, message",
    [
        (_diff().replace("src/slugger.py", "../escape.py"), "outside"),
        (_diff().replace("--- a/src/slugger.py", "Binary files a/x b/x"), "binary"),
        (_diff().replace("--- a/src/slugger.py", "new file mode 120000"), "symbolic"),
        (_diff().replace("--- a/src/slugger.py", "new file mode 160000"), "submodule"),
    ],
)
def test_patch_validation_rejects_unsafe_forms(
    tmp_path: Path, diff: str, message: str
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "slugger.py").write_text("old\n", encoding="utf-8")
    with pytest.raises(PatchError, match=message):
        validate_patch(diff, base_root=tmp_path, base_sha="a" * 40)


def test_patch_validation_rejects_scope_and_bad_base(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "slugger.py").write_text("old\n", encoding="utf-8")
    with pytest.raises(PatchError, match="approved scope"):
        validate_patch(
            _diff(),
            base_root=tmp_path,
            base_sha="a" * 40,
            allowed_paths=["tests/test.py"],
        )
    with pytest.raises(PatchError, match="SHA"):
        validate_patch(_diff(), base_root=tmp_path, base_sha="bad")
    with pytest.raises(PatchError, match="size"):
        validate_patch(_diff(), base_root=tmp_path, base_sha="a" * 40, max_bytes=1)


def test_patch_summary_supports_addition_and_deletion() -> None:
    addition = """--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
"""
    deletion = """--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""

    assert summarize_patch(addition).changed_paths == ("new.txt",)
    assert summarize_patch(deletion).changed_paths == ("old.txt",)


def test_patch_rejects_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("old\n", encoding="utf-8")
    symlink = root / "src"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this Windows runner")
    diff = """--- a/src/file.py
+++ b/src/file.py
@@ -1 +1 @@
-old
+new
"""
    with pytest.raises(PatchError, match="symbolic"):
        validate_patch(diff, base_root=root, base_sha="a" * 40)
