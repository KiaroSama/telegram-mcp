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
_WIN32: Optional[tuple[Any, Any, Any]] = None


class CaptureError(RuntimeError):
    """Raised when a window cannot be located or captured."""


_WINTYPES: Any = None


def _wintypes() -> Any:
    """``ctypes.wintypes``, fetched through a seam rather than imported inline.

    The module imports only on Windows, so every call site used to import it
    locally and the surrounding logic became unreachable everywhere else. That
    logic is not Windows-specific and is where the real bugs have been: the
    enumeration that stopped at the first failing window, the rectangle that came
    back zero-filled for a window destroyed mid-enumeration. Routing the three
    structures actually needed (DWORD, RECT, POINT) through here lets a test
    substitute them and exercise all of it on any platform.
    """
    global _WINTYPES
    if _WINTYPES is None:
        import ctypes.wintypes as wintypes

        _WINTYPES = wintypes
    return _WINTYPES


def _enum_callback(function: Any) -> Any:
    """Wrap an EnumWindows callback in its stdcall thunk.

    Split out for the same reason as :func:`_wintypes`: ``ctypes.WINFUNCTYPE``
    exists only on Windows, and it is the single line that would otherwise keep
    the whole of :func:`list_windows` off every other platform.
    """
    wintypes = _wintypes()
    return ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(function)


def _win32() -> tuple[Any, Any, Any]:
    """Return the bound ``(user32, gdi32, kernel32)`` libraries.

    Declaring argtypes/restype is not optional here. Without it ctypes assumes
    ``c_int`` for arguments and return values, and on 64-bit Windows every
    HWND/HDC/HBITMAP is a 64-bit pointer: an unbound ``GetWindowDC`` returned
    -1677648242 on this machine where the real handle is 18446744071813798555. It
    round-trips only while the handle's upper 32 bits are all ones (sign extension
    rebuilds it by luck) and silently produces an invalid handle when they are not.

    The libraries are cached module-level singletons because ``ctypes.WinDLL()``
    builds a *new* object on every call and function signatures are cached per
    object — binding a throwaway instance would leave every other call site
    unbound.
    """
    global _WIN32
    if _WIN32 is not None:
        return _WIN32

    import ctypes.wintypes as wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    signatures = (
        (user32, "EnumWindows", [ctypes.c_void_p, wintypes.LPARAM], wintypes.BOOL),
        (user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        (user32, "IsIconic", [wintypes.HWND], wintypes.BOOL),
        (user32, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
        (user32, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user32, "GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (
            user32,
            "GetWindowThreadProcessId",
            [wintypes.HWND, wintypes.LPDWORD],
            wintypes.DWORD,
        ),
        (user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user32, "GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user32, "ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        (user32, "GetForegroundWindow", [], wintypes.HWND),
        (user32, "GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
        (user32, "GetWindowDC", [wintypes.HWND], wintypes.HDC),
        (user32, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int),
        (user32, "PrintWindow", [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
        (gdi32, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
        (
            gdi32,
            "CreateCompatibleBitmap",
            [wintypes.HDC, ctypes.c_int, ctypes.c_int],
            wintypes.HBITMAP,
        ),
        (gdi32, "SelectObject", [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        (gdi32, "DeleteObject", [wintypes.HGDIOBJ], wintypes.BOOL),
        (gdi32, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
        (
            gdi32,
            "GetDIBits",
            [
                wintypes.HDC,
                wintypes.HBITMAP,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.UINT,
            ],
            ctypes.c_int,
        ),
        (
            kernel32,
            "OpenProcess",
            [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
            wintypes.HANDLE,
        ),
        (
            kernel32,
            "QueryFullProcessImageNameW",
            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD],
            wintypes.BOOL,
        ),
        (kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    )
    for library, name, argtypes, restype in signatures:
        function = getattr(library, name)
        function.argtypes = argtypes
        function.restype = restype

    _WIN32 = (user32, gdi32, kernel32)
    return _WIN32


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
    wintypes = _wintypes()

    _user32, _gdi32, kernel32 = _win32()
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
            if not path or os.path.basename(path).lower() != target:
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
                f"(SelectObject failed, error {ctypes.get_last_error()})."
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


def describe_windows(process_name: str = DEFAULT_PROCESS_NAME) -> list[dict[str, Any]]:
    """Serializable window list, main window first, for the MCP tool layer."""
    windows = list_windows(process_name)
    described = []
    for index, window in enumerate(windows):
        data = window.to_dict()
        data["is_main"] = index == 0
        described.append(data)
    return described
