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
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

CAPTURE_METHODS = ("window", "screen")
DEFAULT_PROCESS_NAME = os.getenv("TELEGRAM_DESKTOP_PROCESS", "Telegram.exe")

# PrintWindow flags (winuser.h)
_PW_CLIENTONLY = 0x00000001
_PW_RENDERFULLCONTENT = 0x00000002

# OpenProcess access right that works without elevation (Vista+).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_DPI_AWARENESS_CONFIGURED = False


class CaptureError(RuntimeError):
    """Raised when a window cannot be located or captured."""


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
            data["process"] = os.path.basename(self.process_path)
        data.update(self.extra)
        return data


def _require_windows() -> None:
    if sys.platform != "win32":
        raise CaptureError(
            "Telegram Desktop capture is only implemented on Windows. "
            "Use the structured Telethon tools (inspect_message, get_media_thumbnail) "
            f"for content access on {sys.platform}."
        )


def _ensure_dpi_awareness() -> None:
    """Opt into per-monitor DPI awareness so captures are at native resolution.

    Without this, Windows lies to a non-DPI-aware process: window rectangles come
    back in virtualized coordinates and screen grabs are silently upscaled from a
    lower-resolution surface, producing blurry text at the wrong size. Best effort
    and idempotent; a failure only costs fidelity on scaled displays.
    """
    global _DPI_AWARENESS_CONFIGURED
    if _DPI_AWARENESS_CONFIGURED:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_AWARENESS_CONFIGURED = True


def _process_image_path(pid: int) -> str:
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows(process_name: str = DEFAULT_PROCESS_NAME) -> list[WindowInfo]:
    """Visible top-level windows owned by ``process_name``, largest first.

    Matching on the executable path rather than the window class keeps this
    working across Telegram/Qt versions (the class name embeds the Qt build, e.g.
    ``Qt51519QWindowIcon``) and across portable installs.
    """
    _require_windows()
    _ensure_dpi_awareness()

    import ctypes.wintypes as wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    foreground = user32.GetForegroundWindow()
    target = process_name.lower()
    windows: list[WindowInfo] = []

    def _collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = _process_image_path(pid.value)
        if not path or os.path.basename(path).lower() != target:
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

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
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_collect)
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


def _capture_print_window(window: WindowInfo, client_only: bool):
    """Capture the window's own pixels via PrintWindow + GetDIBits."""
    import ctypes.wintypes as wintypes

    from PIL import Image

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    width, height = window.width, window.height
    if width <= 0 or height <= 0:
        raise CaptureError(f"Window {window.hwnd} has a zero-sized rectangle; nothing to capture.")

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
    memory_dc = bitmap = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            raise CaptureError("Could not allocate an off-screen bitmap for the capture.")
        gdi32.SelectObject(memory_dc, bitmap)

        flags = _PW_RENDERFULLCONTENT | (_PW_CLIENTONLY if client_only else 0)
        if not user32.PrintWindow(window.hwnd, memory_dc, flags):
            raise CaptureError(
                "PrintWindow refused to render the window. Retry with method='screen' "
                "after bringing Telegram to the foreground."
            )

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
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(window.hwnd, window_dc)


def _capture_screen_region(window: WindowInfo):
    from PIL import ImageGrab

    if window.is_minimized:
        raise CaptureError(
            f"Window {window.hwnd} is minimized, so its screen area shows other windows. "
            "Use method='window' to capture Telegram's own rendering instead."
        )
    image = ImageGrab.grab(bbox=window.rect, all_screens=True)
    return image.convert("RGB")


def _looks_blank(image) -> bool:
    """True when the capture came back as a single flat colour.

    A GPU-composited window that refuses to render into an off-screen DC yields a
    uniformly black (or white) bitmap. Cheap to detect on a downscaled copy, and
    it is the difference between returning a useless black rectangle and falling
    back to the screen grab.
    """
    probe = image.convert("RGB").resize((32, 32))
    return all(low == high for low, high in probe.getextrema())


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

    meta: dict[str, Any] = {"method": method}
    if method == "window":
        image = _capture_print_window(window, client_only=client_only)
        if _looks_blank(image) and not window.is_minimized:
            # Hardware-accelerated windows occasionally decline to redraw off-screen.
            meta["fallback"] = "window capture returned a blank frame; used the screen region"
            meta["method"] = "screen"
            image = _capture_screen_region(window)
    else:
        image = _capture_screen_region(window)

    meta["full_size"] = {"width": image.width, "height": image.height}

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


def describe_windows(process_name: str = DEFAULT_PROCESS_NAME) -> list[dict[str, Any]]:
    """Serializable window list, main window first, for the MCP tool layer."""
    windows = list_windows(process_name)
    described = []
    for index, window in enumerate(windows):
        data = window.to_dict()
        data["is_main"] = index == 0
        described.append(data)
    return described
