"""The one process boundary the decoders and the capture both go through.

Real children, not doubles. The claims here are about what survives a bound -
processes, threads, memory - and a fake ``Popen`` can only assert that the code
called kill(), never that nothing is left running.

The defect these pin down: ``communicate()`` collects the whole reply and hands
it back at exit, so the byte ceiling was applied to a buffer that had already
been allocated. A helper that writes half a gigabyte inside its time limit passed
every check up to the moment the parent was holding half a gigabyte.
"""

import sys
import threading
import time

import pytest

from telegram_mcp.visual import bounded_process
from telegram_mcp.visual.bounded_process import (
    ProcessBudgetExhausted,
    ProcessCancelled,
    ProcessNotFound,
    ProcessOutputTooLarge,
    ProcessTimeout,
    run_bounded,
)

MEGABYTE = 1024 * 1024


def _child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


HANGS = "import time\nwhile True: time.sleep(0.05)\n"

FLOODS = (
    "import sys\n"
    "block = b'x' * 65536\n"
    "for _ in range(4096):\n"  # 256 MB if nothing stops it
    "    sys.stdout.buffer.write(block)\n"
    "sys.stdout.buffer.flush()\n"
)


def _run(command, **kwargs):
    defaults = {
        "label": "the helper",
        "timeout": 5.0,
        "max_output_bytes": MEGABYTE,
        "max_stderr_bytes": 64 * 1024,
    }
    defaults.update(kwargs)
    return run_bounded(command, **defaults)


@pytest.fixture(autouse=True)
def _no_threads_left():
    """Every bound has to leave the thread count where it found it."""
    before = threading.active_count()
    yield
    deadline = time.monotonic() + 30
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= before, [t.name for t in threading.enumerate()]


# --- it works at all ----------------------------------------------------------


def test_a_helper_that_finishes_hands_back_what_it_wrote():
    result = _run(_child("import sys; sys.stdout.buffer.write(b'hello'); sys.exit(0)"))
    assert result.returncode == 0
    assert result.stdout == b"hello"


def test_stderr_comes_back_separately():
    result = _run(_child("import sys; sys.stderr.buffer.write(b'diagnostic')"))
    assert result.stdout == b"" and result.stderr == b"diagnostic"


def test_a_flood_of_diagnostics_is_capped_without_sinking_the_answer():
    """stderr is attacker-influenced text that ends up in a log line, so it needs
    a bound - but it is a diagnostic, not the answer. Refusing on it would let a
    chatty decoder fail its own perfectly good frame; and blocking the pump would
    wedge the child on its own stderr and turn that into a timeout. So: capped,
    the excess discarded, and the truncation reported."""
    result = _run(
        _child("import sys; sys.stderr.buffer.write(b'z' * 500000); print('kept')"),
        max_stderr_bytes=1000,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"kept"
    assert len(result.stderr) == 1000 and result.stderr_truncated


def test_output_exactly_at_the_ceiling_is_a_complete_reply_not_an_overflow():
    result = _run(
        _child("import sys; sys.stdout.buffer.write(b'y' * 1000)"),
        max_output_bytes=1000,
    )
    assert result.stdout == b"y" * 1000


def test_a_missing_helper_is_named_rather_than_raising_oserror():
    with pytest.raises(ProcessNotFound, match="not installed"):
        _run(["telegram-mcp-no-such-helper-exists"])


# --- time ---------------------------------------------------------------------


def test_a_helper_that_never_returns_is_killed_inside_its_timeout():
    started = time.monotonic()
    with pytest.raises(ProcessTimeout, match="timed out"):
        _run(_child(HANGS), timeout=0.5)
    assert time.monotonic() - started < 10


def test_the_shared_deadline_beats_a_generous_per_call_timeout():
    """Ten frames at thirty seconds each is five minutes one caller can ask for."""
    started = time.monotonic()
    with pytest.raises(ProcessTimeout):
        _run(_child(HANGS), timeout=300.0, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - started < 10


def test_an_exhausted_deadline_starts_nothing_at_all(monkeypatch):
    started = []
    monkeypatch.setattr(
        bounded_process.subprocess,
        "Popen",
        lambda *a, **k: started.append(a) or pytest.fail("a helper was launched"),
    )
    with pytest.raises(ProcessBudgetExhausted, match="budget"):
        _run(_child(HANGS), deadline=time.monotonic() - 1)
    assert started == []


def test_a_timed_out_helper_is_gone_not_merely_abandoned():
    seen = {}
    original = bounded_process.subprocess.Popen

    def _remember(*args, **kwargs):
        process = original(*args, **kwargs)
        seen["process"] = process
        return process

    bounded_process.subprocess.Popen = _remember
    try:
        with pytest.raises(ProcessTimeout):
            _run(_child(HANGS), timeout=0.4)
    finally:
        bounded_process.subprocess.Popen = original

    assert seen["process"].poll() is not None, "the child outlived the call that started it"


# --- cancellation -------------------------------------------------------------


def test_a_cancelled_call_kills_the_helper_rather_than_waiting_it_out():
    cancelled = threading.Event()
    timer = threading.Timer(0.3, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ProcessCancelled, match="cancelled"):
            _run(_child(HANGS), timeout=300.0, cancelled=cancelled)
    finally:
        timer.cancel()
        timer.join(timeout=5)
    assert time.monotonic() - started < 10


def test_a_cancellation_already_set_is_noticed_at_once():
    cancelled = threading.Event()
    cancelled.set()
    started = time.monotonic()
    with pytest.raises(ProcessCancelled):
        _run(_child(HANGS), timeout=300.0, cancelled=cancelled)
    assert time.monotonic() - started < 5


# --- bytes, bounded DURING the run --------------------------------------------


def test_a_flood_is_stopped_mid_write_rather_than_measured_afterwards():
    """The whole point: the helper must not be allowed to finish writing 256 MB.

    ``communicate()`` would have returned every byte and then compared the length
    to the ceiling, which reports the overflow from inside it.
    """
    started = time.monotonic()
    with pytest.raises(ProcessOutputTooLarge, match="ceiling"):
        _run(_child(FLOODS), timeout=60.0, max_output_bytes=MEGABYTE)
    # 256 MB through a pipe takes appreciably longer than the megabyte that was
    # allowed, so finishing quickly is itself evidence the write was cut short.
    assert time.monotonic() - started < 30


def test_the_flooding_helper_does_not_survive_the_refusal():
    seen = {}
    original = bounded_process.subprocess.Popen

    def _remember(*args, **kwargs):
        process = original(*args, **kwargs)
        seen["process"] = process
        return process

    bounded_process.subprocess.Popen = _remember
    try:
        with pytest.raises(ProcessOutputTooLarge):
            _run(_child(FLOODS), timeout=60.0, max_output_bytes=65536)
    finally:
        bounded_process.subprocess.Popen = original

    assert seen["process"].poll() is not None


def test_the_sink_stops_at_its_ceiling_and_says_so():
    sink = bounded_process._Sink(10)
    assert sink.feed(b"12345") is True and sink.overflowed is False
    assert sink.feed(b"67890") is True and sink.overflowed is False
    assert sink.feed(b"x") is False and sink.overflowed is True
    assert sink.value() == b"1234567890"


def test_the_sink_keeps_the_part_that_fits_of_an_oversized_chunk():
    sink = bounded_process._Sink(4)
    assert sink.feed(b"abcdefgh") is False
    assert sink.value() == b"abcd" and sink.overflowed


# --- nothing is left behind ---------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["timeout", "cancel", "overflow", "success"],
)
def test_no_reader_thread_survives_any_exit_path(case):
    before = threading.active_count()
    cancelled = threading.Event()
    if case == "cancel":
        cancelled.set()
    try:
        if case == "success":
            _run(_child("import sys; sys.stdout.buffer.write(b'ok')"))
        elif case == "overflow":
            _run(_child(FLOODS), timeout=60.0, max_output_bytes=65536)
        elif case == "cancel":
            _run(_child(HANGS), timeout=60.0, cancelled=cancelled)
        else:
            _run(_child(HANGS), timeout=0.4)
    except bounded_process.ProcessError:
        pass

    deadline = time.monotonic() + 30
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() == before, [t.name for t in threading.enumerate()]


def test_both_pipes_are_closed_once_the_call_returns():
    seen = {}
    original = bounded_process.subprocess.Popen

    def _remember(*args, **kwargs):
        process = original(*args, **kwargs)
        seen["process"] = process
        return process

    bounded_process.subprocess.Popen = _remember
    try:
        _run(_child("import sys; sys.stdout.buffer.write(b'ok')"))
    finally:
        bounded_process.subprocess.Popen = original

    assert seen["process"].stdout.closed and seen["process"].stderr.closed


def test_a_helper_never_inherits_the_parents_stdin():
    """An unexpected prompt would otherwise block for ever on a terminal nobody
    is watching, which is a hang with no bound at all."""
    result = _run(
        _child("import sys; sys.stdout.buffer.write(str(len(sys.stdin.read())).encode())")
    )
    assert result.stdout == b"0"


def test_an_exception_from_anywhere_still_reaps_the_child(monkeypatch):
    """Not only the bounds this module raises itself. A KeyboardInterrupt, or a
    bug three lines further down, must still leave no child behind."""
    seen = {}
    original = bounded_process.subprocess.Popen

    def _remember(*args, **kwargs):
        process = original(*args, **kwargs)
        seen["process"] = process
        return process

    monkeypatch.setattr(bounded_process.subprocess, "Popen", _remember)

    class _Detonates:
        def is_set(self):
            raise KeyboardInterrupt("the operator gave up")

    with pytest.raises(KeyboardInterrupt):
        _run(_child(HANGS), timeout=60.0, cancelled=_Detonates())
    assert seen["process"].poll() is not None


def test_the_helper_runs_without_a_shell(tmp_path):
    """argv, not a command line: a value with a space or a quote must arrive as
    one argument rather than becoming two, or an injection point."""
    awkward = 'one two "three" & echo pwned'
    result = _run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.argv[1].encode())",
            awkward,
        ]
    )
    assert result.stdout.decode() == awkward


def test_a_nonzero_exit_is_reported_rather_than_raised():
    """A helper that ran and refused is an answer; only a broken bound is an error."""
    result = _run(_child("import sys; sys.stderr.write('nope'); sys.exit(3)"))
    assert result.returncode == 3 and b"nope" in result.stderr
