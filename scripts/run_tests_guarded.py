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
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

# `timeout(1)`'s convention. CI and humans both already know this one.
TIMEOUT_EXIT_CODE = 124

DEFAULT_WALL_SECONDS = 1800
DEFAULT_IDLE_SECONDS = 300

# How long a terminated tree gets to die politely before it is killed outright.
GRACE_SECONDS = 10.0

# How often the watchdog re-checks. Small enough to be responsive, large enough
# not to spin a core doing nothing.
POLL_SECONDS = 0.25

# Once pytest itself has exited, anything still running is finishing up - a
# decoder flushing, a worker draining. This is how long that is allowed to take
# before it counts as a leak rather than a tail. Deliberately short: past this
# point nothing is producing output, so the idle budget (designed for a RUNNING
# suite) would sit for minutes on what is already a failure.
ORPHAN_GRACE_SECONDS = 5.0


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

_JOB_OBJECT_BASIC_ACCOUNTING = 1
_JOB_OBJECT_EXTENDED_LIMIT = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


_WINDOWS_JOB_API: Optional[tuple] = None


def _windows_job_types():
    """ctypes declarations for the job-object calls, built once and cached.

    Imported inside the function so that reading this module on POSIX does not
    touch `ctypes.wintypes`, which does not exist there.

    EVERY function below gets an explicit ``restype``/``argtypes``. Without them
    ctypes assumes C ``int``, and a 64-bit HANDLE is silently truncated to 32
    bits - which works for as long as the OS happens to hand out small handles
    and then fails on a busy machine. That failure mode is not hypothetical
    here: it is why the first version of this code reported an empty job over a
    live grandchild, and it is the same truncation this project already hit once
    in its Win32 capture path.
    """
    global _WINDOWS_JOB_API
    if _WINDOWS_JOB_API is not None:
        return _WINDOWS_JOB_API

    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]

    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

    kernel32.Process32First.restype = wintypes.BOOL
    kernel32.Process32First.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

    kernel32.Process32Next.restype = wintypes.BOOL
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

    _WINDOWS_JOB_API = (
        ctypes,
        kernel32,
        EXTENDED_LIMIT_INFORMATION,
        BASIC_ACCOUNTING_INFORMATION,
    )
    return _WINDOWS_JOB_API


_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


class _WindowsTree:
    """Every process this run started, held by handle rather than by number.

    Job-object accounting was the obvious answer and it does not survive
    contact with reality: when the runner itself is already inside a job - as it
    is under any agent harness, CI container or service wrapper that wraps its
    children - a nested job reported zero active processes while a descendant
    was demonstrably still running. Measured here, repeatedly, with the orphan
    alive in `tasklist` at the moment the job called itself empty.

    So identity comes from an open HANDLE per descendant instead. A handle also
    settles the harder problem underneath: a PID is reusable the instant its
    process dies, and a recycled PID would make a clean run report a leak. While
    a handle is open the number cannot be reused, so "is this exact process
    still running" stays answerable after its parent is gone - which is the only
    moment the question matters.
    """

    __slots__ = ("root_pid", "job", "_handles")

    def __init__(self, root_pid: int, job: Optional[int]) -> None:
        self.root_pid = root_pid
        self.job = job
        self._handles: dict = {}

    def sample(self) -> None:
        """Take a handle on any descendant not seen before. Cheap; call each poll."""
        try:
            ctypes, kernel32, _, _ = _windows_job_types()
        except (OSError, AttributeError, ValueError):
            return
        for pid in _descendant_pids(self.root_pid):
            if pid in self._handles:
                continue
            handle = kernel32.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
                False,
                pid,
            )
            if handle:
                self._handles[pid] = handle

    def alive(self) -> bool:
        """Whether any process from this run is still running."""
        try:
            ctypes, kernel32, _, _ = _windows_job_types()
        except (OSError, AttributeError, ValueError):
            return False
        for pid, handle in list(self._handles.items()):
            if kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT:
                return True
            # Exited: drop the handle so the PID is free again and this stays O(live).
            kernel32.CloseHandle(handle)
            del self._handles[pid]
        return False

    def terminate(self) -> None:
        """Kill each held descendant directly.

        `taskkill /T` cannot do this job: it walks the tree from a PID, and by
        the time an orphan matters its parent has already exited, so there is no
        tree left to walk. The handles were taken while the parent was alive,
        which is exactly why they still name the right processes now.
        """
        try:
            _, kernel32, _, _ = _windows_job_types()
        except (OSError, AttributeError, ValueError):
            return
        for handle in self._handles.values():
            kernel32.TerminateProcess(handle, TIMEOUT_EXIT_CODE)

    def close(self) -> None:
        try:
            _, kernel32, _, _ = _windows_job_types()
        except (OSError, AttributeError, ValueError):
            return
        for handle in self._handles.values():
            kernel32.CloseHandle(handle)
        self._handles.clear()
        if self.job:
            kernel32.CloseHandle(self.job)
            self.job = None


def _descendant_pids(root_pid: int) -> list:
    """Every PID descended from ``root_pid``, via one process snapshot.

    Toolhelp rather than `tasklist`: this runs on every poll, and spawning a
    console tool four times a second to answer it would cost more than the tests.
    """
    try:
        ctypes, kernel32, _, _ = _windows_job_types()
        from ctypes import wintypes

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, -1):
            return []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            children: dict = {}
            more = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while more:
                children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
                more = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

        found: list = []
        pending = [root_pid]
        while pending:
            current = pending.pop()
            for child in children.get(current, ()):
                if child not in found and child != root_pid:
                    found.append(child)
                    pending.append(child)
        return found
    except (OSError, AttributeError, ValueError):
        return []


def _open_tree(process: subprocess.Popen) -> Optional[Any]:
    """A handle on everything this run spawned, or None if the platform gave none.

    Read once, now, while the child is certainly alive. `os.getpgid` raises
    after the process is reaped, and a job object cannot be assigned to a
    process that has exited - so looking either up later fails precisely when
    the tree needs to be found.
    """
    if os.name != "nt":
        try:
            return os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            return None

    handle = getattr(process, "_handle", None)
    if handle is None:
        return None
    try:
        ctypes, kernel32, extended_type, _ = _windows_job_types()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        # If this runner is killed, the whole job goes with it. Without this the
        # tree survives its own watchdog, which is the failure being prevented.
        limits = extended_type()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)
        )

        # The job is kept for TERMINATION, where one call ends everything in it.
        # It is deliberately not used to answer "is anything alive" - see
        # _WindowsTree for the measurement that ruled that out.
        if not kernel32.AssignProcessToJobObject(job, int(handle)):
            kernel32.CloseHandle(job)
            job = None
        tree = _WindowsTree(process.pid, job)
        tree.sample()
        return tree
    except (OSError, AttributeError, ValueError):
        return None


def _tree_alive(tree: Optional[Any]) -> bool:
    """Whether ANY process from this run remains, the parent included.

    False when the platform gave no handle: an unknown answer must not stall
    the run forever, so the parent's own exit stays the fallback signal.
    """
    if tree is None:
        return False

    if os.name != "nt":
        try:
            # Signal 0 only checks for the group's existence, it never kills.
            os.killpg(tree, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Something is there; this process just may not signal it.
            return True

    return tree.alive()


def _run_is_over(parent_alive: bool, tree_alive: bool) -> bool:
    """The run ends when the parent AND everything it spawned are gone.

    Pulled out as a plain function because it is the decision the first version
    got wrong - it ended on the parent alone - and because it is the one part of
    this file that can be tested on any platform, without real process groups,
    job objects or a stubborn grandchild to arrange.
    """
    return not parent_alive and not tree_alive


def _terminate_tree(process: subprocess.Popen, tree: Optional[Any]) -> None:
    """Terminate the child and everything it spawned, on either platform.

    The parent exiting is NOT the end, so this waits on the TREE and escalates
    to an unignorable kill if anything is still there after the grace period. A
    grandchild that ignores SIGTERM otherwise survives the whole run.
    """
    if os.name == "nt":
        if tree is not None:
            # The handles first: they are the only thing that still identifies a
            # descendant once its parent is gone.
            tree.terminate()
        if tree is not None and tree.job:
            try:
                # One call ends every process in the job, including any the
                # child started. Nothing in a job can refuse this.
                _windows_job_types()[1].TerminateJobObject(tree.job, TIMEOUT_EXIT_CODE)
            except (OSError, AttributeError, ValueError):
                pass
        # taskkill /T walks the tree by PID, and covers the case where no job
        # object could be created at all.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        process.poll()
        return

    if tree is None:
        process.kill()
        process.poll()
        return

    try:
        os.killpg(tree, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _tree_alive(tree):
            process.poll()
            return
        time.sleep(POLL_SECONDS)

    try:
        os.killpg(tree, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.poll()


def _close_tree(tree: Optional[Any]) -> None:
    """Release the Windows job handle. A process group needs no closing."""
    if tree is None or os.name != "nt":
        return
    try:
        tree.close()
    except (OSError, AttributeError, ValueError):
        pass


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

    tree = _open_tree(process)
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
