"""Running a capture in a child process, and holding it to a budget.

Split out of :mod:`telegram_mcp.visual.capture` along the seam that already
divides that feature in two. Everything left in ``capture`` runs INSIDE the
worker subprocess - it talks to Win32, walks windows, blits pixels. Nothing here
does: this is the parent half, and its whole job is to start that worker, give
it a deadline, and refuse an answer that is too big.

The parent/child line is the one that matters because the two halves fail
differently. A bug in the child is a wrong picture; a bug here is a subprocess
nobody is waiting on any more, which is why the exit codes are named constants
and why every path through ``capture_frames`` ends in a terminated tree.

Names are reached through the ``capture`` MODULE rather than imported from it.
That is deliberate: ``tests/test_capture_bounds.py`` patches
``capture.MAX_CAPTURE_RESPONSE_BYTES`` to a small number to prove the ceiling
fires, and a ``from ... import`` here would bind the real 48 MB at import time -
the patch would still succeed and the test would pass having proved nothing.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from telegram_mcp.visual.bounded_process import (
    ProcessCancelled,
    ProcessError,
    run_bounded,
)

from telegram_mcp.visual import capture

CAPTURE_EXIT_REFUSED = 3


CAPTURE_EXIT_IMAGE_REFUSED = 4


def _capture_worker_command(request: dict) -> list[str]:
    """argv for one capture_worker run.

    ``-m`` rather than a path so the child resolves ``telegram_mcp`` the same way
    the parent did, whether this is an installed package or a source checkout.
    Its own function because a test has to be able to substitute a child that
    hangs: a real PrintWindow cannot be relied on to misbehave on demand, and
    misbehaving is the case worth proving.
    """
    return [
        sys.executable,
        "-m",
        "telegram_mcp.visual.capture_worker",
        json.dumps(request, separators=(",", ":")),
    ]


def capture_frames(
    hwnd: Optional[int] = None,
    method: str = "window",
    process_name: Optional[str] = None,
    client_only: bool = False,
    region: Optional[tuple[int, int, int, int]] = None,
    image_format: str = "png",
    max_dimension: Optional[int] = None,
    native: bool = False,
    count: int = 1,
    interval_ms: float = 0.0,
    timeout: Optional[float] = None,
    cancelled: Optional[Any] = None,
) -> tuple[dict[str, Any], list[tuple[bytes, dict[str, Any]]]]:
    """Capture ``count`` frames in a child process, bounded and reaped.

    Blocking: call it off the event loop. Returns ``(window, [(bytes, meta)])``.

    The whole request is one child, so ``timeout`` is a bound on the CALL rather
    than on each frame - which is the only kind of bound that holds when a single
    PrintWindow is the thing that never returns.
    """
    capture._require_windows()
    if method not in capture.CAPTURE_METHODS:
        raise capture.CaptureError(
            f"Unknown capture method {method!r}. Expected one of: {', '.join(capture.CAPTURE_METHODS)}."
        )
    count = max(1, int(count))
    interval_ms = max(0.0, float(interval_ms))
    if timeout is None:
        # Startup once, then each frame's own allowance and the waits between
        # them. Anything past this is a capture that is not coming back.
        timeout = (
            capture.CAPTURE_STARTUP_SECONDS
            + count * capture.CAPTURE_FRAME_SECONDS
            + (count - 1) * interval_ms / 1000.0
        )

    request = {
        "hwnd": hwnd,
        "method": method,
        "process_name": process_name or capture.DEFAULT_PROCESS_NAME,
        "client_only": bool(client_only),
        "region": list(region) if region else None,
        "image_format": image_format,
        "max_dimension": max_dimension,
        "native": bool(native),
        "count": count,
        "interval_ms": interval_ms,
        "max_bytes": capture.MAX_CAPTURE_RESPONSE_BYTES,
    }
    try:
        result = run_bounded(
            _capture_worker_command(request),
            label="The Telegram window capture",
            timeout=timeout,
            cancelled=cancelled,
            # The reply is the response ceiling plus one header line; anything
            # past that is a helper that ignored its own limit.
            max_output_bytes=capture.MAX_CAPTURE_RESPONSE_BYTES + 64 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    except ProcessCancelled as error:
        raise capture.CaptureCancelled(str(error)) from error
    except ProcessError as error:
        raise capture.CaptureError(str(error)) from error

    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode in (CAPTURE_EXIT_REFUSED, CAPTURE_EXIT_IMAGE_REFUSED):
        # The worker's own message already says what to do about it.
        raise capture.CaptureError(stderr or "The capture was refused.")
    if result.returncode != 0:
        raise capture.CaptureError(
            stderr or f"The capture helper failed (exit {result.returncode})."
        )

    header_line, _, payload = (result.stdout or b"").partition(b"\n")
    try:
        header = json.loads(header_line or b"{}")
        window = header["window"]
        metas = list(header["frames"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise capture.CaptureError("The capture helper returned a reply this build cannot read.")

    frames, offset = [], 0
    for meta in metas:
        size = int(meta.get("bytes") or 0)
        chunk = payload[offset : offset + size]
        if len(chunk) != size:
            raise capture.CaptureError(
                f"The capture helper returned {len(payload)} bytes for {len(metas)} frame(s); "
                "the capture was cut short."
            )
        offset += size
        frames.append((chunk, meta))
    if not frames:
        raise capture.CaptureError("The capture helper produced no frames.")
    capture.check_response_bytes(sum(len(data) for data, _meta in frames))
    return window, frames


LIST_WINDOWS_TIMEOUT_SECONDS = 30.0


def list_windows_bounded(
    process_name: Optional[str] = None,
    timeout: float = LIST_WINDOWS_TIMEOUT_SECONDS,
    cancelled: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """``describe_windows`` in a child process, bounded and reaped.

    Blocking: call it off the event loop. The enumeration reads each window's
    title, and ``GetWindowTextW`` waits for the window to answer a message - so a
    hung Telegram blocks the listing as completely as it blocks a capture, and on
    a thread there is nothing to interrupt.
    """
    capture._require_windows()
    request = {"job": "list", "process_name": process_name or capture.DEFAULT_PROCESS_NAME}
    try:
        result = run_bounded(
            _capture_worker_command(request),
            label="The Telegram window listing",
            timeout=timeout,
            cancelled=cancelled,
            # A few hundred bytes per window; a megabyte is thousands of them.
            max_output_bytes=1024 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    except ProcessCancelled as error:
        raise capture.CaptureCancelled(str(error)) from error
    except ProcessError as error:
        raise capture.CaptureError(str(error)) from error

    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise capture.CaptureError(
            stderr or f"The window listing failed (exit {result.returncode})."
        )
    try:
        return list(json.loads(result.stdout or b"{}")["windows"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise capture.CaptureError("The window listing returned a reply this build cannot read.")
