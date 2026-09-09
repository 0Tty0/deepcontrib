"""Safe public-repository references and read-only snapshot access."""

from __future__ import annotations

import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class RepositoryError(ValueError):
    """Raised when a repository reference or snapshot operation is unsafe."""


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IGNORED_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules"}


@dataclass(frozen=True)
class RepositoryRef:
    """Canonical public GitHub repository identity."""

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.git"


def _validate_component(value: str, label: str) -> str:
    if not _COMPONENT_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise RepositoryError(f"invalid GitHub repository {label}")
    return value


def parse_repository_url(value: str) -> RepositoryRef:
    """Accept only an HTTPS URL for ``github.com/owner/repo``."""
    raw = value.strip()
    parts = urlsplit(raw)
    try:
        port = parts.port
    except ValueError as exc:
        raise RepositoryError("repository URL contains an invalid port") from exc
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.lower() != "github.com"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.query
        or parts.fragment
    ):
        raise RepositoryError(
            "repository must be an HTTPS github.com/owner/repo URL without "
            "credentials, query parameters, or fragments"
        )

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        raise RepositoryError("repository URL must contain exactly owner and repo")
    owner = _validate_component(segments[0], "owner")
    name = segments[1][:-4] if segments[1].endswith(".git") else segments[1]
    return RepositoryRef(owner, _validate_component(name, "name"))


def parse_issue_number(value: str | int) -> int:
    """Parse a positive decimal GitHub Issue number."""
    if isinstance(value, bool):
        raise RepositoryError("issue number must be a positive integer")
    text = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise RepositoryError("issue number must be a positive integer")
    return int(text)


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int


@dataclass(frozen=True)
class FileExcerpt:
    path: str
    base_sha: str
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line_number: int
    line: str


class RepositorySnapshot:
    """Read-only view over a fixed, size-limited repository directory."""

    def __init__(
        self,
        root: Path,
        base_sha: str,
        *,
        max_files: int = 2_000,
        max_file_bytes: int = 256 * 1024,
        max_total_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if not _SHA_PATTERN.fullmatch(base_sha):
            raise RepositoryError("snapshot base SHA must be a 40-character SHA")
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise RepositoryError(f"snapshot root is not a directory: {root}")
        if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
            raise RepositoryError("snapshot limits must be positive")
        self.root = resolved_root
        self.base_sha = base_sha.lower()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def _safe_path(self, relative_path: str) -> Path:
        normalized = relative_path.strip().replace("\\", "/")
        if not normalized:
            raise RepositoryError("repository path must not be blank")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise RepositoryError("repository path points outside the snapshot")

        candidate = self.root.joinpath(*path.parts)
        current = self.root
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise RepositoryError("symlinks are not allowed in a snapshot")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise RepositoryError(
                "repository path points outside the snapshot"
            ) from exc
        return candidate

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _scan_files(self, start: Path) -> list[FileInfo]:
        files: list[FileInfo] = []
        total_bytes = 0
        for current, directories, filenames in os.walk(start, followlinks=False):
            current_path = Path(current)
            for directory in list(directories):
                directory_path = current_path / directory
                if directory_path.is_symlink():
                    raise RepositoryError("symlinks are not allowed in a snapshot")
                if directory in _IGNORED_DIRECTORIES:
                    directories.remove(directory)
            for filename in sorted(filenames):
                path = current_path / filename
                if path.is_symlink():
                    raise RepositoryError("symlinks are not allowed in a snapshot")
                if path.suffix == ".pyc":
                    continue
                if not path.is_file():
                    continue
                size = path.stat().st_size
                if size > self.max_file_bytes:
                    raise RepositoryError(
                        "file exceeds the configured size limit: "
                        f"{self._relative_path(path)}"
                    )
                files.append(FileInfo(self._relative_path(path), size))
                total_bytes += size
                if len(files) > self.max_files:
                    raise RepositoryError("snapshot contains too many files")
                if total_bytes > self.max_total_bytes:
                    raise RepositoryError("snapshot exceeds the total size limit")
        return files

    def list_files(self, directory: str = ".") -> list[FileInfo]:
        """List regular files beneath a safe relative directory."""
        start = self._safe_path(directory)
        if not start.is_dir():
            raise RepositoryError(f"repository directory does not exist: {directory}")
        return self._scan_files(start)

    def _read_text(self, relative_path: str) -> str:
        path = self._safe_path(relative_path)
        if not path.is_file() or path.is_symlink():
            raise RepositoryError(f"repository file does not exist: {relative_path}")
        if path.stat().st_size > self.max_file_bytes:
            raise RepositoryError(
                f"file exceeds the configured size limit: {relative_path}"
            )
        payload = path.read_bytes()
        if b"\x00" in payload:
            raise RepositoryError(f"binary files are not supported: {relative_path}")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryError(f"file is not valid UTF-8: {relative_path}") from exc

    def read_file(
        self, relative_path: str, *, offset: int = 1, limit: int = 200
    ) -> FileExcerpt:
        """Read a bounded UTF-8 excerpt with one-based line coordinates."""
        if offset < 1 or limit <= 0:
            raise RepositoryError("offset must be >= 1 and limit must be positive")
        text = self._read_text(relative_path)
        lines = text.splitlines()
        start_index = offset - 1
        selected = lines[start_index : start_index + limit]
        end_line = offset + len(selected) - 1 if selected else offset - 1
        return FileExcerpt(
            path=relative_path.replace("\\", "/"),
            base_sha=self.base_sha,
            start_line=offset,
            end_line=end_line,
            content="\n".join(selected),
        )

    def grep(
        self, query: str, directory: str = ".", *, max_matches: int = 100
    ) -> list[SearchMatch]:
        """Case-insensitive literal search over bounded text files."""
        needle = query.strip()
        if not needle:
            raise RepositoryError("search query must not be blank")
        if len(needle) > 200:
            raise RepositoryError("search query is too long")
        if max_matches <= 0:
            raise RepositoryError("max_matches must be positive")
        matches: list[SearchMatch] = []
        for info in self.list_files(directory):
            text = self._read_text(info.path)
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle.casefold() in line.casefold():
                    matches.append(SearchMatch(info.path, line_number, line))
                    if len(matches) >= max_matches:
                        return matches
        return matches

    @staticmethod
    def extract_tarball(
        archive: Path,
        destination: Path,
        base_sha: str,
        *,
        max_files: int = 2_000,
        max_file_bytes: int = 256 * 1024,
        max_total_bytes: int = 20 * 1024 * 1024,
    ) -> RepositorySnapshot:
        """Safely extract a GitHub tarball and return its read-only snapshot."""
        if not _SHA_PATTERN.fullmatch(base_sha):
            raise RepositoryError("snapshot base SHA must be a 40-character SHA")
        if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
            raise RepositoryError("snapshot limits must be positive")
        archive_path = Path(archive)
        if not archive_path.is_file():
            raise RepositoryError(f"snapshot archive does not exist: {archive}")
        target = Path(destination).resolve()
        if target.exists():
            if not target.is_dir():
                raise RepositoryError("snapshot destination must be a directory")
            if any(target.iterdir()):
                raise RepositoryError("snapshot destination must be empty")
        target.mkdir(parents=True, exist_ok=True)

        total_bytes = 0
        file_count = 0
        try:
            with tarfile.open(archive_path, mode="r:*") as handle:
                members = handle.getmembers()
                top_level = (
                    PurePosixPath(members[0].name.replace("\\", "/")).parts[0]
                    if members
                    else ""
                )
                for member in members:
                    member_path = PurePosixPath(member.name.replace("\\", "/"))
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise RepositoryError("archive contains an unsafe path")
                    parts = member_path.parts
                    relative_parts = (
                        parts[1:] if parts and parts[0] == top_level else parts
                    )
                    if not relative_parts:
                        continue
                    if member.issym() or member.islnk():
                        raise RepositoryError("archive contains an unsafe link")
                    output = target.joinpath(*relative_parts).resolve(strict=False)
                    try:
                        output.relative_to(target)
                    except ValueError as exc:
                        raise RepositoryError(
                            "archive contains an unsafe path"
                        ) from exc
                    if member.isdir():
                        output.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise RepositoryError("archive contains an unsupported entry")
                    file_count += 1
                    if file_count > max_files or member.size > max_file_bytes:
                        raise RepositoryError(
                            "archive exceeds the configured file limits"
                        )
                    total_bytes += member.size
                    if total_bytes > max_total_bytes:
                        raise RepositoryError(
                            "archive exceeds the configured size limit"
                        )
                    if output.exists():
                        raise RepositoryError("archive contains duplicate paths")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    extracted = handle.extractfile(member)
                    if extracted is None:
                        raise RepositoryError("archive file could not be read")
                    with output.open("xb") as destination_file:
                        destination_file.write(extracted.read())
        except (OSError, tarfile.TarError) as exc:
            raise RepositoryError("could not read repository snapshot archive") from exc

        return RepositorySnapshot(
            target,
            base_sha,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
