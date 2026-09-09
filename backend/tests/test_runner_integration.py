import os
import threading
import time
from pathlib import Path

import pytest

from deepcontrib.test_runner import DockerTestRunner, TestResult


@pytest.mark.integration
def test_real_docker_runner_proves_a_fixture_red_green(tmp_path: Path) -> None:
    image = os.environ.get("DEEPCONTRIB_TEST_IMAGE")
    if not image:
        pytest.skip("DEEPCONTRIB_TEST_IMAGE is not configured")
    test_text = (
        "from src.value import value\n\ndef test_value():\n    assert value() == 2\n"
    )
    for name, result in (("base", 1), ("patched", 2)):
        root = tmp_path / name
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "src" / "value.py").write_text(
            f"def value():\n    return {result}\n", encoding="utf-8"
        )
        (root / "tests" / "test_value.py").write_text(test_text, encoding="utf-8")

    verification = DockerTestRunner(image, timeout_seconds=30).verify_red_green(
        tmp_path / "base", tmp_path / "patched"
    )

    assert verification.status == "red_green"
    assert verification.baseline.status == "failed"
    assert verification.patched is not None
    assert verification.patched.status == "passed"
    assert (
        verification.baseline.worktree_digest_before
        == verification.baseline.worktree_digest_after
    )


@pytest.mark.integration
def test_real_docker_runner_terminates_a_cancelled_container(tmp_path: Path) -> None:
    image = os.environ.get("DEEPCONTRIB_TEST_IMAGE")
    if not image:
        pytest.skip("DEEPCONTRIB_TEST_IMAGE is not configured")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hang.py").write_text(
        "import time\n\ndef test_hang():\n    time.sleep(60)\n",
        encoding="utf-8",
    )
    runner = DockerTestRunner(image, timeout_seconds=30)
    result_holder: list[TestResult] = []
    worker = threading.Thread(
        target=lambda: result_holder.append(runner.run(tmp_path)),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not runner.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner.running

    runner.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(result_holder) == 1
    result = result_holder[0]
    assert result.status == "not_run"
    assert "cancelled" in result.stderr
