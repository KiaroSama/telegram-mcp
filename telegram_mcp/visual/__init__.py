"""Visual access to the real Telegram Desktop rendering.

Its own package because capture, decode and encode are one coherent
responsibility with a single entry point, not because anything outside depends
on the boundary.

The re-exports below are resolved on first use rather than at import. Every
decode and every capture now runs in a child process that imports one of these
modules and only one - and the eager version made each of those children pay for
all three. Measured on this machine: importing ``telegram_mcp.visual.images``
cost 1.37s through the eager package and 0.49s without it, and a preview decode
pays that once per document. Nothing else changes: ``from telegram_mcp.visual
import capture_window`` still works, and so does importing a submodule directly.
"""

import importlib
from typing import Any

# Public name -> the submodule that defines it.
_EXPORTS = {
    "CaptureError": "capture",
    "capture_frames": "capture",
    "capture_window": "capture",
    "describe_windows": "capture",
    "find_target_window": "capture",
    "list_windows": "capture",
    "MAX_IMAGE_DIMENSION": "images",
    "encode_image": "images",
    "fit_image": "images",
    "open_image_bytes": "images",
    "FrameExtractionError": "frames",
    "extract_frames": "frames",
    "extract_still": "frames",
    "ffmpeg_available": "frames",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    # Cached on the package, so the lookup happens once rather than per access.
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = sorted(_EXPORTS)
