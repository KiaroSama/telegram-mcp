"""Run one untrusted helper in a child process that cannot outlive its bounds.

Every decoder and every capture in this package is native code the caller does
not control. Called in-process it runs on a worker thread, and Python cannot
interrupt a thread from outside: a single native call that does not return holds
the work past every deadline with the caller's cancellation flag unread. The
.tgs renderer proved that with a 332-byte file, and PrintWindow is documented as
synchronous with no promise of returning promptly. In a child process the same
hang is a PID, and a PID can be killed - so this is the one boundary the whole
package goes through, rather than a second mechanism per decoder.

Three bounds, all of them enforced while the child runs rather than after it
exits:

**Time.** A per-call timeout AND a shared request deadline; the smaller wins.
Ten frames each allowed thirty seconds is five minutes one caller can ask for,
so a per-call limit alone does not bound a request.

**Cancellation.** A ``threading.Event`` the caller sets when it stops waiting.
The wait is sliced rather than blocking, so the flag is seen within one poll and
the child is killed instead of burning CPU for an answer nobody will read.

**Bytes.** ``communicate()`` collects the whole reply and hands it over at exit,
so a ceiling applied to its return value bounds what the caller *keeps*, not what
the machine *held*: a hostile helper that writes a gigabyte inside its time limit
has already allocated a gigabyte by the time the check runs. Here two reader
threads copy into capped sinks and stop at the ceiling, which leaves the child
blocked on a full pipe - and blocked is fine, because the next poll kills it.

On every exit path - success, timeout, cancellation, overflow, or an exception
from anywhere - the child is killed and reaped, both pipes are closed and both
reader threads are joined before this returns. Nothing survives the call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

# Read size for the pipe pumps. Large enough that a megabyte costs sixteen reads,
# small enough that the byte ceiling cannot be overshot by much.
_CHUNK_BYTES = 64 * 1024

# How long a reader is given to finish after the child is gone. The pipes are at
# EOF by then, so this is the cost of one final read, not a wait for the helper.
_READER_JOIN_SECONDS = 30.0

# Prefix for the pipe-pump threads, so a thread dump names them.
READER_NAME = "bounded-process-reader"


class ProcessError(RuntimeError):
    """A bounded helper did not produce a usable answer."""


class ProcessBudgetExhausted(ProcessError):
    """The shared deadline had already passed, so nothing was started."""


class ProcessTimeout(ProcessError):
    """The helper outlived its bound and was killed."""


class ProcessCancelled(ProcessError):
    """The caller stopped waiting, so the helper was killed."""


class ProcessOutputTooLarge(ProcessError):
    """The helper wrote past its byte ceiling and was killed mid-write."""


class ProcessNotFound(ProcessError):
    """The helper is not installed or not on PATH."""


@dataclass
class Completed:
    """What a helper that finished on its own produced."""

    args: list
    returncode: int
    stdout: bytes
    stderr: bytes
    stderr_truncated: bool = False


class _Sink:
    """A byte buffer that stops at its ceiling and remembers that it did.

    ``overflowed`` means the helper had more to say than the ceiling allows -
    exactly-at-the-ceiling is a complete reply, not an overflow, so a helper whose
    output is the ceiling to the byte is still accepted.

    ``drain_past_limit`` is the difference between the two streams. stdout is the
    ANSWER, so overflowing it is fatal and the pump stops - which blocks the child
    on a full pipe and gets it killed, and that is the point. stderr is only a
    diagnostic: discarding the excess keeps memory bounded just as well, while
    stopping the pump would block a perfectly good decode on its own chatter and
    turn a successful frame into a timeout.
    """

    __slots__ = ("chunks", "drain_past_limit", "limit", "overflowed", "total")

    def __init__(self, limit: int, drain_past_limit: bool = False) -> None:
        self.chunks: list[bytes] = []
        self.drain_past_limit = drain_past_limit
        self.limit = limit
        self.total = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> bool:
        """Append what fits; the return value says whether to keep reading."""
        room = self.limit - self.total
        if len(chunk) <= room:
            self.chunks.append(chunk)
            self.total += len(chunk)
            return True
        if room > 0:
            self.chunks.append(chunk[:room])
            self.total = self.limit
        self.overflowed = True
        return self.drain_past_limit

    def value(self) -> bytes:
        return b"".join(self.chunks)


def _pump(stream, sink: _Sink) -> None:
    """Copy one pipe into its sink until EOF, the ceiling, or the pipe closes."""
    try:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                return
            if not sink.feed(chunk):
                # Deliberately stop reading. The child blocks on a full pipe,
                # which is what makes the ceiling a bound on memory rather than a
                # report about it; the poll loop kills it on the next pass.
                return
    except (OSError, ValueError):
        # The pipe was closed underneath us during cleanup. Nothing to add.
        return


# Windows: a job object whose closure kills everything in it. `process.kill()`
# ends the DIRECT child and nothing else, so an ffmpeg that had spawned a helper
# of its own left that helper running after a timeout or a cancellation - past
# the deadline, past the caller, and past the point anyone was reading the answer.
#
# POSIX gets the same guarantee from a session: `start_new_session` puts the child
# in its own process group and `killpg` ends the group.
_JOB_OBJECT_EXTENDED_LIMIT = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_RESUME_FAILED = 0xFFFFFFFF

_WIN32: "Optional[tuple]" = None


def _win32():
    """The job and thread calls, bound once. Windows only.

    Every function gets an explicit restype/argtypes: without them ctypes assumes
    C int and truncates a 64-bit HANDLE to 32 bits, which works until the machine
    is busy enough to hand out a large one. This project has been bitten by that
    exact truncation before.
    """
    global _WIN32
    if _WIN32 is not None:
        return _WIN32

    import ctypes
    from ctypes import wintypes

    class _BASIC_LIMITS(ctypes.Structure):
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

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _THREADENTRY32(ctypes.Structure):
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
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    _WIN32 = (ctypes, kernel32, _EXTENDED_LIMITS, _THREADENTRY32)
    return _WIN32


def _contain(process) -> "Optional[int]":
    """Put a SUSPENDED child in a kill-on-close job. Returns the job handle.

    None means this platform or this Python gave nothing to contain with, which
    the caller reports rather than treating as containment.
    """
    if os.name != "nt":
        return None
    handle = getattr(process, "_handle", None)
    if handle is None:
        return None
    ctypes, kernel32, extended, _thread = _win32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = extended()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not kernel32.AssignProcessToJobObject(job, int(handle)):
        kernel32.CloseHandle(job)
        return None
    return job


def _resume(process) -> bool:
    """Let a contained child start. ``Popen`` closed the thread handle, so the
    one thread a suspended process has is found again by owner PID."""
    if os.name != "nt":
        return True
    ctypes, kernel32, _extended, thread_entry = _win32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot in (None, -1):
        return False
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
        return False
    thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        return False
    try:
        return kernel32.ResumeThread(thread) != _RESUME_FAILED
    finally:
        kernel32.CloseHandle(thread)


def _end_tree(process, job) -> None:
    """End the child AND anything it started, then release the job.

    ``process.kill()`` alone ends the direct child, so a helper that ffmpeg
    spawned outlived every timeout and cancellation this module reports.
    """
    if job is not None:
        ctypes, kernel32, _extended, _thread = _win32()
        try:
            kernel32.TerminateJobObject(job, 1)
        except OSError:  # pragma: no cover - the job is already gone
            pass
        kernel32.CloseHandle(job)
        return
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


# What a decoder legitimately needs in order to start, and nothing else.
#
# The workers take their whole input as argv and read no environment variable at
# all - verified across pillow_worker, lottie_worker and capture_worker - so the
# inherited environment was pure accident. What it accidentally included was the
# login: TELEGRAM_API_HASH and TELEGRAM_SESSION_STRING* are loaded by
# load_dotenv() in runtime.py and live in os.environ for the process lifetime.
#
# These children are the ones that decode ATTACKER-SUPPLIED bytes. The job object
# bounds what a compromised decoder can DO; it does nothing about what the
# decoder can READ out of its own environ. Without this, a memory-safety failure
# in Pillow or a Lottie parser turns from "denial of service, contained" into
# "the account".
#
# PATH finds ffmpeg. SYSTEMROOT is not optional on Windows: a child started
# without it fails in ways that look like a corrupted Python install.
_CHILD_ENV_KEYS = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "VIRTUAL_ENV",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)


def child_environment() -> dict:
    """The parent's environment reduced to what a helper needs to start.

    An allowlist rather than a denylist: a new secret added to the server's
    environment must not reach the decoders by default. A helper that genuinely
    needs a variable gets it added here deliberately - and one that needs a
    TELEGRAM_* value would be a design error worth stopping on.
    """
    return {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}


def run_bounded(
    command: list[str],
    *,
    label: str,
    timeout: float,
    max_output_bytes: int,
    max_stderr_bytes: int,
    deadline: Optional[float] = None,
    cancelled: Optional[threading.Event] = None,
    poll_seconds: float = 0.1,
) -> Completed:
    """Run ``command`` under a time, cancellation and byte bound, and reap it.

    Args:
        command: argv, run without a shell.
        label: what to call this helper in an error message.
        timeout: seconds this one call may take.
        max_output_bytes: ceiling on buffered stdout, enforced during the run.
        max_stderr_bytes: ceiling on buffered stderr, same.
        deadline: a ``time.monotonic()`` value shared with the caller. The
            effective bound is the smaller of this and ``timeout``.
        cancelled: set by a caller that has stopped waiting.
        poll_seconds: how often the bounds above are re-checked.

    Raises:
        ProcessBudgetExhausted, ProcessTimeout, ProcessCancelled,
        ProcessOutputTooLarge, ProcessNotFound - all subclasses of ProcessError.
    """
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessBudgetExhausted(
                f"{label} was not started: the request had already run out of its time "
                "budget. Ask for less in one call."
            )
        timeout = min(timeout, remaining)

    try:
        extras: dict = {}
        if os.name == "nt":
            # Suspended, so the job holds it before it runs: contained afterwards,
            # anything it spawned in that window is outside the job and survives
            # the kill.
            extras["creationflags"] = _CREATE_SUSPENDED
        else:
            extras["start_new_session"] = True
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Never inherit the parent's stdin: an unexpected prompt in a helper
            # would block for ever on a terminal nothing is watching.
            stdin=subprocess.DEVNULL,
            # Not the parent's environment: see _CHILD_ENV_KEYS above.
            env=child_environment(),
            **extras,
        )
    except FileNotFoundError as error:
        raise ProcessNotFound(
            f"{os.path.basename(command[0])} is not installed or not on PATH."
        ) from error

    job = _contain(process)
    if not _resume(process):
        # A suspended child nobody resumed is a hang, so it does not get to
        # stand. Reported as a failure to start rather than as a timeout later.
        _end_tree(process, job)
        raise ProcessNotFound(
            f"{os.path.basename(command[0])} could not be started under containment."
        )

    out = _Sink(max_output_bytes)
    err = _Sink(max_stderr_bytes, drain_past_limit=True)
    # Named, so "is anything from this call still running?" is answerable from a
    # thread dump - by an operator reading a hung server, and by the tests that
    # assert nothing survives a bound.
    readers = [
        threading.Thread(target=_pump, args=(stream, sink), daemon=True, name=f"{READER_NAME}-{n}")
        for n, (stream, sink) in enumerate(((process.stdout, out), (process.stderr, err)))
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    try:
        while True:
            try:
                # wait() rather than sleep(): a helper that finishes early is
                # noticed immediately instead of at the next tick.
                process.wait(timeout=poll_seconds)
            except subprocess.TimeoutExpired:
                pass
            if out.overflowed:
                raise ProcessOutputTooLarge(
                    f"{label} wrote past the {max_output_bytes}-byte ceiling for one call "
                    "and was stopped mid-write."
                )
            if cancelled is not None and cancelled.is_set():
                raise ProcessCancelled(f"{label} was cancelled by the caller and terminated.")
            if process.poll() is not None:
                break
            if time.monotonic() - started > timeout:
                raise ProcessTimeout(f"{label} timed out after {timeout:g}s and was terminated.")

        _finish(process, readers)
        # Checked once more after the readers have drained: a helper can exit
        # having already written more than the ceiling into the pipe buffer.
        if out.overflowed:
            raise ProcessOutputTooLarge(
                f"{label} produced more than the {max_output_bytes}-byte ceiling for one call."
            )
        result = Completed(
            list(command), process.returncode, out.value(), err.value(), err.overflowed
        )
        # Closing the job kills whatever is still in it, which after a clean exit
        # is exactly the helpers the child left behind. One handle per call would
        # otherwise leak for the life of the server.
        if job is not None:
            _win32()[1].CloseHandle(job)
            job = None
        return result
    except BaseException:
        # Timeout, cancellation, overflow, or anything at all: the child must not
        # outlive the call that started it, and neither may its readers - nor
        # anything the child spawned, which `process.kill()` alone left running.
        _end_tree(process, job)
        _finish(process, readers)
        raise


# How long a cancelled worker gets to unwind before the caller stops waiting for
# it. Not load-bearing: every helper the workers run is a child that run_bounded
# kills within one poll of the flag being set, so what is left is unwinding. It
# is finite anyway, because a drain that could block for ever would hand a wedged
# decoder the power to hold up the canceller too - which is the failure being
# cancelled in the first place.
CANCEL_DRAIN_SECONDS = 5.0


async def run_cancellable(work, *arguments):
    """Run one blocking call off the event loop so cancelling it reaches the work.

    ``asyncio.to_thread`` alone is not enough. Cancelling the awaiting coroutine
    raises in the caller and frees it, but the worker thread keeps running: Python
    cannot stop a thread from outside, and a ``concurrent.futures`` job that has
    already started cannot be cancelled either. So the flag is how the thread gets
    told, and ``run_bounded`` in the thread is what makes being told sufficient.

    ``work`` is called as ``work(*arguments, cancelled)``.

    Every caller goes through here rather than reaching for ``asyncio.to_thread``
    itself, so the rule exists once instead of at each call site free to forget it.
    """
    cancelled = threading.Event()
    loop = asyncio.get_running_loop()
    # run_in_executor rather than to_thread: the future has to outlive the await,
    # so that cancelling the await does not throw away the handle on the worker.
    worker = loop.run_in_executor(None, work, *arguments, cancelled)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        await drain(worker)
        raise


async def drain(worker) -> None:
    """Wait for a cancelled worker to finish unwinding, and say so if it does not.

    Every await in a cancelled task raises ``CancelledError`` immediately, so the
    wait cannot be an await on the worker itself. A callback plus a plain
    ``threading.Event`` is not subject to that: the loop keeps running, and this
    returns as soon as the worker really is done.
    """
    finished = threading.Event()
    worker.add_done_callback(lambda _future: finished.set())
    deadline = time.monotonic() + CANCEL_DRAIN_SECONDS
    while not finished.is_set() and time.monotonic() < deadline:
        # Yields to the loop without awaiting anything cancellable.
        await asyncio.sleep(0)
        finished.wait(0.02)
    if not finished.is_set():
        # Imported here, not at module scope: the decoder workers import this
        # package's modules, and the whole point of a worker is that it costs the
        # decoder and nothing else.
        from telegram_mcp.safe_log import log_event

        log_event(
            logging.WARNING,
            "worker-outlived-cancellation-drain",
            seconds=CANCEL_DRAIN_SECONDS,
        )
        return
    # Read the result nobody is going to look at. The worker ends in the
    # cancellation error - that is the cancellation working - and an unretrieved
    # future exception makes asyncio log it at ERROR every time, which turns
    # correct behaviour into an alarm in the operator's log.
    worker.exception()


def _finish(process, readers) -> None:
    """Reap the child, drain and close both pipes, and join both readers.

    Order matters. ``wait()`` first so the child is gone and the write ends are
    closed by the OS; the readers then see EOF and return on their own. Closing
    the pipes first would race them into a ValueError instead.
    """
    try:
        process.wait()
    except OSError:
        pass
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    still_running = [reader for reader in readers if reader.is_alive()]
    if still_running:  # pragma: no cover - the child is dead, so the pipes are at EOF
        raise ProcessError(
            f"{len(still_running)} output reader(s) did not stop after the helper was "
            "reaped; refusing to report a clean result while a thread is still live."
        )
