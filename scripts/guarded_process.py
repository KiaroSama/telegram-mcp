"""Owning a child process tree, and ending it whatever it does.

Split out of ``run_tests_guarded.py``, which was two things at once: the loop
that watches a test run, and the machinery that makes "the run is over" a fact
rather than a hope. This is the second one, and it is almost entirely about the
difference between a PROCESS and a process TREE.

The problem it solves: pytest can exit while something it spawned - ffmpeg,
ffprobe, a native rlottie decoder - is still alive, and a descendant that
outlives its parent is a leak the exit code cannot see. So a child is started
SUSPENDED, put in a Windows job object (or a POSIX process group) before it is
allowed to run, and only then resumed; a process that never ran cannot escape
the container it was about to be put in. Ending the run then means ending the
job, not signalling a pid.

``CONTAINMENT_EXIT_CODE`` and ``TIMEOUT_EXIT_CODE`` live here because they are
what this layer reports, and the grace periods do too - they are how long it
waits before escalating, which is a property of terminating, not of watching.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Optional

TIMEOUT_EXIT_CODE = 124

# How often the watch loop and the termination waits look again. Short
# enough that a finished run is noticed promptly, long enough not to spin
# a core doing nothing.
POLL_SECONDS = 0.25


GRACE_SECONDS = 10.0


ORPHAN_GRACE_SECONDS = 5.0


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

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
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

    # Threads, for the one thing Popen cannot do: it closes the thread handle
    # CreateProcess returned before handing the object back, so a child started
    # CREATE_SUSPENDED has nothing left to resume it with. Toolhelp finds it again.
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    # DWORD, and -1 means failure - so the return has to be compared against
    # 0xFFFFFFFF rather than against -1.
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    _WINDOWS_JOB_API = (
        ctypes,
        kernel32,
        EXTENDED_LIMIT_INFORMATION,
        BASIC_ACCOUNTING_INFORMATION,
        THREADENTRY32,
    )
    return _WINDOWS_JOB_API


_TH32CS_SNAPPROCESS = 0x00000002


_TH32CS_SNAPTHREAD = 0x00000004


_CREATE_SUSPENDED = 0x00000004


_THREAD_SUSPEND_RESUME = 0x0002


_RESUME_FAILED = 0xFFFFFFFF


CONTAINMENT_EXIT_CODE = 125


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
            ctypes, kernel32, _, _, _ = _windows_job_types()
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
            ctypes, kernel32, _, _, _ = _windows_job_types()
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
            _, kernel32, _, _, _ = _windows_job_types()
        except (OSError, AttributeError, ValueError):
            return
        for handle in self._handles.values():
            kernel32.TerminateProcess(handle, TIMEOUT_EXIT_CODE)

    def close(self) -> None:
        try:
            _, kernel32, _, _, _ = _windows_job_types()
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
        ctypes, kernel32, _, _, _ = _windows_job_types()
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


class ContainmentFailed(RuntimeError):
    """The child could not be contained before it was allowed to run."""


def _contain_windows(process: subprocess.Popen) -> int:
    """Put the SUSPENDED child in a kill-on-close job. Returns the job handle.

    Order is the point. The previous version created the job after Popen had
    already started the child, so anything the child spawned in that window was
    never in the job and outlived a TerminateJobObject. It also discarded
    SetInformationJobObject's result, which made a job that failed to configure
    look exactly like one that had - the handle existed either way, and only the
    kill-on-close flag it was missing decided whether the tree died with the
    runner.

    Every return is checked, and a failure raises rather than degrading. A
    warning would leave the run going with containment nobody established.
    """
    ctypes, kernel32, extended_type, _, _ = _windows_job_types()
    handle = getattr(process, "_handle", None)
    if handle is None:
        raise ContainmentFailed("this Python exposed no process handle to contain")

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ContainmentFailed(f"CreateJobObject failed ({ctypes.get_last_error()})")

    limits = extended_type()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ContainmentFailed(f"SetInformationJobObject failed ({error})")

    if not kernel32.AssignProcessToJobObject(job, int(handle)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ContainmentFailed(f"AssignProcessToJobObject failed ({error})")
    return job


def _resume_windows(process: subprocess.Popen) -> None:
    """Let the contained child start.

    ``subprocess.Popen`` closes the thread handle CreateProcess returned before
    handing the object back, so a child started CREATE_SUSPENDED has nothing left
    to resume it with. A process suspended at creation has exactly one thread;
    toolhelp finds it by owner PID.

    A failure here is fatal by construction: the caller kills a child it could
    not start, because a suspended process nobody resumes is a hang.
    """
    ctypes, kernel32, _, _, thread_entry = _windows_job_types()

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot in (None, -1):
        raise ContainmentFailed("could not enumerate threads to resume the child")
    try:
        entry = thread_entry()
        entry.dwSize = ctypes.sizeof(thread_entry)
        thread_id = None
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == process.pid:
                thread_id = entry.th32ThreadID
                break
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    if thread_id is None:
        raise ContainmentFailed(f"no thread found for pid {process.pid}")

    thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        raise ContainmentFailed(f"OpenThread failed ({ctypes.get_last_error()})")
    try:
        if kernel32.ResumeThread(thread) == _RESUME_FAILED:
            raise ContainmentFailed(f"ResumeThread failed ({ctypes.get_last_error()})")
    finally:
        kernel32.CloseHandle(thread)


def _open_tree(process: subprocess.Popen, job: Optional[int] = None) -> Optional[Any]:
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

    try:
        # The job was already created, configured and assigned before the child
        # was allowed to run - see `_contain_windows`. All that is left is the
        # per-descendant handle set, which is what answers 'is anything alive'.
        # The job is kept for TERMINATION, where one call ends everything in it;
        # it is deliberately not used for detection - see _WindowsTree.
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
        # Bounded, because this is the watchdog's last resort and a last resort
        # that can block forever is not one. taskkill itself waits on the
        # processes it signals, so a child wedged in an uninterruptible wait
        # takes this call down with it.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=GRACE_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
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
