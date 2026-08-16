"""Unit tests for the visual helpers that need neither Telegram nor a window.

Window capture itself needs a live Telegram Desktop, so only its platform and
argument guards are exercised here; the image and frame helpers are covered end
to end with images built in memory by Pillow.
"""

import importlib.util
import io
import subprocess
import sys
import tempfile

import pytest
from PIL import Image

from telegram_mcp.visual import capture, frames, images
from telegram_mcp.visual.frames import (
    FrameExtractionError,
    extract_frames,
    ffmpeg_available,
    lottie_available,
)
from telegram_mcp.visual.images import (
    MAX_IMAGE_DIMENSION,
    ImageError,
    encode_image,
    fit_image,
    open_image_bytes,
)


def _animated_gif(frame_count=3):
    """A real multi-frame GIF, so the Pillow path runs without ffmpeg."""
    frames = [
        Image.new("RGB", (32, 32), color)
        for color in ("red", "green", "blue", "yellow")[:frame_count]
    ]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=40)
    return buffer.getvalue()


def _animated_webp(frame_count=3):
    """Animated WebP rather than GIF: .gif falls through to ffmpeg on error."""
    frames_ = [
        Image.new("RGB", (32, 32), color) for color in ("red", "green", "blue")[:frame_count]
    ]
    buffer = io.BytesIO()
    frames_[0].save(buffer, format="WEBP", save_all=True, append_images=frames_[1:], duration=40)
    return buffer.getvalue()


def _animated_tgs():
    """A real .tgs: one square whose opacity animates, so frames differ."""
    import gzip
    import json

    lottie = {
        "v": "5.5.7",
        "fr": 30,
        "ip": 0,
        "op": 30,
        "w": 64,
        "h": 64,
        "nm": "t",
        "ddd": 0,
        "assets": [],
        "layers": [
            {
                "ddd": 0,
                "ind": 1,
                "ty": 1,
                "nm": "solid",
                "sr": 1,
                "sc": "#ff0000",
                "sw": 64,
                "sh": 64,
                "ks": {
                    "o": {
                        "a": 1,
                        "k": [
                            {
                                "t": 0,
                                "s": [100],
                                "i": {"x": [1], "y": [1]},
                                "o": {"x": [0], "y": [0]},
                            },
                            {"t": 29, "s": [0]},
                        ],
                    },
                    "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [32, 32, 0]},
                    "a": {"a": 0, "k": [32, 32, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
                "ao": 0,
                "ip": 0,
                "op": 30,
                "st": 0,
                "bm": 0,
            }
        ],
    }
    return gzip.compress(json.dumps(lottie).encode())


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("png", "image/png"), ("jpeg", "image/jpeg"), ("webp", "image/webp")],
)
def test_encode_image_round_trips_supported_formats(image_format, mime_type):
    data, meta = encode_image(Image.new("RGB", (120, 60), "red"), image_format=image_format)

    assert meta == {
        "format": image_format,
        "mime_type": mime_type,
        "width": 120,
        "height": 60,
        "bytes": len(data),
    }
    assert open_image_bytes(data).size == (120, 60)


def test_encode_image_rejects_unknown_format():
    with pytest.raises(ImageError, match="Unsupported image format"):
        encode_image(Image.new("RGB", (8, 8)), image_format="tiff")


def test_encode_image_reports_the_downscale_it_applied():
    data, meta = encode_image(Image.new("RGB", (3000, 1000)))

    assert meta["width"] == MAX_IMAGE_DIMENSION
    assert meta["downscaled"] is True
    assert (meta["original_width"], meta["original_height"]) == (3000, 1000)
    assert open_image_bytes(data).width == MAX_IMAGE_DIMENSION


def test_fit_image_downscales_the_long_side_only():
    fitted, resized = fit_image(Image.new("RGB", (3000, 1000)), MAX_IMAGE_DIMENSION)

    assert resized is True
    assert fitted.size == (1568, 523)


def test_fit_image_never_upscales():
    small = Image.new("RGB", (40, 40))
    fitted, resized = fit_image(small, MAX_IMAGE_DIMENSION)

    assert resized is False
    assert fitted is small


def test_open_image_bytes_rejects_garbage():
    with pytest.raises(ImageError, match="Could not decode image data"):
        open_image_bytes(b"definitely not an image")


def test_extract_frames_samples_an_animated_gif_with_pillow():
    frames = extract_frames(_animated_gif(), ".gif", count=2)

    assert len(frames) == 2
    assert [meta["frame_index"] for _, meta in frames] == [0, 2]
    for data, meta in frames:
        assert data.startswith(b"\x89PNG")
        assert meta["frame_count"] == 3
        assert meta["source"] == "pillow"
        assert meta["mime_type"] == "image/png"


def test_extract_frames_deletes_the_file_it_spooled_to_disk(monkeypatch, tmp_path):
    """The media is written to a temp file; the finally-block must remove it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    assert len(extract_frames(_animated_gif(), ".gif", count=2)) == 2

    assert list(tmp_path.iterdir()) == []


def test_extract_frames_points_tgs_at_the_fallbacks_when_rlottie_is_absent(monkeypatch):
    """Without the optional renderer the error must name every way forward."""
    monkeypatch.setattr("telegram_mcp.visual.frames.lottie_available", lambda: False)

    with pytest.raises(FrameExtractionError) as raised:
        extract_frames(b"gzipped-lottie", ".tgs")

    message = str(raised.value)
    assert "telegram-mcp[lottie]" in message
    assert "get_media_thumbnail" in message
    assert "get_telegram_frames" in message


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_extract_frames_reports_an_undecodable_tgs_clearly():
    """With the renderer present, garbage must still surface as FrameExtractionError."""
    with pytest.raises(FrameExtractionError, match="could not open this .tgs"):
        extract_frames(b"not gzipped lottie at all", ".tgs")


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_extract_frames_renders_distinct_frames_from_a_real_lottie():
    """A moving shape must produce different frames, not the same image N times."""
    frames = extract_frames(_animated_tgs(), ".tgs", count=3)

    assert len(frames) == 3
    assert len({png for png, _ in frames}) == 3, "every frame rendered identically"
    for png, meta in frames:
        assert png[1:4] == b"PNG", "not a PNG frame"
        assert meta["source"] == "rlottie"
        assert meta["animation_format"] == "lottie_tgs"
        assert meta["timestamp_seconds"] >= 0


def test_ffmpeg_available_reports_a_bool():
    assert isinstance(ffmpeg_available(), bool)


# --- Regressions for sender-controlled bytes and metadata --------------------


def test_safe_stderr_redacts_a_temp_path_containing_spaces():
    """The old regex stopped at the first space, leaking the surname and the
    temp filename into the model's context."""
    path = r"C:\Users\John Smith\AppData\Local\Temp\tmpab12.webm"
    stderr = f"{path}: Invalid data found when processing input".encode()

    text = frames._safe_stderr(stderr, path)

    assert "Smith" not in text
    assert "tmpab12" not in text
    assert "AppData" not in text
    assert "<temp-file>" in text
    # The diagnostic itself must survive; redaction must not swallow it.
    assert "Invalid data found" in text


def test_ffmpeg_failure_reports_the_first_seek_not_the_last(monkeypatch, tmp_path):
    """`result` after the loop is the furthest-past-EOF seek, i.e. the least
    informative message; and its stderr still carries our temp path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(frames, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(frames, "probe_duration", lambda path: None)
    messages = iter(
        [
            b"first: moov atom not found",
            b"second: Output file is empty",
            b"third: Output file is empty",
            b"fourth: Output file is empty",
        ]
    )

    def _fake_run(command, timeout):
        # The temp path ffmpeg was handed is the one that must not reach the model.
        spooled = command[command.index("-i") + 1]
        return subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=next(messages) + b" " + spooled.encode()
        )

    monkeypatch.setattr(frames, "_run", _fake_run)

    with pytest.raises(FrameExtractionError) as raised:
        extract_frames(b"not really a video", ".mp4", count=4)

    message = str(raised.value)
    assert "moov atom not found" in message
    assert "Output file is empty" not in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize("hostile", [".webm:ads", ".hta", "." + "a" * 300])
def test_extract_frames_refuses_a_suffix_it_cannot_decode(monkeypatch, hostile):
    """The suffix comes from the sender's mime_type/filename and becomes both a
    real temp filename and the decoder selector."""
    seen = {}

    def _fake_ffmpeg(path, count):
        seen["path"] = path
        return [(b"\x89PNG", {"frame_index": 0})]

    monkeypatch.setattr(frames, "_frames_with_ffmpeg", _fake_ffmpeg)

    extract_frames(b"whatever", hostile, count=1)

    assert seen["path"].endswith(".bin")
    assert "ads" not in seen["path"]


def test_open_image_bytes_refuses_an_oversized_image(monkeypatch):
    """Pillow only warns below 2x its own limit, so ~178M pixels would decode
    and allocate roughly 700 MB in a worker thread."""
    monkeypatch.setattr(images, "MAX_DECODED_PIXELS", 100)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buffer, format="PNG")

    with pytest.raises(ImageError, match="refusing to decode"):
        open_image_bytes(buffer.getvalue())


def test_extract_frames_refuses_an_animation_with_too_many_frames(monkeypatch):
    monkeypatch.setattr(frames, "MAX_ANIMATION_FRAMES", 2)

    with pytest.raises(FrameExtractionError, match="refusing to decode"):
        extract_frames(_animated_webp(3), ".webp", count=2)


def test_capture_methods_are_stable():
    assert capture.CAPTURE_METHODS == ("window", "screen")


def test_capture_window_rejects_an_unknown_method():
    expected = (
        "Unknown capture method" if sys.platform == "win32" else "only implemented on Windows"
    )

    with pytest.raises(capture.CaptureError, match=expected):
        capture.capture_window(method="hologram")


def test_default_process_name_honours_the_env_override(monkeypatch):
    """Executed as a throwaway copy: reloading the real module in place would
    rebuild CaptureError, and every ``except CaptureError`` imported elsewhere
    (telegram_mcp/tools/visual.py) would stop catching it for the rest of the
    session."""
    untouched = capture.DEFAULT_PROCESS_NAME
    monkeypatch.setenv("TELEGRAM_DESKTOP_PROCESS", "PortableTelegram.exe")
    spec = importlib.util.spec_from_file_location("_capture_env_probe", capture.__file__)
    probe = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module's string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, probe)

    spec.loader.exec_module(probe)

    assert probe.DEFAULT_PROCESS_NAME == "PortableTelegram.exe"
    assert capture.DEFAULT_PROCESS_NAME == untouched
    assert capture.CaptureError is sys.modules["telegram_mcp.visual"].CaptureError


def test_looks_blank_separates_a_flat_capture_from_a_rendered_one():
    blank = Image.new("RGB", (64, 64), "black")
    rendered = Image.new("RGB", (64, 64), "black")
    rendered.paste(Image.new("RGB", (32, 64), "white"), (0, 0))

    assert capture._looks_blank(blank) is True
    assert capture._looks_blank(rendered) is False


def _telegram_window(**overrides):
    fields = {
        "hwnd": 1,
        "title": "Telegram",
        "class_name": "Qt5",
        "rect": (0, 0, 100, 100),
        "is_foreground": False,
        "is_minimized": False,
    }
    fields.update(overrides)
    return capture.WindowInfo(**fields)


def test_capture_window_refuses_a_blank_minimized_window(monkeypatch):
    """PrintWindow has no client surface for a minimized window, so the black
    frame it returns was being handed back as a successful capture."""
    monkeypatch.setattr(capture, "_require_windows", lambda: None)
    monkeypatch.setattr(capture, "_ensure_dpi_awareness", lambda: None)
    monkeypatch.setattr(
        capture, "find_target_window", lambda **kwargs: _telegram_window(is_minimized=True)
    )
    monkeypatch.setattr(
        capture,
        "_capture_print_window",
        lambda w, client_only: Image.new("RGB", (64, 64), "black"),
    )

    with pytest.raises(capture.CaptureError, match="minimized"):
        capture.capture_window()


def test_capture_window_still_falls_back_to_the_screen_when_not_minimized(monkeypatch):
    """The minimized guard must not swallow the pre-existing blank fallback."""
    monkeypatch.setattr(capture, "_require_windows", lambda: None)
    monkeypatch.setattr(capture, "_ensure_dpi_awareness", lambda: None)
    monkeypatch.setattr(capture, "find_target_window", lambda **kwargs: _telegram_window())
    monkeypatch.setattr(
        capture,
        "_capture_print_window",
        lambda w, client_only: Image.new("RGB", (64, 64), "black"),
    )
    rendered = Image.new("RGB", (64, 64), "black")
    rendered.paste(Image.new("RGB", (32, 64), "white"), (0, 0))
    monkeypatch.setattr(capture, "_capture_screen_region", lambda w, client_only: rendered)

    image, _window, meta = capture.capture_window()

    assert image is rendered
    assert meta["method"] == "screen"
    assert "fallback" in meta


class _FakeUser32:
    """Enough of user32 for the enumeration tests.

    The real ``EnumWindows`` stops as soon as the callback fails to return TRUE,
    and ctypes turns an exception escaping the callback into exactly that. Calling
    the thunk from Python leaves the return slot uninitialised rather than zeroing
    it, so this watches ``sys.unraisablehook`` for the swallowed exception instead
    of trusting the integer that comes back.
    """

    def __init__(self, hwnds, rect_ok=None, boom=()):
        self.hwnds = hwnds
        self.rect_ok = rect_ok or (lambda hwnd: True)
        self.boom = boom

    def EnumWindows(self, callback, _lparam):
        for hwnd in self.hwnds:
            swallowed = []
            previous_hook = sys.unraisablehook
            sys.unraisablehook = swallowed.append
            try:
                proceed = callback(hwnd, 0)
            finally:
                sys.unraisablehook = previous_hook
            if swallowed or proceed != 1:
                break
        return 1

    def IsWindowVisible(self, hwnd):
        return 1

    def IsIconic(self, hwnd):
        return 0

    def GetForegroundWindow(self):
        return 0

    def GetWindowThreadProcessId(self, hwnd, _pid_ref):
        return 1

    def GetWindowTextLengthW(self, hwnd):
        if hwnd in self.boom:
            raise OSError("window vanished")
        return 3

    def GetWindowTextW(self, hwnd, buffer, _size):
        buffer.value = "chat"
        return 4

    def GetClassNameW(self, hwnd, buffer, _size):
        buffer.value = "Qt5"
        return 3

    def GetWindowRect(self, hwnd, rect_ref):
        if not self.rect_ok(hwnd):
            return 0
        rect = rect_ref._obj
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 100, 100
        return 1

    def GetDpiForWindow(self, hwnd):
        return 96


def _fake_enumeration(monkeypatch, user32):
    monkeypatch.setattr(capture, "_win32", lambda: (user32, None, None))
    monkeypatch.setattr(capture, "_require_windows", lambda: None)
    monkeypatch.setattr(capture, "_ensure_dpi_awareness", lambda: None)
    monkeypatch.setattr(capture, "_process_image_path", lambda pid: r"C:\T\Telegram.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_list_windows_skips_a_window_whose_rectangle_could_not_be_read(monkeypatch):
    """A window destroyed mid-enumeration leaves rect zero-filled, and reporting
    it surfaces a real-looking window with width/height 0."""
    _fake_enumeration(monkeypatch, _FakeUser32([1, 2], rect_ok=lambda hwnd: hwnd != 2))

    windows = capture.list_windows()

    assert [window.hwnd for window in windows] == [1]
    assert all(window.width > 0 for window in windows)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_list_windows_is_not_truncated_by_one_failing_window(monkeypatch):
    """ctypes converts an exception escaping the callback into a 0 return, and a
    0 return stops EnumWindows — so one bad window hid every window after it."""
    _fake_enumeration(monkeypatch, _FakeUser32([1, 2, 3], boom=(2,)))

    windows = capture.list_windows()

    assert [window.hwnd for window in windows] == [1, 3]


class _FakeGdi32:
    """A gdi32 whose SelectObject declines to select the capture bitmap."""

    def CreateCompatibleDC(self, _dc):
        return 101

    def CreateCompatibleBitmap(self, _dc, _width, _height):
        return 202

    def SelectObject(self, _dc, _object):
        return None

    def DeleteObject(self, _object):
        return 1

    def DeleteDC(self, _dc):
        return 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_capture_print_window_reports_a_refused_select_object(monkeypatch):
    """A NULL SelectObject leaves the DC holding its 1x1 default bitmap, so the
    blank capture that follows was being blamed on PrintWindow."""

    class _StubUser32:
        def GetWindowDC(self, _hwnd):
            return 303

        def PrintWindow(self, _hwnd, _dc, _flags):
            return 0

        def ReleaseDC(self, _hwnd, _dc):
            return 1

    monkeypatch.setattr(capture, "_win32", lambda: (_StubUser32(), _FakeGdi32(), None))

    with pytest.raises(capture.CaptureError, match="SelectObject"):
        capture._capture_print_window(_telegram_window(), client_only=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_capture_print_window_deselects_the_bitmap_before_reading_it(monkeypatch):
    """GetDIBits on a bitmap still selected into a DC is forbidden by the API; it
    happens to work here and elsewhere returns 0 or stale scanlines. The lone
    deselect also proves the finally-block does not restore a second time."""
    calls = []

    class _RecordingGdi32:
        def CreateCompatibleDC(self, _dc):
            return 101

        def CreateCompatibleBitmap(self, _dc, _width, _height):
            return 202

        def SelectObject(self, _dc, handle):
            calls.append("select" if handle == 202 else "deselect")
            return 909

        def GetDIBits(self, *_args):
            calls.append("getdibits")
            return 1

        def DeleteObject(self, _handle):
            calls.append("delete-bitmap")
            return 1

        def DeleteDC(self, _dc):
            return 1

    class _RecordingUser32:
        def GetWindowDC(self, _hwnd):
            return 303

        def PrintWindow(self, _hwnd, _dc, _flags):
            calls.append("printwindow")
            return 1

        def ReleaseDC(self, _hwnd, _dc):
            return 1

    monkeypatch.setattr(capture, "_win32", lambda: (_RecordingUser32(), _RecordingGdi32(), None))

    image = capture._capture_print_window(_telegram_window(), client_only=False)

    assert image.size == (100, 100)
    assert calls == ["select", "printwindow", "deselect", "getdibits", "delete-bitmap"]


def test_looks_blank_is_exact_rather_than_a_downscaled_approximation():
    """The old 32x32 probe averaged a lone bright pixel away, so an almost-flat
    frame read as blank and was replaced by a screen grab of whatever overlapped."""
    almost_flat = Image.new("RGB", (1024, 1024), "black")
    almost_flat.putpixel((0, 0), (255, 255, 255))

    assert capture._looks_blank(almost_flat) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_window_enumeration_is_empty_for_an_unknown_process():
    assert capture.describe_windows("no-such-telegram-build.exe") == []

    with pytest.raises(capture.CaptureError, match="No visible"):
        capture.find_target_window(process_name="no-such-telegram-build.exe")


# --- Regressions for the Win32 correctness fixes -----------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_win32_handles_are_bound_to_pointer_types():
    """Handle-returning APIs must not fall back to ctypes' 32-bit c_int default.

    Unbound, GetWindowDC returned a truncated negative int on 64-bit Windows; it
    only round-tripped because sign extension happened to rebuild the real handle.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    user32, gdi32, kernel32 = capture._win32()

    assert user32.GetWindowDC.restype is wintypes.HDC
    assert user32.GetWindowDC.argtypes == [wintypes.HWND]
    assert gdi32.CreateCompatibleBitmap.restype is wintypes.HBITMAP
    assert gdi32.SelectObject.restype is wintypes.HGDIOBJ
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert user32.GetForegroundWindow.restype is wintypes.HWND
    for bound in (user32.GetWindowDC, gdi32.CreateCompatibleDC, kernel32.OpenProcess):
        assert bound.restype is not ctypes.c_int


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_win32_libraries_are_cached_singletons():
    """A fresh ctypes.WinDLL would drop the signatures bound above."""
    first = capture._win32()
    second = capture._win32()
    assert first is second


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_client_geometry_is_inside_the_window_rectangle():
    """client_only must report the client area, not the window rectangle."""
    windows = capture.list_windows()
    if not windows:
        pytest.skip("no Telegram Desktop window is open")
    window = windows[0]

    width, height, screen_rect = capture._client_geometry(window.hwnd)
    assert 0 < width <= window.width
    assert 0 < height <= window.height
    # The client area starts at or inside the window's top-left corner.
    assert screen_rect[0] >= window.rect[0]
    assert screen_rect[1] >= window.rect[1]
    assert screen_rect[2] - screen_rect[0] == width
    assert screen_rect[3] - screen_rect[1] == height


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_client_only_capture_matches_the_client_rectangle():
    if not capture.list_windows():
        pytest.skip("no Telegram Desktop window is open")

    full, window, full_meta = capture.capture_window(client_only=False)
    client, _window, client_meta = capture.capture_window(client_only=True)
    expected_width, expected_height, _rect = capture._client_geometry(window.hwnd)

    assert client.size == (expected_width, expected_height)
    assert client.size != full.size, "client_only returned the whole window"
    assert client_meta["captured_area"] == "client"
    assert full_meta["captured_area"] == "window"
    assert client_meta["client_offset_in_window"]["y"] > 0  # title bar excluded


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_repeated_captures_do_not_leak_gdi_objects():
    """DeleteObject silently fails on a bitmap still selected into a DC."""
    import ctypes
    import ctypes.wintypes as wintypes

    if not capture.list_windows():
        pytest.skip("no Telegram Desktop window is open")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()

    def gdi_objects():
        return user32.GetGuiResources(process, 0)  # GR_GDIOBJECTS

    # Prove the probe can see a leak before trusting it to report none. The count
    # is 0 until this process owns a GDI object, so a bare reading proves nothing.
    screen_dc = user32.GetDC(None)
    control_before = gdi_objects()
    leaked = [gdi32.CreateCompatibleBitmap(screen_dc, 8, 8) for _ in range(10)]
    control_delta = gdi_objects() - control_before
    for handle in leaked:
        gdi32.DeleteObject(handle)
    if control_delta < 8:
        pytest.skip(
            "GetGuiResources does not track GDI objects here; a result would be meaningless"
        )

    capture.capture_window()  # warm up lazy imports and bindings
    before = gdi_objects()
    for _ in range(15):
        capture.capture_window()
    assert gdi_objects() - before <= 2
