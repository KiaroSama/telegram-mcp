"""The guarded runner must actually kill what it says it kills.

README tells contributors to run the suite through a guarded runner rather than
raw pytest. That instruction is only worth following if the runner really does
bound the run and really does clean up after it, so these tests drive the script
end to end against a child that hangs on purpose.

The long sleep here is not an arbitrary wait: it IS the thing under test. The
proof that the guard worked is that the child never reaches the line after it -
if the marker file appears, the child outlived its budget and the runner failed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "run_tests_guarded.py"

# Comfortably longer than any budget these tests give it, so the only way the
# child finishes is if the guard never fired.
CHILD_SLEEP_SECONDS = 120

# Enough headroom for interpreter startup and collection on a loaded machine,
# while keeping the test itself a few seconds.
WALL_BUDGET = 6.0
IDLE_BUDGET = 4.0
HARNESS_TIMEOUT = 90


def _write_case(directory: Path, body: str) -> Path:
    case = directory / "test_case.py"
    case.write_text(body, encoding="utf-8")
    # An empty ini keeps the repo's own pyproject settings and conftest out of
    # the child run, so these tests measure the runner and nothing else.
    (directory / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return case


def _run_guarded(directory: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            *extra,
            "--",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_case.py",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT,
    )


def test_a_hanging_test_is_terminated_and_reported_as_a_timeout(tmp_path):
    marker = tmp_path / "child-finished.marker"
    _write_case(
        tmp_path,
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "def test_hangs():\n"
        f"    time.sleep({CHILD_SLEEP_SECONDS})\n"
        f"    Path({str(marker)!r}).write_text('finished', encoding='utf-8')\n",
    )

    result = _run_guarded(tmp_path, "--wall-seconds", str(WALL_BUDGET))

    assert (
        result.returncode == 124
    ), f"expected the timeout exit code, got {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "wall timeout" in result.stderr
    assert not marker.exists(), "the child outlived its budget - the tree was not terminated"


def test_no_output_for_too_long_is_a_timeout_even_while_busy(tmp_path):
    """A deadlock prints nothing while still holding the CPU, so elapsed time
    alone would let it sit for the whole wall budget before anything noticed."""
    marker = tmp_path / "child-finished.marker"
    _write_case(
        tmp_path,
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "def test_is_quiet():\n"
        f"    time.sleep({CHILD_SLEEP_SECONDS})\n"
        f"    Path({str(marker)!r}).write_text('finished', encoding='utf-8')\n",
    )

    result = _run_guarded(
        tmp_path,
        "--wall-seconds",
        "600",
        "--idle-seconds",
        str(IDLE_BUDGET),
    )

    assert (
        result.returncode == 124
    ), f"expected the timeout exit code, got {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "idle timeout" in result.stderr
    assert not marker.exists(), "the child outlived its idle budget"


def test_a_passing_run_returns_pytest_s_own_exit_code(tmp_path):
    _write_case(tmp_path, "def test_passes():\n    assert True\n")

    result = _run_guarded(tmp_path, "--wall-seconds", "120")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "1 passed" in result.stdout


def test_a_failing_run_propagates_the_failure(tmp_path):
    """A guard that swallowed the exit code would turn every red suite green."""
    _write_case(tmp_path, "def test_fails():\n    assert False\n")

    result = _run_guarded(tmp_path, "--wall-seconds", "120")

    assert result.returncode not in (0, 124), "a real test failure must not look like success"
    assert "1 failed" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_the_child_runs_in_its_own_process_group(tmp_path):
    """One signal has to reach the decoders pytest spawned, not just pytest."""
    _write_case(
        tmp_path,
        "import os\n"
        "\n"
        "def test_reports_its_group():\n"
        "    assert os.getpgid(0) != os.getpgid(os.getppid())\n",
    )

    result = _run_guarded(tmp_path, "--wall-seconds", "120")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
