"""A helper's whole tree dies with the call, and its ceiling is what is left.

``run_bounded`` killed its DIRECT child. An ffmpeg that had spawned a helper of
its own left that helper running past the timeout, past the cancellation, and
past the point anyone was reading the answer - so "terminated" described one
process out of however many the decode had started.

Separately, every decoder was handed a fixed 32 MB stdout ceiling whatever the
call's own reservation had already committed, so the per-call preview budget
bounded what the workers were ASKED for and not what the pipe would accept.
"""

import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from telegram_mcp.visual import frames as frames_mod
from telegram_mcp.visual.bounded_process import ProcessTimeout, run_bounded

# Long enough that the grandchild is unambiguously still alive when the parent
# is killed, and short enough that a leaked one cannot outlive the suite.
GRANDCHILD_SECONDS = 45
HELPER_TIMEOUT_SECONDS = 3
# These helpers say nothing; the ceiling only has to be a real number.
CEILING = 64 * 1024


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        finished = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        )
        return str(pid) in finished.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawner(tmp_path: Path) -> list:
    """A helper that starts a grandchild, records its pid, and then hangs."""
    script = tmp_path / "spawner.py"
    pid_file = tmp_path / "grandchild.pid"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({GRANDCHILD_SECONDS})'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        f"time.sleep({GRANDCHILD_SECONDS})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _await_pid_file(pid_file: Path, seconds: float = 20.0) -> int:
    """Wait for the helper to record its grandchild, bounded."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.05)
    pytest.fail("the helper never recorded a grandchild, so this proved nothing")


def test_a_timed_out_helper_takes_its_descendants_with_it(tmp_path):
    """The acceptance case. `process.kill()` ends one process; what has to end is
    everything the call started."""
    command = _spawner(tmp_path)
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(ProcessTimeout):
        run_bounded(
            command,
            label="spawning helper",
            timeout=HELPER_TIMEOUT_SECONDS,
            max_output_bytes=CEILING,
            max_stderr_bytes=CEILING,
        )

    grandchild = _await_pid_file(pid_file, seconds=1.0)
    # The kill is synchronous with the raise, but the OS reaps asynchronously.
    deadline = time.monotonic() + 15.0
    while _process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _process_alive(grandchild), f"grandchild {grandchild} outlived the timeout"


def test_a_cancelled_helper_takes_its_descendants_with_it(tmp_path):
    """Cancellation is the other way out of the same call, and it used to leave
    the same orphan behind."""
    import threading

    command = _spawner(tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    cancelled = threading.Event()

    def _cancel_once_it_has_spawned():
        _await_pid_file(pid_file, seconds=20.0)
        cancelled.set()

    waiter = threading.Thread(target=_cancel_once_it_has_spawned, daemon=True)
    waiter.start()

    with pytest.raises(Exception):
        run_bounded(
            command,
            label="spawning helper",
            timeout=GRANDCHILD_SECONDS,
            cancelled=cancelled,
            max_output_bytes=CEILING,
            max_stderr_bytes=CEILING,
        )
    waiter.join(timeout=5)

    grandchild = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 15.0
    while _process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _process_alive(grandchild), f"grandchild {grandchild} outlived the cancellation"


def test_a_helper_that_exits_cleanly_leaves_no_descendant_either(tmp_path):
    """Closing the job kills whatever is still in it, which after a clean exit is
    exactly the helpers the child walked away from."""
    script = tmp_path / "quick.py"
    pid_file = tmp_path / "grandchild.pid"
    script.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({GRANDCHILD_SECONDS})'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n",
        encoding="utf-8",
    )

    run_bounded(
        [sys.executable, str(script)],
        label="quick helper",
        timeout=30,
        max_output_bytes=CEILING,
        max_stderr_bytes=CEILING,
    )

    grandchild = _await_pid_file(pid_file, seconds=1.0)
    deadline = time.monotonic() + 15.0
    while _process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _process_alive(grandchild), (
        f"grandchild {grandchild} survived a helper that had already exited, so the "
        "job handle is leaking one process tree per call"
    )


# --- the ceiling is the reservation, not a constant -------------------------


def test_every_decoder_run_is_given_what_the_call_has_left():
    """The fixed ceiling let each decoder in a batch write up to 32 MB whatever
    the call's reservation had already committed."""
    source = inspect.getsource(frames_mod)

    assert (
        source.count("max_output_bytes=budget.remaining_bytes") >= 4
    ), "a decoder call is still handed the constant instead of the reservation"
    # And the probe gets a ceiling sized for metadata rather than for a decode.
    assert "max_output_bytes=MAX_PROBE_OUTPUT_BYTES" in source
    assert frames_mod.MAX_PROBE_OUTPUT_BYTES < frames_mod.MAX_DECODER_OUTPUT_BYTES


def test_the_run_helper_clamps_whatever_it_is_given():
    """A caller passing a larger number than the decoder ceiling must not raise
    it, and one passing zero must not produce a ceiling of zero bytes."""
    source = inspect.getsource(frames_mod._run)

    assert "min(MAX_DECODER_OUTPUT_BYTES" in source
    assert "max(1," in source


def test_the_capture_worker_measures_a_frame_before_it_appends_it():
    """`payload += data` then checking the result materialises the whole
    over-limit buffer first, and `+=` on bytes copies - so at the moment of the
    copy the process holds both."""
    source = Path("telegram_mcp/visual/capture_worker.py").read_text(encoding="utf-8")

    checked = source.index("check_response_bytes(len(payload) + len(data), limit)")
    appended = source.index("payload += data")
    assert checked < appended, "the buffer is built before the ceiling is consulted"
