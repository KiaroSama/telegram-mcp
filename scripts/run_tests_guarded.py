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

The run is over when the pytest process AND everything it spawned are gone.
Watching only the parent is the bug this script existed to prevent and then
had: pytest exits 0 while a decoder it started keeps running, the monitor loop
ended on that exit, and the runner reported success over a live orphan.

Exit code is pytest's own, except 124 for a timeout - the same convention
coreutils `timeout` uses, so CI reads it without special-casing.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import threading
import time
from typing import Optional

# Process containment moved next door. Re-exported because the tests reach
# `run_tests_guarded._tree_alive` and `._run_is_over` through this module,
# and the runner is a script rather than a package.
from guarded_process import (  # noqa: F401  (re-exported)
    POLL_SECONDS,
    CONTAINMENT_EXIT_CODE,
    ContainmentFailed,
    GRACE_SECONDS,
    ORPHAN_GRACE_SECONDS,
    TIMEOUT_EXIT_CODE,
    _CREATE_SUSPENDED,
    _JOB_OBJECT_BASIC_ACCOUNTING,
    _JOB_OBJECT_EXTENDED_LIMIT,
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _PROCESS_QUERY_LIMITED_INFORMATION,
    _PROCESS_TERMINATE,
    _RESUME_FAILED,
    _SYNCHRONIZE,
    _TH32CS_SNAPPROCESS,
    _TH32CS_SNAPTHREAD,
    _THREAD_SUSPEND_RESUME,
    _WAIT_TIMEOUT,
    _WINDOWS_JOB_API,
    _WindowsTree,
    _close_tree,
    _contain_windows,
    _descendant_pids,
    _open_tree,
    _resume_windows,
    _run_is_over,
    _terminate_tree,
    _tree_alive,
    _windows_job_types,
)

# `timeout(1)`'s convention. CI and humans both already know this one.

DEFAULT_WALL_SECONDS = 1800
DEFAULT_IDLE_SECONDS = 300

# How long a terminated tree gets to die politely before it is killed outright.

# How often the watchdog re-checks. Small enough to be responsive, large enough

# Once pytest itself has exited, anything still running is finishing up - a
# decoder flushing, a worker draining. This is how long that is allowed to take
# before it counts as a leak rather than a tail. Deliberately short: past this
# point nothing is producing output, so the idle budget (designed for a RUNNING
# suite) would sit for minutes on what is already a failure.


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


# --------------------------------------------------------------------------
# Knowing whether anything from this run is still alive
#
# The platforms disagree about what "the tree" even is, so each one gets the
# handle it can actually answer the question with: a process group on POSIX, a
# job object on Windows. Both are captured while the child is certainly alive,
# because both become unavailable once it is reaped - which is exactly when the
# answer matters.
# --------------------------------------------------------------------------


# `subprocess` exposes CREATE_NEW_PROCESS_GROUP but not this one, so it is
# spelled out. From the CreateProcess documentation.

# Containment could not be established, so nothing ran. Distinct from a test
# failure and from a timeout: the suite has said nothing at all.


def run(argv: list[str], wall_seconds: float, idle_seconds: float) -> int:
    command = [sys.executable, "-m", "pytest", *argv]

    popen_extras: dict = {}
    if os.name == "nt":
        # CREATE_SUSPENDED so the job can be built and assigned BEFORE a single
        # instruction runs. Started first and contained afterwards, anything the
        # child spawned in that window was outside the job and outlived the kill.
        popen_extras["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
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
        # Bytes, unbuffered. The pump below reads chunks rather than lines, and a
        # text-mode line-buffered stream cannot deliver a partial line.
        bufsize=0,
        env=child_env,
        **popen_extras,
    )

    job = None
    if os.name == "nt":
        try:
            job = _contain_windows(process)
            _resume_windows(process)
        except ContainmentFailed as failure:
            # Nothing ran, and nothing is left running. A suspended child nobody
            # resumed is a hang, and a resumed child nobody contained is the
            # orphan this script exists to prevent - so neither is allowed to
            # stand, and the run reports that it never happened.
            try:
                process.kill()
                process.wait(timeout=GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
            print(
                f"GUARDED RUNNER: refusing to run uncontained - {failure}.",
                file=sys.stderr,
            )
            return CONTAINMENT_EXIT_CODE

    tree = _open_tree(process, job)
    if tree is None:
        # Say so rather than degrading quietly: without a tree handle the run
        # ends on the parent alone, which is exactly the blind spot this
        # script exists to close.
        print(
            "GUARDED RUNNER: no process-tree handle on this platform; "
            "orphan detection is OFF for this run.",
            file=sys.stderr,
        )

    last_output = time.monotonic()
    lock = threading.Lock()

    def pump() -> None:
        """Forward the child's output, counting every byte as progress.

        Read in chunks, not lines. `for line in stdout` yields only on a newline, so
        a run printing a progress bar with carriage returns - or any long line still
        being written - looked completely idle, and the idle clock fired on a child
        that was demonstrably working. Bytes are what "progress" means here.
        """
        nonlocal last_output
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        while True:
            try:
                # Returns as soon as ANY bytes are available, unlike read(n).
                chunk = os.read(descriptor, 65536)
            except OSError:
                return
            if not chunk:
                return
            with lock:
                last_output = time.monotonic()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

    # A daemon thread, so a wedged read on a killed child cannot keep the
    # interpreter alive after the watchdog has given up on it.
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    started = time.monotonic()
    reason = ""
    parent_exited_at: Optional[float] = None
    try:
        while True:
            if os.name == "nt" and tree is not None:
                tree.sample()
            parent_alive = process.poll() is None
            alive = _tree_alive(tree)
            if _run_is_over(parent_alive, alive):
                break

            now = time.monotonic()
            with lock:
                idle_for = now - last_output

            if now - started > wall_seconds:
                reason = f"wall timeout: still running after {wall_seconds:.0f}s"
                break
            if parent_alive and idle_for > idle_seconds:
                reason = f"idle timeout: no output for {idle_for:.0f}s (limit {idle_seconds:.0f}s)"
                break
            if not parent_alive:
                # pytest is done; only its leftovers are still here.
                if parent_exited_at is None:
                    parent_exited_at = now
                elif now - parent_exited_at > ORPHAN_GRACE_SECONDS:
                    reason = (
                        f"pytest exited but its process tree was still alive "
                        f"{ORPHAN_GRACE_SECONDS:.0f}s later"
                    )
                    break
            time.sleep(POLL_SECONDS)

        if reason:
            print(f"\nGUARDED RUNNER: {reason}", file=sys.stderr)
            print("Terminating the test process tree.", file=sys.stderr)
            _terminate_tree(process, tree)
            reader.join(timeout=GRACE_SECONDS)
            print(
                f"GUARDED RUNNER: tree terminated; exiting {TIMEOUT_EXIT_CODE}.",
                file=sys.stderr,
            )
            return TIMEOUT_EXIT_CODE

        reader.join(timeout=GRACE_SECONDS)
        return process.returncode
    finally:
        _close_tree(tree)


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
