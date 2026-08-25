"""What one preview call may spend, and what may still be running when it ends.

Two defects here, and they are the same mistake in two places.

**The budget was charged after the fact.** ``PreviewLedger.charge`` ran once a
document had already produced its bytes, so with a batch running in parallel
every document produced before any of them accounted for it. Reproduced against
the shipped code: two 8-byte outputs against a 10-byte ceiling ended at 16. An
allowance has to be taken before the work starts, or it is a receipt rather than
a budget.

**Pillow ran in-process.** Both the still path and the animated path decoded on a
worker thread, where the budget could only be checked BETWEEN frames - and a
single Pillow call that does not return never reaches the next frame. Python
cannot stop a thread from outside, so there was no bound at all on one decode.
The .tgs renderer already solved this by moving into a child process; these
require the same of every other decoder.
"""

import asyncio
import concurrent.futures
import io
import sys
import threading
import time

import pytest
from PIL import Image

from telegram_mcp import media_preview
from telegram_mcp.media_preview import (
    MAX_CALL_PREVIEW_BYTES,
    PreviewLedger,
    encode_frames_cancellable,
    encode_still_cancellable,
)
from telegram_mcp.visual import bounded_process, frames
from telegram_mcp.visual.frames import FrameExtractionError


def _png(size=(8, 8), colour=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _animated_gif(frame_count=4) -> bytes:
    buffer = io.BytesIO()
    first, *rest = [
        Image.new("RGB", (16, 16), (index * 40 % 255, 90, 140)) for index in range(frame_count)
    ]
    first.save(buffer, format="GIF", save_all=True, append_images=rest, duration=60, loop=0)
    return buffer.getvalue()


def _watch_children(monkeypatch):
    """Every child the bounded runner starts, so a test can check it is gone."""
    created = []
    original = bounded_process.subprocess.Popen

    def _factory(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append((args[0] if args else kwargs.get("args"), process))
        return process

    monkeypatch.setattr(bounded_process.subprocess, "Popen", _factory)
    return created


def _reader_threads() -> list:
    """The bounded runner's pipe pumps, by name. Empty means no decode is live."""
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(bounded_process.READER_NAME) and thread.is_alive()
    ]


def _never_returns(*_args, **_kwargs) -> list:
    """A decoder command that hangs, which no real image can be relied on to do."""
    return [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)\n"]


async def _until(condition, message: str, seconds: float = 30.0) -> None:
    """Wait for a condition with a deadline, rather than sleeping and hoping."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail(message)


@pytest.fixture(autouse=True)
def _no_reader_left():
    """No pipe pump may outlive a test: one still alive is a decode still running."""
    yield
    deadline = time.monotonic() + 30
    while _reader_threads() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _reader_threads() == []


# --- the allowance is taken first ---------------------------------------------


def test_the_ledger_hands_out_shares_that_add_up_to_no_more_than_the_total():
    ledger = PreviewLedger(shares=4, total=100)
    taken = [ledger.reserve() for _ in range(4)]
    assert sum(reservation.allowance for reservation in taken) <= 100


def test_two_documents_cannot_both_produce_against_the_same_ceiling():
    """The reproduced case: two 8-byte outputs against a 10-byte ceiling ended at 16."""
    ledger = PreviewLedger(shares=2, total=10)
    first, second = ledger.reserve(), ledger.reserve()

    assert first.allowance + second.allowance <= 10
    for reservation in (first, second):
        with pytest.raises(FrameExtractionError, match="budget"):
            reservation.settle(8)
    assert ledger.spent <= 10


def test_an_allowance_is_outstanding_while_the_work_runs_not_only_after_it():
    ledger = PreviewLedger(shares=1, total=100)
    reservation = ledger.reserve()
    assert ledger.available == 0, "the pool still showed room for a second full document"
    reservation.settle(10)
    assert ledger.spent == 10
    assert ledger.available == 90, "the unspent part of the allowance was never returned"


def test_a_document_that_cannot_be_covered_is_refused_rather_than_started():
    ledger = PreviewLedger(shares=1, total=100)
    ledger.reserve().settle(100)
    with pytest.raises(FrameExtractionError, match="budget"):
        ledger.reserve()


def test_the_default_ceiling_is_the_documented_one():
    assert PreviewLedger().total == MAX_CALL_PREVIEW_BYTES


@pytest.mark.asyncio
async def test_concurrent_previews_cannot_exceed_the_shared_reservation():
    """Ten documents at once, one small pool: the sum must still fit."""
    ledger = PreviewLedger(shares=10, total=4096)
    raw = _animated_gif()

    async def _one():
        try:
            return await encode_frames_cancellable(raw, ".gif", 2, 64, ledger)
        except (FrameExtractionError, Exception) as error:  # noqa: BLE001 - recorded, not hidden
            return error

    outcomes = await asyncio.gather(*(_one() for _ in range(10)))
    produced = sum(
        sum(len(image.data) for image in outcome[1])
        for outcome in outcomes
        if not isinstance(outcome, BaseException)
    )
    assert produced <= 4096, produced
    assert ledger.spent <= 4096


@pytest.mark.asyncio
async def test_a_still_costs_the_call_the_same_way_a_frame_does():
    ledger = PreviewLedger(shares=1, total=MAX_CALL_PREVIEW_BYTES)
    _metas, images = await encode_still_cancellable(_png(), 64, ledger)
    assert ledger.spent == sum(len(image.data) for image in images) > 0


# --- every decoder is a child process now -------------------------------------


@pytest.mark.asyncio
async def test_a_still_image_is_decoded_in_a_process_that_can_be_killed(monkeypatch):
    created = _watch_children(monkeypatch)
    _metas, images = await encode_still_cancellable(_png(), 64)
    assert images, "no image came back"
    assert created, "the still decode ran in-process, where nothing can stop it"


@pytest.mark.asyncio
async def test_an_animation_is_decoded_in_a_process_that_can_be_killed(monkeypatch):
    created = _watch_children(monkeypatch)
    metas, images = await encode_frames_cancellable(_animated_gif(), ".gif", 3, 64)
    assert len(images) == len(metas) >= 2
    assert created, "the animated decode ran in-process, where nothing can stop it"


def test_a_pillow_decode_bomb_is_refused_by_the_worker_not_the_allocator(tmp_path):
    """A few-KB file declaring ~180 megapixels allocates ~700 MB when decoded."""
    path = tmp_path / "bomb.png"
    path.write_bytes(_png())
    huge = 30000

    class _Declares:
        size = (huge, huge)
        width = huge
        height = huge
        n_frames = 4

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with pytest.raises(FrameExtractionError, match="pixel"):
        frames._pillow_limits_or_raise(_Declares())


# --- nothing outlives a cancelled preview -------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_preview_leaves_no_worker_and_no_child(monkeypatch):
    """Cancelling the await used to free the caller and leave the thread decoding.

    The drain waited five seconds and then abandoned it, so 'cancelled' meant
    'asked to stop' - and anything that then counted processes or cleaned a
    directory raced a worker that was still using it.
    """
    created = _watch_children(monkeypatch)
    monkeypatch.setattr(frames, "_pillow_worker_command", _never_returns)

    # A one-thread executor is what makes this provable: if the worker were still
    # running, the sentinel below could never be scheduled. With the default pool
    # a wedged worker just occupies one of thirty-odd idle threads and nothing
    # about the thread count changes.
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        loop.set_default_executor(pool)
        task = asyncio.ensure_future(encode_frames_cancellable(_animated_gif(), ".gif", 3, 64))
        await _until(lambda: bool(created), "the decode never started")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert created[0][1].poll() is not None, "the decoder child outlived the cancellation"
        # The single worker has to be free again. A wedged one could never run this.
        assert await asyncio.wait_for(loop.run_in_executor(pool, lambda: "free"), 30) == "free"
        await _until(
            lambda: not _reader_threads(),
            f"a pipe reader survived: {_reader_threads()}",
        )


@pytest.mark.asyncio
async def test_a_cancelled_preview_removes_the_file_it_spooled_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(media_preview.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(frames.tempfile, "gettempdir", lambda: str(tmp_path))
    created = _watch_children(monkeypatch)
    monkeypatch.setattr(
        frames,
        "_pillow_worker_command",
        lambda *args, **kwargs: [
            media_preview.sys.executable,
            "-c",
            "import time\nwhile True: time.sleep(0.05)\n",
        ],
    )

    task = asyncio.ensure_future(encode_frames_cancellable(_animated_gif(), ".gif", 3, 64))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if created:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and list(tmp_path.iterdir()):
        await asyncio.sleep(0.05)
    assert list(tmp_path.iterdir()) == [], "a spooled temp file survived the cancellation"


# --- one clock over the whole call --------------------------------------------


@pytest.mark.asyncio
async def test_the_call_deadline_stops_the_second_document_not_only_the_first():
    """Per-document budgets do not bound a call: ten documents each inside their
    own budget is ten times the budget, and nothing upstream notices."""
    ledger = PreviewLedger(shares=2, total=MAX_CALL_PREVIEW_BYTES, deadline=time.monotonic() - 1)
    with pytest.raises(FrameExtractionError, match="budget"):
        await encode_frames_cancellable(_animated_gif(), ".gif", 2, 64, ledger)


@pytest.mark.asyncio
async def test_a_call_with_time_left_still_produces():
    ledger = PreviewLedger(shares=2, deadline=time.monotonic() + 60)
    metas, images = await encode_frames_cancellable(_animated_gif(), ".gif", 2, 64, ledger)
    assert images and len(metas) == len(images)
