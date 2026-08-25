"""Visual access to the real Telegram Desktop rendering.

Its own package because capture, decode and encode are one coherent
responsibility with a single entry point, not because anything outside depends
on the boundary.
"""

from telegram_mcp.visual.capture import (
    CaptureError,
    capture_frames,
    capture_window,
    describe_windows,
    find_target_window,
    list_windows,
)
from telegram_mcp.visual.images import (
    MAX_IMAGE_DIMENSION,
    encode_image,
    fit_image,
    open_image_bytes,
)
from telegram_mcp.visual.frames import FrameExtractionError, extract_frames, ffmpeg_available

__all__ = [
    "CaptureError",
    "FrameExtractionError",
    "MAX_IMAGE_DIMENSION",
    "capture_frames",
    "capture_window",
    "describe_windows",
    "encode_image",
    "extract_frames",
    "ffmpeg_available",
    "find_target_window",
    "fit_image",
    "list_windows",
    "open_image_bytes",
]
