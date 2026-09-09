from pathlib import Path

import pytest

from deepcontrib.repository import (
    RepositoryError,
    RepositorySnapshot,
    parse_issue_number,
    parse_repository_url,
)


def test_parse_public_github_url() -> None:
    repo = parse_repository_url("https://github.com/langchain-ai/deepagents/")

    assert repo.owner == "langchain-ai"
    assert repo.name == "deepagents"
    assert repo.full_name == "langchain-ai/deepagents"


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/owner/repo",
        "https://gitlab.com/owner/repo",
        "https://github.com/owner/repo/issues/1",
        "https://github.com/owner/repo?token=secret",
        "https://github.com/owner/../secret",
        "https://github.com:bad-port/owner/repo",
    ],
)
def test_parse_repository_url_rejects_non_canonical_input(value: str) -> None:
    with pytest.raises(RepositoryError):
        parse_repository_url(value)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1; whoami"])
def test_parse_issue_number_requires_positive_integer(value: str) -> None:
    with pytest.raises(RepositoryError, match="issue"):
        parse_issue_number(value)


def test_snapshot_lists_reads_and_greps_with_line_citations() -> None:
    root = Path(__file__).parents[2] / "tests" / "fixtures" / "python-bug"
    snapshot = RepositorySnapshot(root, "a" * 40)

    files = snapshot.list_files()
    excerpt = snapshot.read_file("src/slugger.py", offset=1, limit=20)
    matches = snapshot.grep("slugify")

    assert "src/slugger.py" in [item.path for item in files]
    assert excerpt.base_sha == "a" * 40
    assert excerpt.start_line == 1
    assert "def slugify" in excerpt.content
    slugger_matches = [item for item in matches if item.path == "src/slugger.py"]
    assert slugger_matches
    assert slugger_matches[0].line_number == 4


def test_snapshot_rejects_path_escape() -> None:
    root = Path(__file__).parents[2] / "tests" / "fixtures" / "python-bug"
    snapshot = RepositorySnapshot(root, "b" * 40)

    with pytest.raises(RepositoryError, match="outside"):
        snapshot.read_file("../secret.txt")


def test_snapshot_enforces_file_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_text("x" * 20, encoding="utf-8")
    snapshot = RepositorySnapshot(
        tmp_path,
        "c" * 40,
        max_file_bytes=10,
    )

    with pytest.raises(RepositoryError, match="size"):
        snapshot.read_file("large.txt")


def test_snapshot_rejects_invalid_root_sha_limits_and_ranges(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="SHA"):
        RepositorySnapshot(tmp_path, "bad")
    with pytest.raises(RepositoryError, match="directory"):
        RepositorySnapshot(tmp_path / "missing", "a" * 40)
    with pytest.raises(RepositoryError, match="limits"):
        RepositorySnapshot(tmp_path, "a" * 40, max_files=0)

    snapshot = RepositorySnapshot(tmp_path, "a" * 40)
    with pytest.raises(RepositoryError, match="blank"):
        snapshot.read_file(" ")
    with pytest.raises(RepositoryError, match="offset"):
        snapshot.read_file("missing", offset=0)
    with pytest.raises(RepositoryError, match="query"):
        snapshot.grep(" ")
    with pytest.raises(RepositoryError, match="too long"):
        snapshot.grep("x" * 201)
    with pytest.raises(RepositoryError, match="max_matches"):
        snapshot.grep("x", max_matches=0)
    with pytest.raises(RepositoryError, match="directory"):
        snapshot.list_files("missing")


def test_snapshot_rejects_binary_and_non_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\x00binary")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    snapshot = RepositorySnapshot(tmp_path, "a" * 40)

    with pytest.raises(RepositoryError, match="binary"):
        snapshot.read_file("binary.bin")
    with pytest.raises(RepositoryError, match="UTF-8"):
        snapshot.read_file("invalid.txt")


def test_snapshot_enforces_file_count_and_total_size(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("1234", encoding="utf-8")
    (tmp_path / "two.txt").write_text("5678", encoding="utf-8")
    with pytest.raises(RepositoryError, match="too many"):
        RepositorySnapshot(tmp_path, "a" * 40, max_files=1).list_files()
    with pytest.raises(RepositoryError, match="total size"):
        RepositorySnapshot(tmp_path, "a" * 40, max_total_bytes=5).list_files()


def test_snapshot_extracts_safe_archive(tmp_path: Path) -> None:
    import io
    import tarfile

    archive = tmp_path / "safe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"hello\nworld\n"
        info = tarfile.TarInfo("repo-sha/readme.txt")
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))

    snapshot = RepositorySnapshot.extract_tarball(
        archive,
        tmp_path / "safe",
        "a" * 40,
    )

    assert snapshot.read_file("readme.txt", offset=2, limit=1).content == "world"


def test_tar_extraction_rejects_traversal(tmp_path: Path) -> None:
    import io
    import tarfile

    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("owner-repo-sha/../../escape.txt")
        payload = b"unsafe"
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))

    with pytest.raises(RepositoryError, match="unsafe"):
        RepositorySnapshot.extract_tarball(archive, tmp_path / "out", "d" * 40)


def test_tar_extraction_rejects_file_destination_and_invalid_limits(
    tmp_path: Path,
) -> None:
    import tarfile

    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    destination = tmp_path / "destination"
    destination.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RepositoryError, match="directory"):
        RepositorySnapshot.extract_tarball(archive, destination, "a" * 40)
    with pytest.raises(RepositoryError, match="limits"):
        RepositorySnapshot.extract_tarball(
            archive,
            tmp_path / "other",
            "a" * 40,
            max_files=0,
        )
