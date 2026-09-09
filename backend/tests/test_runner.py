import subprocess
from pathlib import Path
from typing import Any

import pytest

from deepcontrib.test_runner import (
    DockerTestRunner,
    TestResult,
    VerificationResult,
)
from deepcontrib.test_runner import (
    TestRunnerError as RunnerError,
)

IMAGE = "python:3.11-slim@sha256:" + "a" * 64


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_bug.py").write_text(
        "def test_ok(): pass\n", encoding="utf-8"
    )
    return root


def _completed(command: list[str], code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, "1 passed\n", "")


def test_runner_uses_fixed_restricted_docker_command(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return _completed(command)

    result = DockerTestRunner(IMAGE, runner=fake_runner).run(
        root, test_paths=("tests/test_bug.py",)
    )

    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.worktree_digest_before == result.worktree_digest_after
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:8] == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--read-only",
    ]
    assert "--cap-drop" in command and "ALL" in command
    assert "--security-opt" in command and "no-new-privileges" in command
    assert "--mount" in command
    assert any(
        value.endswith(",readonly")
        for value in command
        if value.startswith("type=bind,")
    )
    assert command[-5:] == [
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests/test_bug.py",
    ]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 120
    assert kwargs["env"] == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@pytest.mark.parametrize(
    "exception, status",
    [
        (FileNotFoundError(), "unsupported_environment"),
        (subprocess.TimeoutExpired("docker", 1, output="partial"), "timeout"),
    ],
)
def test_runner_reports_unavailable_or_timeout(
    tmp_path: Path, exception: Exception, status: str
) -> None:
    root = _worktree(tmp_path)

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        raise exception

    result = DockerTestRunner(IMAGE, runner=fake_runner, timeout_seconds=1).run(root)

    assert result.status == status
    assert result.timed_out is (status == "timeout")
    assert result.worktree_digest_before == result.worktree_digest_after


def test_runner_reports_failure_and_truncates_output(tmp_path: Path) -> None:
    root = _worktree(tmp_path)

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, "x" * 100, "failed")

    result = DockerTestRunner(IMAGE, runner=fake_runner, max_log_bytes=16).run(root)

    assert result.status == "failed"
    assert result.exit_code == 3
    assert result.stdout.endswith("[output truncated]")
    assert len(result.stdout.encode("utf-8")) <= 40


def test_runner_distinguishes_no_tests_from_a_failed_test_run(tmp_path: Path) -> None:
    root = _worktree(tmp_path)

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 5, "no tests ran\n", "")

    result = DockerTestRunner(IMAGE, runner=fake_runner).run(root)

    assert result.status == "not_run"
    assert result.exit_code == 5


def test_runner_rejects_unsafe_test_paths(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    runner = DockerTestRunner(IMAGE, runner=lambda *_args, **_kwargs: _completed([]))

    for path in ("../outside", "C:/outside", "/tmp/test.py", "tests\\secret.py"):
        with pytest.raises(RunnerError, match="repository-relative"):
            runner.run(root, test_paths=(path,))


def test_runner_detects_test_mutation(tmp_path: Path) -> None:
    root = _worktree(tmp_path)

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        (root / "created.py").write_text("unexpected\n", encoding="utf-8")
        return _completed(command)

    result = DockerTestRunner(IMAGE, runner=fake_runner).run(root)

    assert result.status == "failed"
    assert "worktree changed" in result.stderr


def test_verify_red_green_does_not_accept_a_mutating_baseline(
    tmp_path: Path,
) -> None:
    baseline = _worktree(tmp_path / "baseline")
    patched = _worktree(tmp_path / "patched")
    calls = 0

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            (baseline / "unexpected.py").write_text("x\n", encoding="utf-8")
            return _completed(command, 1)
        return _completed(command, 0)

    verification = DockerTestRunner(IMAGE, runner=fake_runner).verify_red_green(
        baseline, patched
    )

    assert verification.status == "not_run"


def test_verify_red_green_requires_baseline_failure(tmp_path: Path) -> None:
    baseline = _worktree(tmp_path / "baseline")
    patched = _worktree(tmp_path / "patched")
    calls = 0

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _completed(command, 1 if calls == 1 else 0)

    verification = DockerTestRunner(IMAGE, runner=fake_runner).verify_red_green(
        baseline, patched
    )

    assert verification.status == "red_green"
    assert verification.baseline.status == "failed"
    assert verification.patched is not None
    assert verification.patched.status == "passed"


def test_verify_red_green_marks_baseline_pass_as_not_reproducible(
    tmp_path: Path,
) -> None:
    baseline = _worktree(tmp_path / "baseline")
    patched = _worktree(tmp_path / "patched")

    def fake_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(command)

    verification = DockerTestRunner(IMAGE, runner=fake_runner).verify_red_green(
        baseline, patched
    )

    assert verification.status == "baseline_passed"
    assert verification.patched is not None


def test_runner_validates_image_and_limits(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="pinned"):
        DockerTestRunner("python:3.11-slim")
    with pytest.raises(RunnerError, match="positive"):
        DockerTestRunner(IMAGE, timeout_seconds=0)
    with pytest.raises(RunnerError, match="blank"):
        DockerTestRunner(IMAGE, memory="")


def test_verification_result_round_trips_from_artifact_json() -> None:
    runner = DockerTestRunner(IMAGE)
    baseline = TestResult(
        status="failed",
        command=("docker", "run"),
        exit_code=1,
        stdout="red",
        stderr="",
        duration_seconds=0.2,
        timed_out=False,
        worktree_digest_before="a",
        worktree_digest_after="a",
    )
    patched = TestResult(
        status="passed",
        command=("docker", "run"),
        exit_code=0,
        stdout="green",
        stderr="",
        duration_seconds=0.2,
        timed_out=False,
        worktree_digest_before="b",
        worktree_digest_after="b",
    )
    original = VerificationResult("red_green", baseline, patched, "ok")

    restored = VerificationResult.from_dict(original.as_dict())

    assert restored == original
    assert runner.tool_name == "docker pytest"
