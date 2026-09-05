"""The ctypes layer: loading Win32, and declaring what its calls really take.

Split out of :mod:`telegram_mcp.visual.capture` because it is the one part of
that file with a reason to change of its own - somebody needs a new Win32 call -
and because a 90-line signature table stood between the module docstring and the
first line about capturing anything.

Two silent failures live here, and they are why the seams have names.

Unbound ctypes assumes ``c_int`` for every argument and every return value,
while on 64-bit Windows an HWND, HDC or HBITMAP is a 64-bit pointer. An unbound
``GetWindowDC`` returned -1677648242 on this machine where the real handle is
18446744071813798555 - it round-trips only while the upper 32 bits are all ones
and sign extension rebuilds it by luck. The table below is therefore not
documentation; it is the thing that makes the handles real. The libraries are
cached because ``ctypes.WinDLL()`` builds a NEW object per call and signatures
are cached per object, so binding a throwaway leaves every other call site bare.

``ctypes.wintypes``, ``ctypes.WINFUNCTYPE`` and ``ctypes.get_last_error`` exist
only on Windows. Used inline they take the surrounding logic off every other
platform with them - and the surrounding logic is where the real bugs have been:
an enumeration that stopped at the first failing window, a rectangle read back
zero-filled for a window destroyed mid-enumeration. Named seams are what let a
test substitute them and exercise that logic anywhere.

What belongs here: anything whose entire content is talking to Win32 through
ctypes. What does not: what the answers MEAN. Which window to capture, what a
rectangle may cost, what a blank frame implies - all of that stays in capture.py,
which reads these names out of its OWN namespace, so patching ``capture._win32``
still reaches every caller of it there.

The exception is inside this file: ``_enum_callback`` and ``_process_image_path``
resolve ``_wintypes`` and ``_win32`` HERE, so a test substituting the Win32 layer
has to replace those two as well rather than only the libraries underneath them
- which is what ``_fake_enumeration`` in test_visual_capture.py already does.
"""

from __future__ import annotations

import ctypes
from typing import Any, Optional

# OpenProcess access right that works without elevation (Vista+).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_DPI_AWARENESS_CONFIGURED = False
_WIN32: Optional[tuple[Any, Any, Any]] = None


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


def _last_error() -> int:
    """``GetLastError()`` where it exists, and a harmless 0 where it does not.

    ``ctypes.get_last_error`` is imported under ``if _os.name == "nt"`` in
    CPython's own ``ctypes/__init__.py``, so touching it off Windows raises
    AttributeError - which turned a deliberate CaptureError about a refused
    SelectObject into an unhandled AttributeError instead.
    """
    getter = getattr(ctypes, "get_last_error", None)
    return getter() if getter is not None else 0


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
