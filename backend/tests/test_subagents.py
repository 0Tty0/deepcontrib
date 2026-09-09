from pathlib import Path

from deepcontrib.repository import RepositorySnapshot
from deepcontrib.subagents import RepoExplorer


def test_repo_explorer_returns_symbols_calls_and_read_only_tool_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calculator.py").write_text(
        "def add(value: int) -> int:\n    return helper(value)\n\n"
        "def helper(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    snapshot = RepositorySnapshot(root, "a" * 40)

    result = RepoExplorer().explore(snapshot, relevant_paths=["src/calculator.py"])

    assert result.base_sha == "a" * 40
    assert result.files == ("src/calculator.py",)
    assert {item.name for item in result.symbols} == {"add", "helper"}
    assert result.symbols[0].calls == ("helper",)
    assert result.tool_names == ("list_files", "read_file", "grep")
    assert result.can_write is False
    assert result.can_execute is False


def test_repo_explorer_reports_invalid_python_as_uncertainty(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    snapshot = RepositorySnapshot(root, "b" * 40)

    result = RepoExplorer().explore(snapshot, relevant_paths=["broken.py"])

    assert result.symbols == ()
    assert result.uncertainties
    assert "broken.py" in result.uncertainties[0]
