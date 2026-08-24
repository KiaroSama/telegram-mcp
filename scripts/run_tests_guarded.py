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
import math
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

# `timeout(1)`'s convention. CI and humans both already know this one.
TIMEOUT_EXIT_CODE = 124

DEFAULT_WALL_SECONDS = 1800
DEFAULT_IDLE_SECONDS = 300

# How long a terminated tree gets to die politely before it is killed outright.
GRACE_SECONDS = 10.0

# How often the watchdog re-checks. Small enough to be responsive, large enough
# not to spin a core doing nothing.
POLL_SECONDS = 0.25


def _positive_seconds(name: str, value: float) -> float:
    """A budget that is not a finite positive number is not a budget.

    `nan` is the dangerous one: every comparison against it is False, so
    `elapsed > wall` never fires and the ceiling silently does not exist -
    which is worse than having no ceiling at all, because the output still
    claims one. `inf` disables it honestly but pointlessly, and a negative
    value fires instantly on a run that has not misbehaved.
    """
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(
            f"GUARDED RUNNER: --{name.replace('_', '-')} must be a finite positive "
            f"number of seconds, got {value!r}."
        )
    return value


def _worker_ceiling() -> int:
    """One shared cap on parallelism, resource-aware rather than core-count-proud.

    Leaves headroom for the OS, this parent process, and whatever else the
    machine is doing - a test suite that spawns ffmpeg per test oversubscribes
    long before the core count says it should.
    """
    cores = os.cpu_count() or 2
    return max(2, min(8, cores - 2))


def _group_alive(pgid: int) -> bool:
    """Whether ANY process remains in the group. Signal 0 only checks, never kills."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Something is there; this process just may not signal it.
        return True


def _terminate_tree(process: subprocess.Popen, pgid: Optional[int]) -> None:
    """Terminate the child and everything it spawned, on either platform.

    Three things here are deliberate, and each of them was wrong in the first
    version of this script:

    * The parent exiting is NOT the end. pytest can die while a decoder it
      spawned keeps running, so returning early on `process.poll()` left exactly
      the orphan this function exists to prevent.
    * ``pgid`` is captured while the child is still alive and passed in.
      ``os.getpgid(pid)`` raises once the parent is reaped, so looking it up here
      would fail precisely when escalation is needed.
    * The grace period waits on the GROUP, not the parent, and escalates to
      SIGKILL if anything is still there - a grandchild that ignores SIGTERM
      otherwise survives the whole run.
    """
    if os.name == "nt":
        # taskkill /T walks the tree; no process-group signal on Windows reaches
        # grandchildren reliably. /F because a graceful ask has already failed by
        # the time we are here.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        process.poll()
        return

    if pgid is None:
        process.kill()
        process.poll()
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            process.poll()
            return
        time.sleep(POLL_SECONDS)

    try:
        os.killpg(pgid, signal.SIGKILL)
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

    # The ceiling is exported, not merely printed. Nothing in this suite runs
    # parallel today (no xdist is installed), so this constrains whatever reads
    # the documented variable rather than pretending to cap workers that do not
    # exist - the previous version printed a number it never applied anywhere.
    child_env = dict(os.environ)
    child_env.setdefault("HOOKMAKER_MAX_TEST_WORKERS", str(_worker_ceiling()))

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Detached: an interactive prompt in automation is a hang, and it must
        # fail rather than wait for a human who is not there.
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=child_env,
        **popen_extras,
    )

    # Read once, now, while the parent is certainly alive: os.getpgid raises
    # after it is reaped, which is exactly when termination needs the group.
    pgid: Optional[int] = None
    if os.name != "nt":
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None

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
        _terminate_tree(process, pgid)
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
    _positive_seconds("wall_seconds", known.wall_seconds)
    _positive_seconds("idle_seconds", known.idle_seconds)

    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    print(
        f"GUARDED RUNNER: wall {known.wall_seconds:.0f}s, idle {known.idle_seconds:.0f}s, "
        f"HOOKMAKER_MAX_TEST_WORKERS={_worker_ceiling()}",
        file=sys.stderr,
    )
    return run(passthrough, known.wall_seconds, known.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
