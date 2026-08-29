"""Every timing argument on the event path has a floor, a ceiling, and a refusal.

The defect these pin down: ``wait_for_new_message``, ``wait_for_settled_message``
and ``enable_incoming_feed`` each took a raw number and did arithmetic with it.
``timeout=inf`` produced ``deadline = inf``; ``remaining <= 0`` was never true and
``asyncio.wait_for(..., timeout=inf)`` waits for ever, so the only thing that ever
ended that call was cancellation from outside. ``nan`` is worse: every comparison
against it is False, so the call claims a bound it does not have.

The lock has the same shape one layer down - a grace period with no maximum, and
a poll interval that could be zero, which is a busy-spin dressed up as a wait.
"""

import asyncio
import base64
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from telegram_mcp import paging, runner
from telegram_mcp.runtime import ValidationError
from telegram_mcp.singleton import SessionLock
from telegram_mcp.tools import events

# One list, used against every timing argument: what a caller must never be able
# to turn into an unbounded wait.
UNUSABLE = [
    math.inf,
    -math.inf,
    math.nan,
    -1,
    True,
    False,
    "soon",
    None,
    10**18,
]


def _pump_lines(stream, sink) -> None:
    """Hand the watcher's stdout to the test without blocking it on readline."""
    for line in stream:
        sink.put(line)


async def _bounded(coroutine, seconds: float = 5.0):
    """Await a tool call that is supposed to refuse, without hanging the suite.

    An unbounded timeout is exactly the defect under test, so the assertion has
    to be "this returned" rather than "this eventually returned": without the
    wrapper a regression here stalls the whole run instead of failing one test.
    """
    try:
        return await asyncio.wait_for(coroutine, timeout=seconds)
    except asyncio.TimeoutError:
        pytest.fail(f"the call did not return within {seconds}s; its bound is not a bound")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(events, "_pending_msgs", {})
    monkeypatch.setattr(events, "_feed_task", None)
    monkeypatch.setattr(events, "_activity_event", None)
    monkeypatch.setattr(events, "_feed_settle_ms", 6000)
    monkeypatch.setattr(events, "_feed_autostart_done", False)
    monkeypatch.setenv("TELEGRAM_EVENT_FEED_FILE", str(tmp_path / "feed.jsonl"))
    yield
    task = events._feed_task
    if task is not None:
        task.cancel()


# --- the shared validator ----------------------------------------------------


@pytest.mark.parametrize("value", UNUSABLE)
def test_the_validator_refuses_everything_that_is_not_a_usable_duration(value):
    span = paging.bounded_number(value, "timeout")
    assert span.error, f"{value!r} was accepted"
    assert "timeout" in span.error


def test_the_validator_names_its_own_floor_and_ceiling():
    low, high = paging.TIMING_BOUNDS["timeout"]
    assert paging.bounded_number(low, "timeout").error is None
    assert paging.bounded_number(high, "timeout").error is None
    assert paging.bounded_number(low / 2, "timeout").error
    assert paging.bounded_number(high * 2, "timeout").error


def test_zero_is_refused_where_zero_would_spin_and_allowed_where_it_means_now():
    # A zero poll interval is a busy loop; a zero grace is "do not wait at all",
    # which is a legitimate thing to ask for.
    assert paging.bounded_number(0, "lock_poll_interval").error
    assert paging.bounded_number(0, "lock_grace_seconds").error is None


def test_every_timing_argument_the_event_path_takes_has_a_documented_bound():
    for name in (
        "timeout",
        "settle_ms",
        "max_wait_ms",
        "lock_grace_seconds",
        "lock_poll_interval",
    ):
        low, high = paging.TIMING_BOUNDS[name]
        assert math.isfinite(low) and math.isfinite(high) and low < high


# --- the three tools ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", UNUSABLE)
async def test_wait_for_new_message_refuses_an_unusable_timeout(value):
    refusal = await _bounded(events.wait_for_new_message(timeout=value))
    assert "timeout" in refusal
    assert "pending_chats" not in refusal


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["settle_ms", "max_wait_ms"])
@pytest.mark.parametrize("value", UNUSABLE)
async def test_wait_for_settled_message_refuses_an_unusable_wait(field, value):
    kwargs = {"settle_ms": 50, "max_wait_ms": 100, field: value}
    refusal = await _bounded(events.wait_for_settled_message(**kwargs))
    assert field in refusal
    assert '"event": true' not in refusal.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", UNUSABLE)
async def test_enable_incoming_feed_refuses_an_unusable_settle_and_starts_nothing(value):
    refusal = await _bounded(events.enable_incoming_feed(settle_ms=value))
    assert "settle_ms" in refusal
    assert not events.feed_enabled()


@pytest.mark.asyncio
async def test_the_bounds_are_shared_rather_than_re_stated_per_tool():
    # The same out-of-range number must be refused by all three, or the "one
    # shared rule" is three rules that happen to agree today.
    over = paging.TIMING_BOUNDS["settle_ms"][1] + 1
    assert "settle_ms" in await _bounded(events.wait_for_settled_message(settle_ms=over))
    assert "settle_ms" in await _bounded(events.enable_incoming_feed(settle_ms=over))


@pytest.mark.asyncio
async def test_a_wait_inside_its_bounds_still_works():
    result = json.loads(await events.wait_for_new_message(timeout=0.2))
    assert result["event"] is False and result["reason"] == "timeout"


# --- the lock ----------------------------------------------------------------


def test_the_lock_refuses_a_grace_period_with_no_end(tmp_path):
    lock = SessionLock("identity", lock_dir=tmp_path)
    for value in (math.inf, math.nan, -1, 10**18):
        with pytest.raises(ValueError):
            lock.acquire(grace_seconds=value)


def test_the_lock_refuses_a_poll_interval_that_would_busy_spin(tmp_path):
    lock = SessionLock("identity", lock_dir=tmp_path)
    for value in (0, -1, math.nan, math.inf, 10**18):
        with pytest.raises(ValueError):
            lock.acquire(grace_seconds=1, poll_interval=value)


def test_a_bounded_wait_still_gives_up_and_says_so(tmp_path):
    held = SessionLock("identity", lock_dir=tmp_path)
    held.acquire(grace_seconds=0)
    try:
        second = SessionLock("identity", lock_dir=tmp_path)
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            second.acquire(grace_seconds=0.3, poll_interval=0.05)
        elapsed = time.monotonic() - started
        assert 0.2 < elapsed < 5, elapsed
        assert "already connected" in str(caught.value)
    finally:
        held.release()


@pytest.mark.parametrize("value", ["inf", "nan", "-1", "1e18", "soon"])
def test_the_configured_grace_period_refuses_anything_unusable(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_LOCK_GRACE_SECONDS", value)
    with pytest.raises(ValidationError):
        runner._lock_grace_seconds()


def test_the_configured_grace_period_accepts_a_real_number(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LOCK_GRACE_SECONDS", "2.5")
    assert runner._lock_grace_seconds() == 2.5


# --- feed protection by object identity, not by pathname ---------------------


def test_a_feed_file_replaced_at_the_same_path_is_protected_again(monkeypatch, tmp_path):
    """The cache keyed on the PATHNAME, so a new object at the same name was
    treated as already-protected and never hardened."""
    path = tmp_path / "feed.jsonl"
    monkeypatch.setenv("TELEGRAM_EVENT_FEED_FILE", str(path))

    events._touch_feed_file()
    hardened = []
    if os.name == "nt":
        real = events.restrict_to_owner_strict
        monkeypatch.setattr(
            events,
            "restrict_to_owner_strict",
            lambda target: hardened.append(str(target)) or real(target),
        )
    else:
        # POSIX hardens the DESCRIPTOR it just opened, so the Windows entry
        # point is never reached and watching it proved nothing here.
        real_fchmod = os.fchmod
        monkeypatch.setattr(
            os,
            "fchmod",
            lambda fd, mode: hardened.append(mode) or real_fchmod(fd, mode),
        )

    # A file at the same name, but a different object: exactly what an external
    # rotation, an editor's write-new-then-rename, or a hostile replacement does.
    path.unlink()
    path.write_text("planted\n", encoding="utf-8")
    if os.name != "nt":
        # Whatever the runner's umask, the replacement starts genuinely readable,
        # so "was it hardened again" has a determinate answer.
        os.chmod(path, 0o644)
    assert not events.verify_owner_only(path), "the replacement was already private"

    events._touch_feed_file()

    assert hardened, "the replacement object was never re-protected"
    assert events.verify_owner_only(path)


def test_the_open_descriptor_and_the_name_must_be_the_same_object(monkeypatch, tmp_path):
    """A name and an open descriptor are two different claims about a file.

    ``st_dev``/``st_ino`` are the identity on both platforms, so this is what
    distinguishes "the file I opened" from "whatever answers to that name now".
    """
    import os

    one = tmp_path / "feed.jsonl"
    one.write_text("one\n", encoding="utf-8")
    two = tmp_path / "other.jsonl"
    two.write_text("two\n", encoding="utf-8")

    fd = os.open(one, os.O_RDONLY)
    try:
        assert events._same_object(one, fd)
        assert not events._same_object(two, fd)
        assert not events._same_object(tmp_path / "gone.jsonl", fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "nt", reason="the POSIX branch hardens the descriptor itself")
def test_hardening_a_name_while_holding_a_different_object_is_refused(tmp_path):
    """Windows cannot chmod a descriptor, so the DACL is written through the NAME.

    That is the whole exposure: the call succeeds against whatever the name
    points at, which need not be the file that was opened. It has to refuse
    rather than report success about an object it never touched.
    """
    import os

    target = tmp_path / "feed.jsonl"
    target.write_text("target\n", encoding="utf-8")
    other = tmp_path / "other.jsonl"
    other.write_text("other\n", encoding="utf-8")

    fd = os.open(other, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="no longer refers"):
            events._restrict_to_owner(target, fd)
    finally:
        os.close(fd)

    fd = os.open(target, os.O_RDONLY)
    try:
        events._restrict_to_owner(target, fd)  # the same object is accepted
    finally:
        os.close(fd)
    assert events.verify_owner_only(target)


def test_protection_is_re_checked_rather_than_remembered(monkeypatch, tmp_path):
    """No pathname cache: the state of the file decides, every time."""
    assert not hasattr(events, "_owner_only_paths")


# --- a watcher that works on this platform ------------------------------------


def test_the_watch_command_is_usable_in_this_shell():
    state = events.incoming_feed_state()
    command = state["watch_command"]
    if sys.platform == "win32":
        assert "tail" not in command and "grep" not in command
        assert "powershell" in command.lower() or "pwsh" in command.lower()
    else:
        assert command.startswith("tail -n 0 -F ")


def test_the_one_chat_watch_command_filters_in_the_same_shell():
    state = events.incoming_feed_state()
    command = state["watch_command_for_one_chat"]
    if sys.platform == "win32":
        assert "grep" not in command
        # The filter lives inside -EncodedCommand now, so the readable form is
        # what carries it - and the encoded one has to decode back to exactly
        # that, or the two have drifted and users are shown one and run another.
        script = state["watch_script_for_one_chat"]
        assert "chat_id" in script
        encoded = command.split()[-1]
        assert base64.b64decode(encoded).decode("utf-16-le") == script


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell watcher is the Windows one")
def test_the_powershell_watcher_survives_a_rotation(monkeypatch, tmp_path):
    """A watcher that stops at the first rotation is not a monitor.

    ``_rotate_feed_if_needed`` uses ``os.replace``, so after a rotation the name
    points at a brand-new empty file. Anything that follows the DESCRIPTOR keeps
    reading the rotated generation for ever; the watcher has to follow the name.
    """
    import os

    path = tmp_path / "feed.jsonl"
    monkeypatch.setenv("TELEGRAM_EVENT_FEED_FILE", str(path))
    path.write_text('{"before": 1}\n', encoding="utf-8")

    # The EXACT command the state reports, not a reconstruction of it. Running
    # `_watch_script` directly is what let the quoting defect live: the string
    # handed to users wrapped the script in double quotes, so the shell they
    # pasted it into expanded $p and $o before the child ever saw them.
    #
    # `.split()` is exact here because -EncodedCommand is base64: four bare
    # tokens, no quotes, nothing for a shell or for this test to re-interpret.
    command = events.incoming_feed_state()["watch_command"]
    process = subprocess.Popen(
        command.split(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert "powershell" in command.lower()
    lines: "queue.Queue[str]" = queue.Queue()
    reader = threading.Thread(target=_pump_lines, args=(process.stdout, lines), daemon=True)
    reader.start()
    assert "$" not in command, "the command still carries something a shell can expand"

    def _await_line(marker: str, seconds: float = 30.0) -> None:
        """Wait for one specific line, bounded, instead of sleeping and hoping."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if marker in line:
                return
        pytest.fail(f"the watcher never emitted a line containing {marker!r}")

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"first": 1}\n')
        # Rotation only proves something once the watcher is demonstrably reading
        # the pre-rotation file, so this waits for that rather than assuming it.
        _await_line('"first"')

        # Rotate the way the feed does, then append to the fresh generation.
        rotated = Path(str(path) + ".1")
        os.replace(path, rotated)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"after": 1}\n')

        _await_line('"after"')
    finally:
        process.kill()
        process.communicate(timeout=30)
        reader.join(timeout=30)
    assert process.poll() is not None
    assert not reader.is_alive()
