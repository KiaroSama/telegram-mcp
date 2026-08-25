"""The preview helpers that decide a suffix and turn bytes into images.

These are pure and blocking by design - the tool layer calls them in a worker
thread - so they are tested directly rather than through a client. The suffix
one matters most: it picks which DECODER runs, and the value it works from is
the sender's own extension or mime type.
"""

import asyncio
import io
import threading
import time

import pytest
from PIL import Image as PILImage

from telegram_mcp import media_preview
from telegram_mcp.visual import bounded_process


def _png_bytes(size=(8, 8), colour="red"):
    buffer = io.BytesIO()
    PILImage.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_declared_extension_is_used_with_or_without_its_dot():
    assert media_preview._media_suffix({"extension": ".webm"}) == ".webm"
    assert media_preview._media_suffix({"extension": "webm"}) == ".webm"


def test_the_mime_type_is_the_fallback_when_no_extension_was_declared():
    suffix = media_preview._media_suffix({"mime_type": "image/webp"})

    assert suffix.startswith("."), suffix
    assert suffix != ".bin", "a known mime type should map to its own suffix"


def test_an_unknown_mime_type_falls_back_to_bin_rather_than_guessing():
    """The suffix selects the decoder, and the mime type is the sender's. An
    unrecognised one must not be turned into a decoder choice."""
    assert media_preview._media_suffix({"mime_type": "application/x-invented"}) == ".bin"
    assert media_preview._media_suffix({}) == ".bin"


def test_the_mime_lookup_ignores_case():
    assert media_preview._media_suffix({"mime_type": "IMAGE/WEBP"}) == media_preview._media_suffix(
        {"mime_type": "image/webp"}
    )


def test_one_still_comes_back_as_a_single_metadata_and_image_pair():
    metas, images = media_preview._encode_one(_png_bytes(), max_dimension=64)

    assert len(metas) == 1 and len(images) == 1
    assert images[0].data, "no encoded bytes came back"
    assert metas[0]["width"] <= 64 and metas[0]["height"] <= 64


def test_a_still_larger_than_the_bound_is_scaled_down():
    """64 rather than something smaller because MIN_IMAGE_DIMENSION clamps the
    argument upward - a caller cannot shrink a preview below it, deliberately."""
    metas, _images = media_preview._encode_one(_png_bytes(size=(400, 200)), max_dimension=64)

    assert max(metas[0]["width"], metas[0]["height"]) <= 64
    assert metas[0]["downscaled"] is True
    assert metas[0]["original_width"] == 400


def test_frames_carry_both_the_extractor_s_metadata_and_the_encoder_s(monkeypatch):
    """The two dicts are merged, and a frame index from the extractor must survive
    the encoder's own keys."""
    png = _png_bytes()
    monkeypatch.setattr(
        media_preview,
        "extract_frames",
        lambda raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None: [
            (png, {"frame_index": 0, "width": 8, "height": 8, "bytes": len(png)}),
            (png, {"frame_index": 1, "width": 8, "height": 8, "bytes": len(png)}),
        ],
    )

    metas, images = media_preview._encode_frames(b"ignored", ".webp", count=2, max_dimension=64)

    assert [m["frame_index"] for m in metas] == [0, 1]
    assert all("width" in m for m in metas), "the decoder's metadata was dropped"
    assert len(images) == 2


def test_no_frames_extracted_yields_no_images_rather_than_an_error(monkeypatch):
    monkeypatch.setattr(
        media_preview,
        "extract_frames",
        lambda raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None: [],
    )

    metas, images = media_preview._encode_frames(b"", ".webp", count=4, max_dimension=64)

    assert metas == [] and images == []


# --- cancelling the caller has to reach the worker thread ----------------------


@pytest.mark.asyncio
async def test_cancelling_the_caller_stops_the_decode(monkeypatch):
    """`asyncio.to_thread` alone leaves the worker running: Python cannot stop a
    thread from outside, and a started concurrent.futures job cannot be cancelled.
    So a client that disconnected still paid for every remaining frame, and ffmpeg
    kept burning CPU with nobody left to read the answer.

    No sleep decides this test. The worker signals when it has really begun, and
    signals again when it has noticed - both waits are bounded.
    """
    running = threading.Event()
    noticed = threading.Event()

    def _slow_extract(
        raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None
    ):
        running.set()
        for _ in range(200):
            if cancelled is not None and cancelled.is_set():
                noticed.set()
                raise media_preview.FrameExtractionError("cancelled")
            time.sleep(0.02)
        raise AssertionError("the decode ran to completion after being cancelled")

    monkeypatch.setattr(media_preview, "extract_frames", _slow_extract)

    task = asyncio.create_task(
        media_preview.encode_frames_cancellable(b"", ".webp", 4, max_dimension=64)
    )
    assert await asyncio.to_thread(running.wait, 5), "the worker never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(
        noticed.wait, 5
    ), "the worker was never told the caller had gone"


@pytest.mark.asyncio
async def test_a_decode_that_is_not_cancelled_gets_no_signal(monkeypatch):
    """The event must stay clear on the happy path, or every decode would abort."""
    seen = {}

    def _extract(raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None):
        seen["set"] = cancelled is not None and cancelled.is_set()
        return []

    monkeypatch.setattr(media_preview, "extract_frames", _extract)

    metas, images = await media_preview.encode_frames_cancellable(
        b"", ".webp", 4, max_dimension=64
    )

    assert seen["set"] is False
    assert metas == [] and images == []


@pytest.mark.asyncio
async def test_cancellation_waits_for_the_worker_to_actually_stop(monkeypatch):
    """Setting the flag is a request; the worker finishing is the event.

    The old version set the event and re-raised in the same breath, so
    `CancelledError` reached the caller while the decoder was still mid-frame with
    its ffmpeg child running. Anything that then counted processes, removed a temp
    directory or reported "nothing left running" was racing a worker still using
    it. This asserts the worker has genuinely finished by the time the cancellation
    surfaces.
    """
    running = threading.Event()
    finished = threading.Event()

    def _slow_extract(
        raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None
    ):
        running.set()
        try:
            for _ in range(400):
                if cancelled is not None and cancelled.is_set():
                    # Unwinding is not instant: a real decoder kills and reaps a
                    # child here, and that is exactly what must complete first.
                    time.sleep(0.05)
                    raise media_preview.FrameExtractionError("cancelled")
                time.sleep(0.01)
            raise AssertionError("the decode ran to completion after being cancelled")
        finally:
            finished.set()

    monkeypatch.setattr(media_preview, "extract_frames", _slow_extract)

    task = asyncio.create_task(
        media_preview.encode_frames_cancellable(b"", ".webp", 4, max_dimension=64)
    )
    assert await asyncio.to_thread(running.wait, 5), "the worker never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set(), "cancellation surfaced while the worker was still running"


@pytest.mark.asyncio
async def test_a_wedged_worker_cannot_hold_the_canceller_forever(monkeypatch):
    """The drain is bounded on purpose. A decoder that ignores the flag is the
    failure being cancelled; letting it also pin the canceller would turn one stuck
    request into two.
    """
    # The drain lives with the cancellation mechanics now, shared with the
    # window capture, so that is where the budget is.
    monkeypatch.setattr(bounded_process, "CANCEL_DRAIN_SECONDS", 0.3)
    started = threading.Event()
    release = threading.Event()

    def _wedged(raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None):
        started.set()
        release.wait(30)  # ignores the cancel flag entirely
        return [], []

    monkeypatch.setattr(media_preview, "extract_frames", _wedged)

    task = asyncio.create_task(
        media_preview.encode_frames_cancellable(b"", ".webp", 4, max_dimension=64)
    )
    assert await asyncio.to_thread(started.wait, 5), "the worker never started"

    task.cancel()
    began = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - began

    assert elapsed < 3, f"the wedged worker held the canceller for {elapsed:.1f}s"
    release.set()


# --- one ceiling for the call, not one per document ---------------------------


def test_the_call_ledger_refuses_once_the_whole_request_is_too_large():
    """The per-request frame budget bounds ONE document. A batch of ten each staying
    inside it holds ten times it, so the batch had no ceiling of its own - the
    existing gate bounds SOURCE bytes in flight, which is a different and much
    smaller quantity than what those bytes decode into.
    """
    ledger = media_preview.PreviewLedger(shares=1, total=1000)

    ledger.reserve().settle(600)
    ledger.reserve().settle(300)  # 900, still inside

    with pytest.raises(media_preview.FrameExtractionError) as caught:
        ledger.reserve().settle(200)

    message = str(caught.value)
    # It has to say what to change, or the only move left is to stop using the tool.
    assert "max_dimension" in message
    assert ledger.spent <= 1000, "the ceiling was passed rather than enforced"


def test_the_ledger_keeps_what_was_already_produced():
    """The document that crosses the line is the one refused - the nine already
    decoded are not thrown away with it."""
    ledger = media_preview.PreviewLedger(shares=1, total=100)
    ledger.reserve().settle(90)

    with pytest.raises(media_preview.FrameExtractionError):
        ledger.reserve().settle(50)

    assert ledger.spent == 100, "what was produced has to stay booked"


@pytest.mark.asyncio
async def test_frames_are_charged_to_the_ledger_they_were_given(monkeypatch):
    """The wiring, not just the arithmetic: a ledger nothing charges is decoration."""
    png = _png_bytes()

    monkeypatch.setattr(
        media_preview,
        "extract_frames",
        lambda raw, suffix, count, cancelled=None, deadline=None, max_side=None, allowance=None: [
            (png, {"frame_index": 0}),
            (png, {"frame_index": 1}),
        ],
    )
    ledger = media_preview.PreviewLedger(shares=1, total=10_000_000)

    _metas, images = await media_preview.encode_frames_cancellable(
        b"", ".webp", 2, max_dimension=64, ledger=ledger
    )

    assert ledger.spent == sum(len(image.data) for image in images)
    assert ledger.spent > 0
