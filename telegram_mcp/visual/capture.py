"""Capture the Telegram Desktop window exactly as it is rendered on screen.

Two capture methods, because they answer two different questions:

``window``
    ``PrintWindow`` with ``PW_RENDERFULLCONTENT``. Asks the window to redraw
    itself into an off-screen bitmap, so the result is Telegram's own pixels
    even when another application sits on top of it, and even when Telegram is
    not the foreground window. Verified working against Telegram Desktop's Qt
    window on Windows 11; this is the default.

``screen``
    Grab the screen rectangle the window occupies. This is literally what the
    user's monitor shows, including anything overlapping Telegram. Useful when
    the question is "what is on screen right now", misleading when the window
    is occluded.

Both preserve native resolution, real font rendering, line wrapping, theme,
bubbles, avatars, reactions and every other UI element, because no re-rendering
happens on our side.

Windows only. Every entry point raises :class:`CaptureError` with an actionable
message on other platforms so callers can degrade gracefully.

The ctypes half - loading user32/gdi32/kernel32 and declaring signatures that do
not truncate a 64-bit handle - is :mod:`telegram_mcp.visual.winapi`. What is left
here is the part with opinions: which window, how big it may be, and whether what
came back is a picture of anything.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from telegram_mcp.visual.winapi import (
    _ensure_dpi_awareness,
    _enum_callback,
    _last_error,
    _process_image_path,
    _win32,
    _wintypes,
)

CAPTURE_METHODS = ("window", "screen")
DEFAULT_PROCESS_NAME = os.getenv("TELEGRAM_DESKTOP_PROCESS", "Telegram.exe")

# --- what a capture may allocate, before it allocates it -----------------------
#
# The bitmap and the readback buffer are built from the window rectangle exactly
# as the OS reports it, and the only size check used to happen afterwards, in the
# downscale. So the numbers below are the first thing that looks at the rectangle
# at all: a window claiming 100000x100000 is 40 GB of RGBA requested before
# anything questions it.
#
# 16384 is comfortably past any real display arrangement and is also about where
# GDI bitmap creation gives out, so a larger side is a broken or hostile
# rectangle rather than a big monitor.
MAX_CAPTURE_SIDE = 16_384

# The product matters separately: two legal sides still multiply. A triple-4K
# desktop is ~25 megapixels, so 32 million (128 MB of BGRA) leaves real room and
# still refuses the absurd.
MAX_CAPTURE_PIXELS = 32_000_000

# What one CALL may hand back, across every frame in it. get_telegram_frames
# multiplies a single capture by up to eight, and native_resolution removes the
# downscale, so the per-frame limits multiplied out to something no reply should
# carry. 48 MB of encoded image is already far past what a model can use.
MAX_CAPTURE_RESPONSE_BYTES = 48 * 1024 * 1024

# One frame's own allowance. PrintWindow is documented as synchronous with no
# promise of returning promptly - it runs on the target window's message loop -
# so this is a soft check between frames; the hard bound is the parent killing
# the whole helper.
CAPTURE_FRAME_SECONDS = 20.0

# Interpreter start plus Pillow's import, paid once for the whole helper.
CAPTURE_STARTUP_SECONDS = 20.0

# PrintWindow flags (winuser.h)
_PW_CLIENTONLY = 0x00000001
_PW_RENDERFULLCONTENT = 0x00000002


class CaptureError(RuntimeError):
    """Raised when a window cannot be located or captured."""


class CaptureCancelled(CaptureError):
    """The caller stopped waiting, so the capture helper was killed.

    Its own type so the tool layer can tell 'the caller went away' from 'this
    window could not be captured' - only the second is a fault worth reporting.
    It subclasses CaptureError so nothing that already handles a capture failure
    stops handling this one.
    """


@dataclass
class WindowInfo:
    """A visible top-level window belonging to the target process."""

    hwnd: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    is_foreground: bool
    is_minimized: bool
    dpi: Optional[int] = None
    process_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def area(self) -> int:
        return max(self.width, 0) * max(self.height, 0)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "rect": {
                "left": self.rect[0],
                "top": self.rect[1],
                "right": self.rect[2],
                "bottom": self.rect[3],
            },
            "width": self.width,
            "height": self.height,
            "is_foreground": self.is_foreground,
            "is_minimized": self.is_minimized,
        }
        if self.dpi:
            data["dpi"] = self.dpi
            data["scale_percent"] = round(self.dpi / 96 * 100)
        if self.process_path:
            # Executable name only: the full path is host layout the model has no
            # use for, and it ends up in every window listing and capture result.
            data["process"] = _process_basename(self.process_path)
        data.update(self.extra)
        return data


def _process_basename(path: str) -> str:
    """Last component of a WINDOWS path, whatever host is asking.

    These paths come from the Win32 API, so they are Windows paths even when the
    interpreter running this is not on Windows - and ``os.path.basename`` follows
    the HOST's separator rules. On Linux it treats ``C:\\T\\Telegram.exe`` as one
    long filename, the process-name comparison never matches, and every window is
    filtered out of the enumeration with no error to show for it.
    """
    return ntpath.basename(path)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise CaptureError(
            "Telegram Desktop capture is only implemented on Windows. "
            "Use the structured Telethon tools (inspect_message, get_media_thumbnail) "
            f"for content access on {sys.platform}."
        )


def list_windows(process_name: str = DEFAULT_PROCESS_NAME) -> list[WindowInfo]:
    """Visible top-level windows owned by ``process_name``, largest first.

    Matching on the executable path rather than the window class keeps this
    working across Telegram/Qt versions (the class name embeds the Qt build, e.g.
    ``Qt51519QWindowIcon``) and across portable installs.
    """
    _require_windows()
    _ensure_dpi_awareness()

    wintypes = _wintypes()

    user32, _gdi32, _kernel32 = _win32()
    foreground = user32.GetForegroundWindow()
    target = process_name.lower()
    windows: list[WindowInfo] = []

    def _collect(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = _process_image_path(pid.value)
            if not path or _process_basename(path).lower() != target:
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            class_buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buffer, 256)

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                # The window died between EnumWindows handing over the handle and
                # this call. rect is still zero-filled, and reporting it would
                # surface a real-looking window with width/height 0.
                return True

            dpi = None
            try:
                dpi = int(user32.GetDpiForWindow(hwnd)) or None
            except Exception:  # pragma: no cover - pre-Windows 10 1607
                dpi = None

            windows.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title_buffer.value,
                    class_name=class_buffer.value,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    is_foreground=int(hwnd) == int(foreground),
                    is_minimized=bool(user32.IsIconic(hwnd)),
                    dpi=dpi,
                    process_path=path,
                )
            )
        except Exception:
            # ctypes swallows an exception raised in a callback and converts the
            # return value to 0 — which STOPS EnumWindows. One transient failure
            # would silently truncate the whole window list, after which
            # find_target_window picks a popup or reports "no window found".
            pass
        return True

    callback = _enum_callback(_collect)
    user32.EnumWindows(callback, 0)
    windows.sort(key=lambda w: w.area, reverse=True)
    return windows


def find_target_window(
    hwnd: Optional[int] = None,
    process_name: str = DEFAULT_PROCESS_NAME,
) -> WindowInfo:
    """Resolve the window to capture: an explicit handle, else the main window.

    "Main window" is the largest visible one, which is the chat window rather
    than a media viewer popup or a tooltip.
    """
    windows = list_windows(process_name)
    if not windows:
        raise CaptureError(
            f"No visible {process_name} window found. Start Telegram Desktop, or set "
            "TELEGRAM_DESKTOP_PROCESS if the executable has a different name."
        )
    if hwnd is None:
        return windows[0]
    for window in windows:
        if window.hwnd == hwnd:
            return window
    known = ", ".join(str(w.hwnd) for w in windows)
    raise CaptureError(
        f"Window handle {hwnd} is not a visible {process_name} window. Known: {known}."
    )


def _client_geometry(hwnd: int) -> tuple[int, int, tuple[int, int, int, int]]:
    """Client-area size plus its rectangle in screen coordinates.

    The client area excludes the title bar, borders and resize grips, so it is
    both smaller than and offset from the window rectangle. Capturing it needs
    both numbers: the size for the bitmap, the screen rectangle for a screen grab.
    """
    wintypes = _wintypes()

    user32, _gdi32, _kernel32 = _win32()

    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise CaptureError(f"Could not read the client rectangle of window {hwnd}.")
    width, height = rect.right - rect.left, rect.bottom - rect.top

    origin = wintypes.POINT(rect.left, rect.top)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise CaptureError(
            f"Could not map the client area of window {hwnd} to screen coordinates."
        )
    return width, height, (origin.x, origin.y, origin.x + width, origin.y + height)


def _within_capture_limits(width: int, height: int) -> Optional[str]:
    """The refusal for a rectangle too big to allocate, or ``None`` to proceed.

    Returns a message rather than raising so the same rule can be asked as a
    question - the worker checks it, and so does anything deciding whether a
    capture is worth attempting.
    """
    if width > MAX_CAPTURE_SIDE or height > MAX_CAPTURE_SIDE:
        return (
            f"The window reports a {width}x{height} rectangle, and no side may exceed "
            f"{MAX_CAPTURE_SIDE} pixels; it is too large to capture. Nothing was allocated."
        )
    if width * height > MAX_CAPTURE_PIXELS:
        return (
            f"The window reports {width}x{height} ({width * height} pixels), above the "
            f"{MAX_CAPTURE_PIXELS}-pixel capture limit; it is too large to capture. "
            f"That bitmap alone would be {width * height * 4} bytes. Nothing was allocated. "
            "Use get_telegram_region for part of it."
        )
    return None


def check_response_bytes(produced: int, limit: Optional[int] = None) -> None:
    """Refuse a reply that has grown past what one call may hand back.

    Per-frame limits do not bound a call: eight frames each inside the native
    ceiling is eight times it, and the flag that removes the downscale is exactly
    the one a caller reaches for before asking for eight.

    ``limit`` defaults to ``None`` rather than to the constant, so the constant is
    read when the check runs. A module-level default is bound once at definition
    and would quietly stop being the single source of the number it names.
    """
    if limit is None:
        limit = MAX_CAPTURE_RESPONSE_BYTES
    if produced > limit:
        raise CaptureError(
            f"This capture produced {produced} bytes, above the {limit}-byte response "
            "ceiling for one call. Ask for fewer frames, a smaller max_dimension, drop "
            "native_resolution, or use get_telegram_region for the part that matters."
        )


def _capture_print_window(window: WindowInfo, client_only: bool):
    """Capture the window's own pixels via PrintWindow + GetDIBits."""
    wintypes = _wintypes()

    from PIL import Image

    user32, gdi32, _kernel32 = _win32()

    if client_only:
        # PW_CLIENTONLY renders only the client area, so the bitmap has to be that
        # size too. Keeping the window size would leave the excluded chrome as a
        # blank band and report dimensions the image does not actually have.
        width, height, _screen_rect = _client_geometry(window.hwnd)
    else:
        width, height = window.width, window.height
    if width <= 0 or height <= 0:
        raise CaptureError(f"Window {window.hwnd} has a zero-sized rectangle; nothing to capture.")
    # Before the device context, before the bitmap, before the buffer. Every one
    # of those is sized from these two numbers, and the only check used to be the
    # downscale that runs after all three already exist.
    refusal = _within_capture_limits(width, height)
    if refusal:
        raise CaptureError(refusal)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    window_dc = user32.GetWindowDC(window.hwnd)
    if not window_dc:
        raise CaptureError(f"Could not obtain a device context for window {window.hwnd}.")
    memory_dc = bitmap = previous_object = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            raise CaptureError("Could not allocate an off-screen bitmap for the capture.")
        # Keep what SelectObject displaces: DeleteObject refuses to free a bitmap
        # that is still selected into a DC, so dropping this handle leaks the
        # bitmap on every single capture.
        previous_object = gdi32.SelectObject(memory_dc, bitmap)
        if not previous_object:
            # NULL means the bitmap was never selected, so PrintWindow below would
            # render into the DC's 1x1 default bitmap and the blank result would be
            # blamed on PrintWindow.
            raise CaptureError(
                "Could not select the capture bitmap into the device context "
                f"(SelectObject failed, error {_last_error()})."
            )

        flags = _PW_RENDERFULLCONTENT | (_PW_CLIENTONLY if client_only else 0)
        if not user32.PrintWindow(window.hwnd, memory_dc, flags):
            raise CaptureError(
                "PrintWindow refused to render the window. Retry with method='screen' "
                "after bringing Telegram to the foreground."
            )

        # GetDIBits must not be called on a bitmap that is still selected into a
        # DC. It happens to work on this driver stack; where it does not it either
        # returns 0 or hands back stale scanlines that pass _looks_blank and
        # become a plausible-looking screenshot of the wrong thing.
        gdi32.SelectObject(memory_dc, previous_object)
        previous_object = None

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative => top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise CaptureError("Reading the captured bitmap failed (GetDIBits).")

        return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert(
            "RGB"
        )
    finally:
        if memory_dc and previous_object:
            gdi32.SelectObject(memory_dc, previous_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(window.hwnd, window_dc)


def _capture_screen_region(window: WindowInfo, client_only: bool = False):
    from PIL import ImageGrab

    if window.is_minimized:
        raise CaptureError(
            f"Window {window.hwnd} is minimized, so its screen area shows other windows. "
            "Use method='window' to capture Telegram's own rendering instead."
        )
    box = _client_geometry(window.hwnd)[2] if client_only else window.rect
    # The same check the PrintWindow path makes, for the same reason: ImageGrab
    # allocates the whole box before anything downstream sees a pixel, so a
    # rectangle nobody questioned is a rectangle nobody bounded.
    refusal = _within_capture_limits(box[2] - box[0], box[3] - box[1])
    if refusal:
        raise CaptureError(refusal)
    image = ImageGrab.grab(bbox=box, all_screens=True)
    return image.convert("RGB")


def _looks_blank(image) -> bool:
    """True when the capture came back as a single flat colour.

    A GPU-composited window that refuses to render into an off-screen DC yields a
    uniformly black (or white) bitmap. Cheap to detect on a downscaled copy, and
    it is the difference between returning a useless black rectangle and falling
    back to the screen grab.
    """
    # Both callers already hand this an RGB image, so convert("RGB") was a pure
    # full-frame copy (~25 MB on a 4K window) and resize then resampled every
    # source pixel — twice the memory traffic of the capture itself, paid once per
    # frame (up to 8 per get_telegram_frames call). getextrema() is one C pass
    # over the bands with no allocation, and exact rather than approximate.
    return all(low == high for low, high in image.getextrema())


def capture_window(
    hwnd: Optional[int] = None,
    method: str = "window",
    process_name: str = DEFAULT_PROCESS_NAME,
    client_only: bool = False,
    region: Optional[tuple[int, int, int, int]] = None,
) -> tuple[Any, WindowInfo, dict[str, Any]]:
    """Capture a Telegram Desktop window.

    Args:
        hwnd: Explicit window handle; the main window is used when omitted.
        method: ``window`` (PrintWindow, occlusion-proof) or ``screen`` (raw screen area).
        process_name: Executable name to search for.
        client_only: Exclude the title bar and borders.
        region: ``(left, top, right, bottom)`` in window-relative pixels, cropped
            after capture.

    Returns:
        ``(PIL.Image, WindowInfo, capture_metadata)``.
    """
    _require_windows()
    if method not in CAPTURE_METHODS:
        raise CaptureError(
            f"Unknown capture method {method!r}. Expected one of: {', '.join(CAPTURE_METHODS)}."
        )

    window = find_target_window(hwnd=hwnd, process_name=process_name)
    _ensure_dpi_awareness()

    meta: dict[str, Any] = {"method": method, "client_only": client_only}
    if method == "window":
        image = _capture_print_window(window, client_only=client_only)
        if _looks_blank(image):
            if window.is_minimized:
                # PrintWindow has no client surface to render for a minimized
                # window, so the "capture" is a black rectangle. Returning it
                # silently lets the model describe an empty frame as the chat,
                # and the screen fallback below refuses minimized windows too.
                raise CaptureError(
                    f"Window {window.hwnd} is minimized and rendered a blank frame. "
                    "Restore the Telegram window and retry."
                )
            # Hardware-accelerated windows occasionally decline to redraw off-screen.
            # The fallback keeps client_only, so the caller still gets the area it
            # asked for rather than a silently different framing.
            meta["fallback"] = "window capture returned a blank frame; used the screen region"
            meta["method"] = "screen"
            image = _capture_screen_region(window, client_only=client_only)
    else:
        image = _capture_screen_region(window, client_only=client_only)

    # Report what was actually captured: with client_only the image is the client
    # area, which is smaller than and offset from the window rectangle.
    meta["full_size"] = {"width": image.width, "height": image.height}
    if client_only:
        meta["captured_area"] = "client"
        client_width, client_height, client_rect = _client_geometry(window.hwnd)
        meta["client_rect"] = {
            "left": client_rect[0],
            "top": client_rect[1],
            "right": client_rect[2],
            "bottom": client_rect[3],
        }
        meta["client_offset_in_window"] = {
            "x": client_rect[0] - window.rect[0],
            "y": client_rect[1] - window.rect[1],
        }
    else:
        meta["captured_area"] = "window"

    if region:
        left, top, right, bottom = region
        if right <= left or bottom <= top:
            raise CaptureError(
                f"Invalid region {region}: right must exceed left and bottom must exceed top."
            )
        box = (
            max(0, min(left, image.width)),
            max(0, min(top, image.height)),
            max(0, min(right, image.width)),
            max(0, min(bottom, image.height)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            raise CaptureError(
                f"Region {region} lies outside the {image.width}x{image.height} window."
            )
        image = image.crop(box)
        meta["region"] = {"left": box[0], "top": box[1], "right": box[2], "bottom": box[3]}

    return image, window, meta


# Mirrors capture_worker's exit codes. Duplicated rather than imported so the
# parent never loads the worker module to read its own child's result.


# Enumeration is cheap when the windows answer and unbounded when one does not,
# so it gets a bound of its own rather than a share of the capture's.


def describe_windows(process_name: str = DEFAULT_PROCESS_NAME) -> list[dict[str, Any]]:
    """Serializable window list, main window first, for the MCP tool layer."""
    windows = list_windows(process_name)
    described = []
    for index, window in enumerate(windows):
        data = window.to_dict()
        data["is_main"] = index == 0
        described.append(data)
    return described
