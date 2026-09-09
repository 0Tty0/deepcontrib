"""Fixed-command Docker test execution and baseline red/green verification."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Any, Literal, Protocol, cast

from deepcontrib.patches import PatchError, working_tree_digest


class TestRunnerError(RuntimeError):
    """Raised when a test run request is invalid."""

    __test__ = False


class _RunCancelledError(RuntimeError):
    """Internal signal used when a running Docker process is terminated."""

    __test__ = False


RunStatus = Literal[
    "passed",
    "failed",
    "timeout",
    "unsupported_environment",
    "not_run",
]
VerificationStatus = Literal[
    "red_green",
    "baseline_passed",
    "patched_failed",
    "unsupported_environment",
    "not_run",
]


@dataclass(frozen=True)
class TestResult:
    status: RunStatus
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    worktree_digest_before: str | None
    worktree_digest_after: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "worktree_digest_before": self.worktree_digest_before,
            "worktree_digest_after": self.worktree_digest_after,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> TestResult:
        """Restore a bounded result persisted in a test report artifact."""
        if not isinstance(payload, dict):
            raise ValueError("test result must be an object")
        status = payload.get("status")
        valid_statuses = {
            "passed",
            "failed",
            "timeout",
            "unsupported_environment",
            "not_run",
        }
        if status not in valid_statuses:
            raise ValueError("test result has an invalid status")
        command = payload.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            raise ValueError("test result command must be a string list")
        exit_code = payload.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("test result exit_code must be an integer or null")
        duration = payload.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("test result duration_seconds must be numeric")
        timed_out = payload.get("timed_out")
        if not isinstance(timed_out, bool):
            raise ValueError("test result timed_out must be boolean")
        stdout = payload.get("stdout", "")
        stderr = payload.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ValueError("test result logs must be strings")
        before = payload.get("worktree_digest_before")
        after = payload.get("worktree_digest_after")
        if before is not None and not isinstance(before, str):
            raise ValueError("test result digest must be a string or null")
        if after is not None and not isinstance(after, str):
            raise ValueError("test result digest must be a string or null")
        return cls(
            status=cast(RunStatus, status),
            command=tuple(command),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=float(duration),
            timed_out=timed_out,
            worktree_digest_before=before,
            worktree_digest_after=after,
        )


setattr(TestResult, "__test__", False)


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    baseline: TestResult
    patched: TestResult | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "baseline": self.baseline.as_dict(),
            "patched": self.patched.as_dict() if self.patched else None,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> VerificationResult:
        """Restore a verification artifact while rejecting malformed evidence."""
        if not isinstance(payload, dict):
            raise ValueError("verification result must be an object")
        status = payload.get("status")
        valid_statuses = {
            "red_green",
            "baseline_passed",
            "patched_failed",
            "unsupported_environment",
            "not_run",
        }
        if status not in valid_statuses:
            raise ValueError("verification result has an invalid status")
        baseline = TestResult.from_dict(payload.get("baseline"))
        patched_payload = payload.get("patched")
        patched = (
            None if patched_payload is None else TestResult.from_dict(patched_payload)
        )
        message = payload.get("message")
        if not isinstance(message, str):
            raise ValueError("verification result message must be a string")
        return cls(
            status=cast(VerificationStatus, status),
            baseline=baseline,
            patched=patched,
            message=message,
        )


setattr(VerificationResult, "__test__", False)


class TestRunner(Protocol):
    """Protocol used by the task service so tests can inject a fake runner."""

    def verify_red_green(
        self,
        baseline_root: Path,
        patched_root: Path,
        *,
        test_paths: Iterable[str] = ("tests",),
    ) -> VerificationResult: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]
_IMAGE_DIGEST = re.compile(r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_LOG_BYTES = 100_000
_MANAGED_LABEL = "com.deepcontrib.managed=deepcontrib"


@dataclass
class DockerTestRunner:
    """Run only the application-owned Python/pytest command in Docker."""

    image: str
    runner: Runner = subprocess.run
    timeout_seconds: int = 120
    memory: str = "512m"
    cpus: str = "1.0"
    max_log_bytes: int = _MAX_LOG_BYTES
    _cancel_event: Event = field(default_factory=Event, init=False, repr=False)
    _process_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _active_process: subprocess.Popen[str] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not _IMAGE_DIGEST.fullmatch(self.image):
            raise TestRunnerError("test image must be pinned by a sha256 digest")
        if self.timeout_seconds <= 0 or self.max_log_bytes <= 0:
            raise TestRunnerError("test timeout and log limits must be positive")
        if not self.memory or not self.cpus:
            raise TestRunnerError("test resource limits must not be blank")

    @property
    def tool_name(self) -> str:
        return "docker pytest"

    @property
    def running(self) -> bool:
        with self._process_lock:
            return self._active_process is not None

    def cancel(self) -> None:
        """Request cancellation and terminate the active Docker CLI process."""
        self._cancel_event.set()
        with self._process_lock:
            process = self._active_process
        if process is not None:
            _terminate_process(process)

    def reset_cancel(self) -> None:
        """Clear a prior request before a new task-owned test stage starts."""
        self._cancel_event.clear()

    def cleanup_orphaned(self) -> int:
        """Remove managed containers left by a terminated service process."""
        try:
            listed = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"label={_MANAGED_LABEL}"],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=10,
                env=_safe_environment(),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return 0
        if listed.returncode != 0:
            return 0
        container_ids = [
            item.strip() for item in listed.stdout.splitlines() if item.strip()
        ]
        removed = 0
        for container_id in container_ids:
            try:
                result = subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,
                    timeout=10,
                    env=_safe_environment(),
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                removed += 1
        return removed

    def run(
        self,
        workdir: Path,
        *,
        test_paths: Iterable[str] = ("tests",),
    ) -> TestResult:
        """Run fixed pytest arguments against one read-only task worktree."""
        root = Path(workdir).resolve()
        if not root.is_dir():
            raise TestRunnerError("test worktree does not exist")
        paths = _validate_test_paths(test_paths)
        command = self._command(root, paths)
        try:
            before = working_tree_digest(root)
        except PatchError as exc:
            raise TestRunnerError(str(exc)) from exc
        started = time.monotonic()
        try:
            completed = self._invoke(command, root)
        except _RunCancelledError:
            after = self._digest_after(root)
            return self._result(
                "not_run",
                command,
                None,
                "",
                "test run cancelled",
                started,
                before,
                root,
                after=after,
            )
        except FileNotFoundError:
            return self._result(
                "unsupported_environment",
                command,
                None,
                "",
                "Docker is not installed or not available on PATH.",
                started,
                before,
                root,
            )
        except subprocess.TimeoutExpired as exc:
            after = self._digest_after(root)
            stderr = (
                _truncate(exc.stderr, self.max_log_bytes) or "test command timed out"
            )
            status: RunStatus = "timeout"
            if before != after:
                status = "failed"
                stderr = _truncate(
                    f"{stderr}\nworktree changed during a read-only test run",
                    self.max_log_bytes,
                )
            return self._result(
                status,
                command,
                None,
                _truncate(exc.stdout, self.max_log_bytes),
                stderr,
                started,
                before,
                root,
                after=after,
                timed_out=True,
            )
        after = self._digest_after(root)
        run_status: RunStatus = (
            "passed"
            if completed.returncode == 0
            else "not_run"
            if completed.returncode == 5
            else "failed"
        )
        stderr = _truncate(completed.stderr, self.max_log_bytes)
        stdout = _truncate(completed.stdout, self.max_log_bytes)
        if before != after:
            run_status = "failed"
            stderr = _truncate(
                f"{stderr}\nworktree changed during a read-only test run",
                self.max_log_bytes,
            )
        return self._result(
            run_status,
            command,
            completed.returncode,
            stdout,
            stderr,
            started,
            before,
            root,
            after=after,
        )

    def verify_red_green(
        self,
        baseline_root: Path,
        patched_root: Path,
        *,
        test_paths: Iterable[str] = ("tests",),
    ) -> VerificationResult:
        """Run baseline and patched copies and classify only a true red/green pair."""
        baseline = self.run(baseline_root, test_paths=test_paths)
        if baseline.status == "unsupported_environment":
            return VerificationResult(
                "unsupported_environment",
                baseline,
                None,
                "Docker is unavailable; the baseline and patch were not verified.",
            )
        if baseline.status == "timeout":
            return VerificationResult(
                "not_run", baseline, None, "baseline tests timed out; patch was not run"
            )
        patched = self.run(patched_root, test_paths=test_paths)
        if (
            baseline.status == "failed"
            and baseline.worktree_digest_before == baseline.worktree_digest_after
            and patched.status == "passed"
        ):
            return VerificationResult(
                "red_green",
                baseline,
                patched,
                "baseline failed and patched copy passed",
            )
        if baseline.status == "passed":
            return VerificationResult(
                "baseline_passed",
                baseline,
                patched,
                "the reproduction did not fail on the baseline copy",
            )
        if patched.status != "passed":
            return VerificationResult(
                "patched_failed", baseline, patched, "patched copy did not pass"
            )
        return VerificationResult(
            "not_run",
            baseline,
            patched,
            "baseline result was not a reproducible failure",
        )

    def _invoke(
        self, command: list[str], root: Path
    ) -> subprocess.CompletedProcess[str]:
        if self.runner is not subprocess.run:
            return self.runner(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
                env=_safe_environment(),
            )

        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=_safe_environment(),
        )
        with self._process_lock:
            self._active_process = process
        started = time.monotonic()
        try:
            while process.poll() is None:
                if self._cancel_event.is_set():
                    _terminate_process(process)
                    stdout, stderr = process.communicate()
                    self._cleanup_container(command)
                    raise _RunCancelledError()
                if time.monotonic() - started >= self.timeout_seconds:
                    _terminate_process(process)
                    stdout, stderr = process.communicate()
                    self._cleanup_container(command)
                    raise subprocess.TimeoutExpired(
                        command,
                        self.timeout_seconds,
                        output=stdout,
                        stderr=stderr,
                    )
                time.sleep(0.05)
            stdout, stderr = process.communicate()
            if self._cancel_event.is_set():
                self._cleanup_container(command)
                raise _RunCancelledError()
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        finally:
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None

    def _cleanup_container(self, command: list[str]) -> None:
        try:
            name_index = command.index("--name") + 1
            name = command[name_index]
        except (ValueError, IndexError):
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=10,
                env=_safe_environment(),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return

    def _command(self, root: Path, paths: tuple[str, ...]) -> list[str]:
        source = str(root)
        name = (
            "deepcontrib-test-"
            + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        )
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "1000:1000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--mount",
            f"type=bind,src={source},dst=/workspace,readonly",
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--label",
            _MANAGED_LABEL,
            "--name",
            name,
            self.image,
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *paths,
        ]

    def _digest_after(self, root: Path) -> str | None:
        try:
            return working_tree_digest(root)
        except PatchError:
            return None

    def _result(
        self,
        status: RunStatus,
        command: list[str],
        exit_code: int | None,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
        started: float,
        before: str | None,
        root: Path,
        *,
        after: str | None = None,
        timed_out: bool = False,
    ) -> TestResult:
        return TestResult(
            status=status,
            command=tuple(command),
            exit_code=exit_code,
            stdout=_truncate(stdout, self.max_log_bytes),
            stderr=_truncate(stderr, self.max_log_bytes),
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=timed_out,
            worktree_digest_before=before,
            worktree_digest_after=after
            if after is not None
            else self._digest_after(root),
        )


def _validate_test_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in paths:
        raw = value.strip()
        if "\\" in raw:
            raise TestRunnerError("test paths must be safe repository-relative paths")
        text = raw
        path = PurePosixPath(text)
        if (
            not text
            or path.is_absolute()
            or ".." in path.parts
            or any(not _PATH_PART.fullmatch(part) for part in path.parts)
        ):
            raise TestRunnerError("test paths must be safe repository-relative paths")
        normalized.append(path.as_posix())
    if not normalized:
        raise TestRunnerError("at least one test path is required")
    return tuple(dict.fromkeys(normalized))


def _safe_environment() -> dict[str, str]:
    """Pass only locale and deterministic Python settings into the container."""
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate a Docker CLI process, escalating if it does not exit."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _truncate(value: str | bytes | None, limit: int = _MAX_LOG_BYTES) -> str:
    if value is None:
        return ""
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore") + "\n[output truncated]"
