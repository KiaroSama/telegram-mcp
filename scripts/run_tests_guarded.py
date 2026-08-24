#!/usr/bin/env python3
"""Run pytest with the bounds a raw invocation does not have.

A raw `pytest` has no wall ceiling, no no-progress ceiling and no process-tree
cleanup, so a hang cannot be detected and cannot be proven to have been cleaned
up. That matters here more than in most suites: this project drives ffmpeg,
ffprobe and a native rlottie decoder, each of which can wedge without exiting,
and a wedged child outlives the pytest process that spawned it.

Two independent ceilings, because they catch different failures:

* **wall** - the whole run took too long, however busy it looked.
* **idle** - no output for too long. A deadlock still prints nothing while
  burning CPU, so elapsed time alone would let a fast machine sit for the full
  wall budget before noticing.

On either, the child's whole process *tree* is terminated, not just the child:
pytest is the parent of the decoders, and killing only pytest orphans them.

Exit code is pytest's own, except 124 for a timeout - the same convention
coreutils `timeout` uses, so CI reads it without special-casing.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

# `timeout(1)`'s convention. CI and humans both already know this one.
TIMEOUT_EXIT_CODE = 124

DEFAULT_WALL_SECONDS = 1800
DEFAULT_IDLE_SECONDS = 300

# How long a terminated tree gets to die politely before it is killed outright.
GRACE_SECONDS = 10.0

# How often the watchdog re-checks. Small enough to be responsive, large enough
# not to spin a core doing nothing.
POLL_SECONDS = 0.25


def _worker_ceiling() -> int:
    """One shared cap on parallelism, resource-aware rather than core-count-proud.

    Leaves headroom for the OS, this parent process, and whatever else the
    machine is doing - a test suite that spawns ffmpeg per test oversubscribes
    long before the core count says it should.
    """
    cores = os.cpu_count() or 2
    return max(2, min(8, cores - 2))


def _terminate_tree(process: subprocess.Popen) -> None:
    """Terminate the child and everything it spawned, on either platform."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        # taskkill /T walks the tree; there is no process-group signal on
        # Windows that reaches grandchildren reliably.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        # The child was started in its own session, so one signal reaches the
        # whole group - including decoders pytest spawned.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(POLL_SECONDS)

    if os.name != "nt":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    process.poll()


def run(argv: list[str], wall_seconds: float, idle_seconds: float) -> int:
    command = [sys.executable, "-m", "pytest", *argv]

    popen_extras: dict = {}
    if os.name == "nt":
        popen_extras["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_extras["start_new_session"] = True

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Detached: an interactive prompt in automation is a hang, and it must
        # fail rather than wait for a human who is not there.
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        **popen_extras,
    )

    last_output = time.monotonic()
    lock = threading.Lock()

    def pump() -> None:
        nonlocal last_output
        assert process.stdout is not None
        for line in process.stdout:
            with lock:
                last_output = time.monotonic()
            sys.stdout.write(line)
            sys.stdout.flush()

    # A daemon thread, so a wedged read on a killed child cannot keep the
    # interpreter alive after the watchdog has given up on it.
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    started = time.monotonic()
    reason = ""
    while process.poll() is None:
        now = time.monotonic()
        with lock:
            idle_for = now - last_output
        if now - started > wall_seconds:
            reason = f"wall timeout: still running after {wall_seconds:.0f}s"
            break
        if idle_for > idle_seconds:
            reason = f"idle timeout: no output for {idle_for:.0f}s (limit {idle_seconds:.0f}s)"
            break
        time.sleep(POLL_SECONDS)

    if reason:
        print(f"\nGUARDED RUNNER: {reason}", file=sys.stderr)
        print("Terminating the test process tree.", file=sys.stderr)
        _terminate_tree(process)
        reader.join(timeout=GRACE_SECONDS)
        print(f"GUARDED RUNNER: tree terminated; exiting {TIMEOUT_EXIT_CODE}.", file=sys.stderr)
        return TIMEOUT_EXIT_CODE

    reader.join(timeout=GRACE_SECONDS)
    return process.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run pytest under a wall clock, an idle clock, and process-tree cleanup.",
        epilog="Everything after -- is passed to pytest unchanged.",
    )
    parser.add_argument(
        "--wall-seconds",
        type=float,
        default=float(os.environ.get("TEST_WALL_SECONDS", DEFAULT_WALL_SECONDS)),
        help="abandon the run after this long overall (default: %(default)s)",
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=float(os.environ.get("TEST_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)),
        help="abandon the run after this long with no output (default: %(default)s)",
    )
    known, passthrough = parser.parse_known_args()

    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    print(
        f"GUARDED RUNNER: wall {known.wall_seconds:.0f}s, idle {known.idle_seconds:.0f}s, "
        f"worker ceiling {_worker_ceiling()}",
        file=sys.stderr,
    )
    return run(passthrough, known.wall_seconds, known.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
