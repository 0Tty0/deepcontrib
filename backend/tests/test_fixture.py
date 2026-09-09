from pathlib import Path


def test_bug_fixture_has_reproduction_test_and_issue() -> None:
    fixture = Path(__file__).parents[2] / "tests" / "fixtures" / "python-bug"

    assert (fixture / "src" / "slugger.py").is_file()
    assert (fixture / "tests" / "test_slugger.py").is_file()
    assert (fixture / "ISSUE.md").is_file()
