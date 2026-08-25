"""What a window capture may allocate, how long it may take, and what ends it.

Three defects, all of them about a bound that was not there.

**PrintWindow can block for ever.** Microsoft documents it as synchronous with no
guarantee of returning promptly - it asks the target window to redraw itself, on
that window's message loop. It ran under ``asyncio.to_thread``, so a capture that
never returned held a worker thread with no deadline, and a cancellation test
against the shipped code showed the coroutine finishing while the thread stayed
alive.

**The bitmap is allocated before any cap.** ``CreateCompatibleBitmap`` and a
``width * height * 4`` buffer are built from the window rectangle as reported,
and the only size check happened afterwards, in the downscale. A window claiming
a 100000x100000 rectangle is 40 GB asked for before anything looks at the number.

**The response has no ceiling.** ``native_resolution`` skips the downscale
entirely, and ``get_telegram_frames`` multiplies that by up to eight.
"""

import asyncio
import json
import sys
import threading
import time

import pytest
from PIL import Image

from telegram_mcp.visual import bounded_process, capture, images
from telegram_mcp.visual.capture import CaptureError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the Win32 capture path only exists on Windows"
)


def _telegram_window(width=1200, height=800):
    return capture.WindowInfo(
        hwnd=1,
        title="Telegram",
        class_name="Qt",
        rect=(0, 0, width, height),
        is_foreground=True,
        is_minimized=False,
        process_path=r"C:\T\Telegram.exe",
    )


def _watch_children(monkeypatch):
    created = []
    original = bounded_process.subprocess.Popen

    def _factory(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(bounded_process.subprocess, "Popen", _factory)
    return created


def _reader_threads():
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(bounded_process.READER_NAME) and thread.is_alive()
    ]


@pytest.fixture(autouse=True)
def _no_reader_left():
    yield
    deadline = time.monotonic() + 30
    while _reader_threads() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _reader_threads() == []


# --- the size is checked before anything is allocated -------------------------


class _RefusesToAllocate:
    """A gdi32 that fails the test if it is asked for a bitmap at all."""

    def __init__(self):
        self.calls = []

    def CreateCompatibleDC(self, _dc):
        self.calls.append("CreateCompatibleDC")
        return 1

    def CreateCompatibleBitmap(self, _dc, width, height):
        self.calls.append(("CreateCompatibleBitmap", width, height))
        return 1

    def SelectObject(self, _dc, _obj):
        return 1

    def DeleteObject(self, _obj):
        return 1

    def DeleteDC(self, _dc):
        return 1

    def GetDIBits(self, *_args):
        return 1


class _StubUser32:
    def __init__(self, calls):
        self.calls = calls

    def GetWindowDC(self, _hwnd):
        self.calls.append("GetWindowDC")
        return 1

    def ReleaseDC(self, _hwnd, _dc):
        return 1

    def PrintWindow(self, _hwnd, _dc, _flags):
        self.calls.append("PrintWindow")
        return 1


@pytest.mark.parametrize(
    "size",
    [
        (100_000, 100_000),  # 40 GB of RGBA
        (capture.MAX_CAPTURE_SIDE + 1, 100),
        (100, capture.MAX_CAPTURE_SIDE + 1),
    ],
)
def test_an_impossible_window_rectangle_is_refused_before_a_bitmap_exists(monkeypatch, size):
    gdi = _RefusesToAllocate()
    allocations = gdi.calls
    monkeypatch.setattr(capture, "_win32", lambda: (_StubUser32(allocations), gdi, None))
    monkeypatch.setattr(
        capture.ctypes,
        "create_string_buffer",
        lambda *a, **k: pytest.fail("the readback buffer was allocated"),
    )

    with pytest.raises(CaptureError, match="too large"):
        capture._capture_print_window(_telegram_window(*size), client_only=False)

    assert allocations == [], f"an allocation ran before the check: {allocations}"


def test_a_window_inside_the_limits_is_not_refused():
    width, height = capture.MAX_CAPTURE_SIDE, 1
    assert capture._within_capture_limits(width, height) is None
    assert capture._within_capture_limits(1200, 800) is None


def test_the_pixel_ceiling_bites_before_the_side_ceiling_does():
    """Two sides can each be legal and their product still be absurd."""
    side = capture.MAX_CAPTURE_SIDE
    refusal = capture._within_capture_limits(side, side)
    assert refusal and "too large" in refusal


def test_a_zero_sized_window_is_still_refused():
    with pytest.raises(CaptureError, match="zero-sized"):
        capture._capture_print_window(_telegram_window(0, 0), client_only=False)


def test_the_screen_grab_is_bounded_by_the_same_rule(monkeypatch):
    """ImageGrab allocates the whole box too, so it needs the same check.

    Guarding only PrintWindow would leave method='screen' as the way around it,
    and the method is a plain tool argument.
    """
    import PIL.ImageGrab

    monkeypatch.setattr(
        PIL.ImageGrab, "grab", lambda **kwargs: pytest.fail("the screen area was allocated")
    )

    with pytest.raises(CaptureError, match="too large"):
        capture._capture_screen_region(_telegram_window(100_000, 100_000))


# --- native resolution is not unlimited resolution ----------------------------


def test_native_resolution_still_has_a_ceiling():
    """`native=True` is 'do not downscale to max_dimension', not 'no limit'.

    An 8K window at native is roughly 20k+ tokens of base64 in the model's
    context, and the flag was the documented way to ask for exactly that.
    """
    huge = Image.new("RGB", (images.MAX_NATIVE_DIMENSION * 2, 100), (10, 20, 30))
    _data, meta = images.encode_image(huge, native=True)

    assert meta["width"] <= images.MAX_NATIVE_DIMENSION
    assert meta["native_resolution"] is True
    assert meta["downscaled"] is True, "the clamp has to be visible in the metadata"


def test_native_resolution_leaves_an_image_inside_the_ceiling_alone():
    original = Image.new("RGB", (800, 600), (10, 20, 30))
    _data, meta = images.encode_image(original, native=True, max_dimension=64)

    assert (meta["width"], meta["height"]) == (800, 600)
    assert meta.get("downscaled") is not True


# --- the whole call, not only each frame --------------------------------------


def test_the_response_ceiling_is_smaller_than_eight_native_frames():
    """Eight 4K frames at native is the request the ceiling exists for."""
    eight_native = 8 * images.MAX_NATIVE_DIMENSION * images.MAX_NATIVE_DIMENSION * 3
    assert capture.MAX_CAPTURE_RESPONSE_BYTES < eight_native


def test_the_worker_refuses_once_the_response_ceiling_is_reached():
    frames = [(b"x" * 600, {"bytes": 600}), (b"y" * 600, {"bytes": 600})]
    with pytest.raises(CaptureError, match="response"):
        capture.check_response_bytes(sum(len(data) for data, _ in frames), limit=1000)


# --- a capture that never returns ---------------------------------------------


HANGING_PRINTWINDOW = """
import sys, time, json
from telegram_mcp.visual import capture

class _Hangs:
    def GetWindowDC(self, _hwnd):
        return 1
    def ReleaseDC(self, _hwnd, _dc):
        return 1
    def PrintWindow(self, _hwnd, _dc, _flags):
        # Exactly what Microsoft does not promise against: the call goes to the
        # target window's message loop and simply does not come back.
        while True:
            time.sleep(0.05)

class _Gdi:
    def CreateCompatibleDC(self, _dc): return 1
    def CreateCompatibleBitmap(self, _dc, w, h): return 1
    def SelectObject(self, _dc, _o): return 1
    def DeleteObject(self, _o): return 1
    def DeleteDC(self, _dc): return 1
    def GetDIBits(self, *a): return 1

capture._win32 = lambda: (_Hangs(), _Gdi(), None)
capture._require_windows = lambda: None
capture._ensure_dpi_awareness = lambda: None
capture.find_target_window = lambda **k: capture.WindowInfo(
    hwnd=1, title="T", class_name="Qt", rect=(0, 0, 200, 200),
    is_foreground=True, is_minimized=False, process_path="C:/T/Telegram.exe",
)
sys.stderr.write("started\\n")
sys.stderr.flush()
capture.capture_window(hwnd=1, method="window")
"""


def _hanging_worker(*_args, **_kwargs):
    return [sys.executable, "-c", HANGING_PRINTWINDOW]


def test_listing_the_windows_is_bounded_the_same_way(monkeypatch):
    """GetWindowTextW waits for the window to answer a message, so enumerating a
    hung Telegram blocks exactly as capturing one does - and a bound on only the
    capture leaves list_telegram_windows as the way to wedge the server."""
    created = _watch_children(monkeypatch)
    monkeypatch.setattr(capture, "_capture_worker_command", _hanging_worker)

    started = time.monotonic()
    with pytest.raises(CaptureError, match="timed out"):
        capture.list_windows_bounded(timeout=1.0)

    assert time.monotonic() - started < 30
    assert created and created[0].poll() is not None


def test_a_printwindow_that_never_returns_is_killed_rather_than_waited_out(monkeypatch):
    created = _watch_children(monkeypatch)
    monkeypatch.setattr(capture, "_capture_worker_command", _hanging_worker)

    started = time.monotonic()
    with pytest.raises(CaptureError, match="timed out"):
        capture.capture_frames(count=1, timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"the capture waited {elapsed:.1f}s for a bound of 1s"
    assert created and created[0].poll() is not None, "the capture process outlived its bound"


@pytest.mark.asyncio
async def test_cancelling_a_capture_terminates_it_rather_than_orphaning_it(monkeypatch):
    from telegram_mcp.tools import visual as visual_tool

    created = _watch_children(monkeypatch)
    monkeypatch.setattr(capture, "_capture_worker_command", _hanging_worker)

    task = asyncio.ensure_future(visual_tool._capture_encoded(None, "window", None, "png", 900))
    deadline = time.monotonic() + 30
    while not created and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert created, "the capture never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created[0].poll() is not None, "the capture process survived the cancellation"


# --- eight frames at native resolution, for real ------------------------------


def _stub_worker_frames(monkeypatch, count, width, height, tmp_path):
    """A worker that really replies with ``count`` encoded frames of that size.

    Real bytes over a real pipe, because the ceilings under test are about how
    many bytes come back - a stub that returns Python objects would step over the
    part that does the counting.
    """
    import base64

    payload, metas = b"", []
    for index in range(count):
        image = Image.new("RGB", (width, height), (index * 7 % 255, 40, 90))
        data, meta = images.encode_image(image, native=True)
        metas.append({"index": index, "bytes": len(data), "image": meta})
        payload += data
    header = json.dumps({"window": {"hwnd": 1, "title": "T"}, "frames": metas}).encode()

    reply = tmp_path / "reply.b64"
    reply.write_text(base64.b64encode(header + b"\n" + payload).decode(), encoding="ascii")
    script = (
        "import base64,pathlib,sys\n"
        f"sys.stdout.buffer.write(base64.b64decode(pathlib.Path({str(reply)!r}).read_text()))\n"
    )
    monkeypatch.setattr(
        capture, "_capture_worker_command", lambda *a, **k: [sys.executable, "-c", script]
    )


def test_eight_frames_at_native_resolution_are_refused_rather_than_returned(monkeypatch, tmp_path):
    """The request the response ceiling exists for, run end to end."""
    # Lowered so eight real frames cross it in a second rather than a minute;
    # that the SHIPPED ceiling sits below eight native frames is asserted above.
    monkeypatch.setattr(capture, "MAX_CAPTURE_RESPONSE_BYTES", 5_000)
    _stub_worker_frames(monkeypatch, count=8, width=900, height=900, tmp_path=tmp_path)

    with pytest.raises(CaptureError, match="response"):
        capture.capture_frames(count=8, native=True, timeout=60.0)


def test_a_reasonable_multi_frame_request_still_comes_back(monkeypatch, tmp_path):
    _stub_worker_frames(monkeypatch, count=4, width=200, height=150, tmp_path=tmp_path)

    window, frames = capture.capture_frames(count=4, timeout=60.0)

    assert window["hwnd"] == 1
    assert len(frames) == 4
    assert all(data for data, _meta in frames)
