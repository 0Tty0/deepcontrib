"""Bounded unified-diff generation, validation, and application helpers."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from deepcontrib.plan import ImplementationPlan


class PatchError(RuntimeError):
    """Raised when a patch is invalid or cannot be applied safely."""


_DIFF_HEADER = re.compile(r"^diff --git a/([^\n]+) b/([^\n]+)$")
_HUNK_HEADER = re.compile(r"^@@ .+ @@")
_MAX_PATCH_BYTES = 5 * 1024 * 1024
_MAX_PATCH_FILES = 100


@dataclass(frozen=True)
class PatchSummary:
    changed_paths: tuple[str, ...]
    additions: int
    deletions: int


@dataclass(frozen=True)
class PatchArtifact:
    patch_id: str
    task_id: str
    version: int
    base_sha: str
    patch_path: Path
    metadata_path: Path
    sha256: str
    summary: PatchSummary


def _safe_diff_path(value: str) -> str:
    path_text = value.strip()
    if not path_text or "\x00" in path_text or "\\" in path_text:
        raise PatchError("patch contains an invalid path")
    path = PurePosixPath(path_text)
    if path.is_absolute() or ".." in path.parts or path_text != path.as_posix():
        raise PatchError("patch path points outside the repository")
    return path.as_posix()


def _header_path(value: str, prefix: str) -> str | None:
    path = value.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if not path.startswith(prefix):
        raise PatchError("patch file header has an invalid prefix")
    return _safe_diff_path(path[len(prefix) :])


def _parse_paths(diff: str) -> tuple[str, ...]:
    paths: set[str] = set()
    old_path: str | None = None
    new_path: str | None = None
    old_header_seen = False
    new_header_seen = False
    for line in diff.splitlines():
        match = _DIFF_HEADER.fullmatch(line)
        if match:
            old_git, new_git = match.groups()
            old_path = _safe_diff_path(old_git)
            new_path = _safe_diff_path(new_git)
            if old_path != new_path:
                raise PatchError(
                    "file renames are not supported in the first patch version"
                )
            paths.add(old_path)
            continue
        if line.startswith("--- "):
            old_path = _header_path(line[4:], "a/")
            old_header_seen = True
            if old_path:
                paths.add(old_path)
        elif line.startswith("+++ "):
            new_path = _header_path(line[4:], "b/")
            new_header_seen = True
            if new_path:
                paths.add(new_path)
        elif line.startswith(("Binary files ", "GIT binary patch")):
            raise PatchError("binary patches are not supported")
        elif re.match(r"^(new|old) file mode 120000$", line):
            raise PatchError("symbolic-link patches are not supported")
        elif re.match(r"^(new|old) file mode 160000$", line):
            raise PatchError("submodule patches are not supported")
        elif line.startswith(("rename from ", "rename to ")):
            raise PatchError("file renames are not supported")
    if not paths:
        raise PatchError("patch does not contain a file change")
    if len(paths) > _MAX_PATCH_FILES:
        raise PatchError("patch changes too many files")
    if not old_header_seen or not new_header_seen:
        raise PatchError("patch must contain both file headers")
    return tuple(sorted(paths))


def summarize_patch(diff: str) -> PatchSummary:
    """Parse a text patch into paths and line-change counts."""
    paths = _parse_paths(diff)
    additions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return PatchSummary(paths, additions, deletions)


def validate_patch(
    diff: str,
    *,
    base_root: Path,
    base_sha: str,
    allowed_paths: Iterable[str] | None = None,
    max_bytes: int = _MAX_PATCH_BYTES,
) -> PatchSummary:
    """Validate patch shape and repository boundaries without changing files."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise PatchError("patch base SHA must be a 40-character SHA")
    if max_bytes <= 0 or len(diff.encode("utf-8")) > max_bytes:
        raise PatchError("patch exceeds the configured size limit")
    root = Path(base_root).resolve()
    if not root.is_dir():
        raise PatchError("patch base directory does not exist")
    summary = summarize_patch(diff)
    allowed = None
    if allowed_paths is not None:
        allowed = {_safe_diff_path(path) for path in allowed_paths}
    for path_text in summary.changed_paths:
        if allowed is not None and path_text not in allowed:
            raise PatchError(
                f"patch changes a path outside the approved scope: {path_text}"
            )
        candidate = root.joinpath(*PurePosixPath(path_text).parts)
        current = root
        for part in PurePosixPath(path_text).parts:
            current /= part
            if current.is_symlink():
                raise PatchError("patch target contains a symbolic link")
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise PatchError("patch target points outside the repository") from exc
    if "@@" not in diff or not any(
        _HUNK_HEADER.match(line) for line in diff.splitlines()
    ):
        raise PatchError("patch does not contain a valid hunk")
    return summary


def _run_git_apply(diff: str, root: Path, *, check: bool, timeout_seconds: int) -> None:
    command = ["git", "apply"]
    if check:
        command.append("--check")
    command.extend(["--whitespace=error", "-"])
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            input=diff.encode("utf-8"),
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
            env={
                **os.environ,
                # Snapshots are plain directories and may live below the
                # application's own Git checkout.  Keep Git rooted at the
                # snapshot so paths such as ``src/main.py`` are unambiguous.
                "GIT_CEILING_DIRECTORIES": str(root.parent),
                # Do not let a developer's global ``core.autocrlf`` rewrite
                # repository bytes while applying an approval-bound patch.
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
    except FileNotFoundError as exc:
        raise PatchError("Git is required to validate and apply patches") from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchError("Git patch operation timed out") from exc
    if completed.returncode != 0:
        detail = (
            (completed.stderr or b"")
            .decode("utf-8", errors="replace")
            .strip()
            .replace("\n", " ")[:500]
        )
        raise PatchError(
            f"git apply {'check' if check else 'operation'} failed: {detail}"
        )


def check_patch(
    diff: str,
    *,
    base_root: Path,
    base_sha: str,
    allowed_paths: Iterable[str] | None = None,
    timeout_seconds: int = 15,
) -> PatchSummary:
    """Validate and ask Git whether the patch can apply, without writing files."""
    if timeout_seconds <= 0:
        raise PatchError("patch timeout must be positive")
    summary = validate_patch(
        diff,
        base_root=base_root,
        base_sha=base_sha,
        allowed_paths=allowed_paths,
    )
    _run_git_apply(
        diff, Path(base_root).resolve(), check=True, timeout_seconds=timeout_seconds
    )
    return summary


def apply_patch(
    diff: str,
    *,
    target_root: Path,
    base_sha: str,
    allowed_paths: Iterable[str] | None = None,
    timeout_seconds: int = 15,
) -> PatchSummary:
    """Validate, check, and apply a patch to a task-owned working copy."""
    summary = check_patch(
        diff,
        base_root=target_root,
        base_sha=base_sha,
        allowed_paths=allowed_paths,
        timeout_seconds=timeout_seconds,
    )
    _run_git_apply(
        diff,
        Path(target_root).resolve(),
        check=False,
        timeout_seconds=timeout_seconds,
    )
    return summary


def build_unified_diff(
    base_root: Path,
    modified_root: Path,
    paths: Iterable[str],
) -> str:
    """Build a deterministic UTF-8 text diff between two task-owned directories."""
    base = Path(base_root).resolve()
    modified = Path(modified_root).resolve()
    if not base.is_dir() or not modified.is_dir():
        raise PatchError("both patch input directories must exist")
    chunks: list[str] = []
    normalized_paths = sorted({_safe_diff_path(path) for path in paths})
    if not normalized_paths or len(normalized_paths) > _MAX_PATCH_FILES:
        raise PatchError("patch must contain between one and the file limit changes")
    for path_text in normalized_paths:
        base_path = base.joinpath(*PurePosixPath(path_text).parts)
        modified_path = modified.joinpath(*PurePosixPath(path_text).parts)
        old_lines = _read_text_lines(base_path) if base_path.exists() else []
        new_lines = _read_text_lines(modified_path) if modified_path.exists() else []
        if old_lines == new_lines:
            continue
        old_name = f"a/{path_text}" if base_path.exists() else "/dev/null"
        new_name = f"b/{path_text}" if modified_path.exists() else "/dev/null"
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=old_name,
                tofile=new_name,
                lineterm="",
            )
        )
    if not chunks:
        raise PatchError("modified directory contains no changes")
    return "\n".join(chunks) + "\n"


def build_unified_diff_from_contents(
    base_root: Path,
    files: Mapping[str, str | None],
) -> str:
    """Build a deterministic diff from complete post-change file contents."""
    base = Path(base_root).resolve()
    if not base.is_dir():
        raise PatchError("patch input directory does not exist")
    if not files or len(files) > _MAX_PATCH_FILES:
        raise PatchError("patch must contain between one and the file limit changes")
    chunks: list[str] = []
    for raw_path, content in sorted(files.items()):
        path_text = _safe_diff_path(raw_path)
        base_path = base.joinpath(*PurePosixPath(path_text).parts)
        if base_path.exists() and base_path.is_symlink():
            raise PatchError("symbolic-link files are not supported")
        if base_path.exists() and not base_path.is_file():
            raise PatchError(f"patch target is not a regular file: {path_text}")
        old_lines = _read_text_lines(base_path) if base_path.exists() else []
        if content is None:
            new_lines: list[str] = []
        else:
            if "\x00" in content:
                raise PatchError(f"binary files are not supported: {path_text}")
            try:
                content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PatchError(f"file is not valid UTF-8: {path_text}") from exc
            new_lines = content.splitlines()
        if old_lines == new_lines and base_path.exists():
            continue
        old_name = f"a/{path_text}" if base_path.exists() else "/dev/null"
        new_name = f"b/{path_text}" if content is not None else "/dev/null"
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=old_name,
                tofile=new_name,
                lineterm="",
            )
        )
    if not chunks:
        raise PatchError("modified files contain no changes")
    return "\n".join(chunks) + "\n"


def _read_text_lines(path: Path) -> list[str]:
    if path.is_symlink():
        raise PatchError("symbolic-link files are not supported")
    payload = path.read_bytes()
    if b"\x00" in payload:
        raise PatchError(f"binary files are not supported: {path.name}")
    try:
        return payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PatchError(f"file is not valid UTF-8: {path.name}") from exc


def save_patch_artifact(
    *,
    task_id: str,
    plan: ImplementationPlan,
    diff: str,
    directory: Path,
    version: int = 1,
    base_root: Path | None = None,
    allowed_paths: Iterable[str] | None = None,
    filename_prefix: str = "patch",
) -> PatchArtifact:
    """Persist an immutable patch and its summary, bound to the Plan base SHA."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", plan.base_sha):
        raise PatchError("plan base SHA is invalid")
    if version <= 0:
        raise PatchError("patch version must be positive")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", filename_prefix):
        raise PatchError("patch artifact filename prefix is invalid")
    summary = (
        validate_patch(
            diff,
            base_root=base_root,
            base_sha=plan.base_sha,
            allowed_paths=allowed_paths,
        )
        if base_root is not None
        else summarize_patch(diff)
    )
    payload = diff.encode("utf-8")
    patch_id = hashlib.sha256(
        f"{task_id}:{version}:{plan.base_sha}".encode() + payload
    ).hexdigest()[:32]
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    patch_path = target / f"{filename_prefix}-v{version}.diff"
    metadata_path = target / f"{filename_prefix}-v{version}.json"
    if patch_path.exists() or metadata_path.exists():
        raise PatchError("patch version already exists and is immutable")
    patch_sha256 = hashlib.sha256(payload).hexdigest()
    metadata = {
        "patch_id": patch_id,
        "task_id": task_id,
        "version": version,
        "base_sha": plan.base_sha,
        "sha256": patch_sha256,
        "summary": {
            "changed_paths": list(summary.changed_paths),
            "additions": summary.additions,
            "deletions": summary.deletions,
        },
    }
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=target, delete=False
        ) as handle:
            patch_tmp = Path(handle.name)
            handle.write(diff)
        patch_tmp.replace(patch_path)
        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=target, delete=False
        ) as handle:
            metadata_tmp = Path(handle.name)
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        metadata_tmp.replace(metadata_path)
    except OSError as exc:
        raise PatchError(f"could not save patch artifacts in {target}") from exc
    return PatchArtifact(
        patch_id=patch_id,
        task_id=task_id,
        version=version,
        base_sha=plan.base_sha,
        patch_path=patch_path,
        metadata_path=metadata_path,
        sha256=patch_sha256,
        summary=summary,
    )


def working_tree_digest(root: Path, *, ignored_directories: Iterable[str] = ()) -> str:
    """Hash a tree while treating text newline styles as equivalent."""
    directory = Path(root).resolve()
    if not directory.is_dir():
        raise PatchError("working tree is not a directory")
    ignored = {name for name in ignored_directories if name}
    digest = hashlib.sha256()
    for current, directories, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name not in ignored]
        directories.sort()
        for name in list(directories):
            if (current_path / name).is_symlink():
                raise PatchError("working tree contains a symbolic link")
        for name in sorted(filenames):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise PatchError("working tree contains an unsupported entry")
            relative = path.relative_to(directory).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(_canonical_file_bytes(path))
            digest.update(b"\0")
    return digest.hexdigest()


def _canonical_file_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
