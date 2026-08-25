"""The guarded runner must actually kill what it says it kills.

README tells contributors to run the suite through a guarded runner rather than
raw pytest. That instruction is only worth following if the runner really does
bound the run and really does clean up after it, so these tests drive the script
end to end against a child that hangs on purpose.

The long sleep here is not an arbitrary wait: it IS the thing under test. The
proof that the guard worked is that the child never reaches the line after it -
if the marker file appears, the child outlived its budget and the runner failed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The runner is a script, not a package module, so the suite reaches it the
# same way the shell does. Importing it is what lets the platform-independent
# decision tests run on a host where neither process groups nor job objects
# can be exercised.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_tests_guarded  # noqa: E402

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


def _process_alive(pid: int) -> bool:
    """Whether ``pid`` is still running, asked of the OS rather than inferred.

    The marker file these tests relied on was not evidence: a grandchild still
    sleeping out CHILD_SLEEP_SECONDS has not written its marker either way, so
    `not marker.exists()` passed just as happily over a LIVE orphan as over a
    reaped one. It caught nothing while reading as proof, which is worse than
    not being there. The marker stays as a second signal; this one decides.
    """
    if sys.platform == "win32":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


# --- the runner's own budgets have to be budgets ------------------------------


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "0"])
def test_a_budget_that_is_not_a_finite_positive_number_is_refused(tmp_path, bad):
    """`nan` is the dangerous one: every comparison against it is False, so
    `elapsed > wall` never fires and the ceiling silently does not exist while
    the banner still claims one. `inf` disables it pointlessly and a negative
    value fires instantly on a run that has not misbehaved."""
    _write_case(tmp_path, "def test_passes():\n    assert True\n")

    result = _run_guarded(tmp_path, "--wall-seconds", bad)

    assert result.returncode != 0, f"{bad} was accepted as a wall budget"
    assert "must be a finite positive" in result.stderr
    assert "1 passed" not in result.stdout, "pytest ran despite an invalid budget"


@pytest.mark.parametrize("bad", ["nan", "-1"])
def test_an_invalid_idle_budget_is_refused_too(tmp_path, bad):
    _write_case(tmp_path, "def test_passes():\n    assert True\n")

    result = _run_guarded(tmp_path, "--wall-seconds", "60", "--idle-seconds", bad)

    assert result.returncode != 0
    assert "must be a finite positive" in result.stderr


def test_the_reported_worker_ceiling_is_one_the_child_can_actually_see(tmp_path):
    """The banner used to print a 'worker ceiling' that was computed and then
    applied to nothing. A number in the output that constrains nothing is worse
    than no number, because it reads as a guarantee."""
    _write_case(
        tmp_path,
        "import os\n"
        "\n"
        "def test_sees_the_ceiling():\n"
        "    assert os.environ['HOOKMAKER_MAX_TEST_WORKERS'].isdigit()\n",
    )

    result = _run_guarded(tmp_path, "--wall-seconds", "120")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "HOOKMAKER_MAX_TEST_WORKERS=" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and signals")
def test_a_grandchild_that_ignores_sigterm_is_still_killed(tmp_path):
    """pytest can exit while something it spawned keeps running. The first
    version of this runner returned as soon as the PARENT died, so a grandchild
    that ignores SIGTERM outlived the whole run - and `os.getpgid` on the reaped
    parent then raised, so the SIGKILL escalation could not even find the group.
    """
    marker = tmp_path / "grandchild-finished.marker"
    pid_file = tmp_path / "grandchild.pid"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"time.sleep({CHILD_SLEEP_SECONDS})\n"
        f"Path({str(marker)!r}).write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _write_case(
        tmp_path,
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "\n"
        "def test_spawns_a_stubborn_grandchild():\n"
        f"    p = subprocess.Popen([sys.executable, {str(stubborn)!r}])\n"
        f"    Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
        # pytest itself finishes quickly; the grandchild is what must be reaped.
        "    time.sleep(1)\n",
    )

    result = _run_guarded(tmp_path, "--wall-seconds", str(WALL_BUDGET))

    assert result.returncode == 124, f"{result.stdout}\n{result.stderr}"
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _process_alive(pid), f"grandchild {pid} outlived the run"
    assert not marker.exists(), "the grandchild ran to completion"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_a_grandchild_that_outlives_pytest_is_killed_on_windows(tmp_path):
    """The Windows half of the same guarantee, and the half that can actually be
    run here.

    There is no signal to ignore on Windows, so the POSIX test's stubborn
    grandchild has no equivalent - a process that simply keeps running is enough.
    `taskkill /T` cannot help either: it walks the tree by PID, and once pytest
    has been reaped its PID names nothing. A job object is what still knows the
    grandchild belongs to this run, which is why the runner opens one.
    """
    marker = tmp_path / "grandchild-finished.marker"
    pid_file = tmp_path / "grandchild.pid"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"time.sleep({CHILD_SLEEP_SECONDS})\n"
        f"Path({str(marker)!r}).write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _write_case(
        tmp_path,
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "\n"
        "def test_spawns_a_grandchild():\n"
        f"    p = subprocess.Popen([sys.executable, {str(stubborn)!r}])\n"
        f"    Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')\n"
        # pytest itself finishes quickly; the grandchild is what must be reaped.
        "    time.sleep(1)\n",
    )

    result = _run_guarded(tmp_path, "--wall-seconds", str(WALL_BUDGET))

    assert result.returncode == 124, f"{result.stdout}\n{result.stderr}"
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _process_alive(pid), f"grandchild {pid} outlived the run"
    assert not marker.exists(), "the grandchild ran to completion"


def test_the_run_is_not_over_while_anything_it_spawned_is_alive():
    """The decision the first version got wrong, as a plain function.

    It ended the monitor loop on `while process.poll() is None`, so pytest
    exiting 0 over a live decoder was reported as a clean pass. This is the one
    part of the runner that can be checked without arranging real process
    groups, job objects or a stubborn grandchild - which matters, because the
    platform-specific halves cannot both be executed on one machine.
    """
    assert run_tests_guarded._run_is_over(parent_alive=False, tree_alive=False) is True

    assert run_tests_guarded._run_is_over(parent_alive=True, tree_alive=True) is False
    assert run_tests_guarded._run_is_over(parent_alive=True, tree_alive=False) is False
    # The regression: pytest is gone, its children are not, and this must NOT
    # read as a finished run.
    assert run_tests_guarded._run_is_over(parent_alive=False, tree_alive=True) is False


def test_an_unavailable_tree_handle_does_not_stall_the_run():
    """`_open_tree` returns None when the platform gives nothing to hold onto.

    Treating that unknown as "something is alive" would hang every run on a
    platform the runner cannot introspect, so the parent's own exit stays the
    fallback signal. Fail open here, not closed.
    """
    assert run_tests_guarded._tree_alive(None) is False
    assert run_tests_guarded._run_is_over(parent_alive=False, tree_alive=False) is True
