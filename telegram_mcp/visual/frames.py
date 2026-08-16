"""Extract representative frames from animated Telegram media.

A single screenshot cannot represent a GIF, a video sticker or a video note, so
these helpers pull several frames out of the original asset:

* animated GIF / animated WebP -> Pillow, no external tools needed
* video, video note, video sticker (webm/mp4) -> ffmpeg, when it is on PATH
* ``.tgs`` animated stickers (gzipped Lottie vector JSON) -> rlottie, when the
  optional ``telegram-mcp[lottie]`` extra is installed. Without it the error
  names the two fallbacks: Telegram's static thumbnail, or capturing Telegram
  Desktop while the sticker plays.

Every subprocess call is bounded by a timeout and cleans up its temporary file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Optional

FFPROBE_TIMEOUT_SECONDS = 15
FFMPEG_FRAME_TIMEOUT_SECONDS = 30
MAX_FRAMES = 10

# n_frames is a header value (declared outright for APNG/WebP). The sample set
# always includes total - 1, and ImageSequence.Iterator decodes every frame in
# between before the loop can break, so an animation declaring a huge count pins
# a worker thread with no timeout — unlike the ffmpeg path, which _run bounds.
MAX_ANIMATION_FRAMES = 3000

PILLOW_ANIMATED_SUFFIXES = {".gif", ".webp", ".png", ".apng"}
FFMPEG_SUFFIXES = {".webm", ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".gif"}

# The suffix arrives from Telethon's File.ext, i.e. from the sender's mime_type or
# filename. It is concatenated into a real temp filename AND selects the decoder
# below, so anything outside the decodable set is replaced rather than trusted:
# ".webm:ads" would create an NTFS alternate data stream whose base file os.unlink
# then leaves behind, and a registry-derived ".hta" would put a shell-interpreted
# file in %TEMP% for the duration of the call.
DECODABLE_SUFFIXES = PILLOW_ANIMATED_SUFFIXES | FFMPEG_SUFFIXES | {".tgs"}


class FrameExtractionError(RuntimeError):
    """Raised when frames cannot be extracted from a media file."""


def ffmpeg_available() -> bool:
    """Whether ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


# Any absolute path in ffmpeg's diagnostics is our own temporary file, and on
# Windows it carries the OS account name (C:\Users\<user>\AppData\Local\Temp\...).
# That ends up verbatim in a tool result and therefore in the model's context.
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+")


def _safe_stderr(stderr: Optional[bytes], path: str = "", limit: int = 300) -> str:
    """ffmpeg's message with filesystem paths redacted and length bounded."""
    text = (stderr or b"").decode("utf-8", errors="replace").strip()
    # The regex below stops at the first whitespace, so on a Windows profile like
    # "C:\Users\John Smith\..." it redacts only "C:\Users\John" and leaks the
    # surname, the temp layout and the temp filename into the model's context.
    # We know the exact path we passed to ffmpeg, so remove that literally first.
    if path:
        text = text.replace(path, "<temp-file>")
    text = text.replace(tempfile.gettempdir(), "<temp-dir>")
    text = _ABSOLUTE_PATH_RE.sub("<temp-file>", text)
    return text if len(text) <= limit else text[:limit] + "…"


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise FrameExtractionError(
            f"{os.path.basename(command[0])} timed out after {timeout}s and was terminated."
        ) from error
    except FileNotFoundError as error:
        raise FrameExtractionError(
            f"{os.path.basename(command[0])} is not installed or not on PATH."
        ) from error


def probe_duration(path: str) -> Optional[float]:
    """Media duration in seconds via ffprobe, or ``None`` when unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    try:
        duration = json.loads(result.stdout or b"{}").get("format", {}).get("duration")
        return float(duration) if duration is not None else None
    except (ValueError, AttributeError, json.JSONDecodeError):
        return None


def _frames_with_pillow(path: str, count: int) -> list[tuple[bytes, dict[str, Any]]]:
    """Evenly spaced frames from an animated GIF/WebP/APNG using Pillow."""
    from PIL import Image, ImageSequence

    from telegram_mcp.visual.images import encode_image

    try:
        source = Image.open(path)
    except Exception as error:
        # UnidentifiedImageError and friends are neither FrameExtractionError nor
        # ImageError, so without this they escape every handler in the tool layer
        # and surface as an opaque internal error.
        raise FrameExtractionError(f"Pillow could not decode this media: {type(error).__name__}.")

    with source:
        total = getattr(source, "n_frames", 1)
        if total > MAX_ANIMATION_FRAMES:
            raise FrameExtractionError(
                f"Animation declares {total} frames, above the {MAX_ANIMATION_FRAMES} limit; "
                "refusing to decode it. Use get_media_thumbnail for a static preview."
            )
        if total <= 1:
            raise FrameExtractionError("File is not animated; a single frame is all there is.")
        wanted = min(count, total)
        indexes = (
            sorted({round(i * (total - 1) / max(1, wanted - 1)) for i in range(wanted)})
            if wanted > 1
            else [0]
        )

        frames: list[tuple[bytes, dict[str, Any]]] = []
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            if index not in indexes:
                continue
            data, meta = encode_image(frame.convert("RGB"), image_format="png")
            meta.update({"frame_index": index, "frame_count": total, "source": "pillow"})
            frames.append((data, meta))
            if len(frames) >= wanted:
                break
    if not frames:
        raise FrameExtractionError("Pillow decoded no frames from the animation.")
    return frames


def lottie_available() -> bool:
    """Whether the optional rlottie renderer is installed."""
    try:
        import rlottie_python  # noqa: F401
    except Exception:
        return False
    return True


# Telegram renders .tgs at 512x512; rendering larger buys nothing and the frames
# are downscaled again at the tool layer anyway.
LOTTIE_RENDER_SIZE = 512


def _frames_with_lottie(path: str, count: int) -> list[tuple[bytes, dict[str, Any]]]:
    """Rasterise a .tgs (gzipped Lottie) with rlottie, when it is installed."""
    from rlottie_python import LottieAnimation

    from telegram_mcp.visual.images import encode_image

    try:
        animation = LottieAnimation.from_tgs(path)
    except Exception as error:
        raise FrameExtractionError(
            f"rlottie could not open this .tgs animation: {type(error).__name__}."
        )

    total = animation.lottie_animation_get_totalframe() or 1
    wanted = min(count, total)
    indexes = (
        sorted({round(i * (total - 1) / max(1, wanted - 1)) for i in range(wanted)})
        if wanted > 1
        else [0]
    )

    frame_rate = animation.lottie_animation_get_framerate() or 0
    frames: list[tuple[bytes, dict[str, Any]]] = []
    for index in indexes:
        image = animation.render_pillow_frame(
            frame_num=index, width=LOTTIE_RENDER_SIZE, height=LOTTIE_RENDER_SIZE
        )
        data, meta = encode_image(image.convert("RGBA"), image_format="png")
        meta.update(
            {
                "frame_index": index,
                "frame_count": total,
                "source": "rlottie",
                "animation_format": "lottie_tgs",
            }
        )
        if frame_rate:
            meta["timestamp_seconds"] = round(index / frame_rate, 3)
        frames.append((data, meta))

    if not frames:
        raise FrameExtractionError("rlottie decoded no frames from this .tgs animation.")
    return frames


def _frames_with_ffmpeg(path: str, count: int) -> list[tuple[bytes, dict[str, Any]]]:
    """Evenly spaced frames from a video file using ffmpeg input seeking."""
    if not ffmpeg_available():
        raise FrameExtractionError(
            "ffmpeg is required to extract frames from video media but was not found on PATH. "
            "Install ffmpeg, or use get_media_thumbnail for a static preview."
        )

    duration = probe_duration(path)
    if duration and duration > 0:
        # Sample inside the clip: the very first and last frames are often black.
        timestamps = [round(duration * (i + 0.5) / count, 3) for i in range(count)]
    else:
        # Without ffprobe the duration is unknown. A 0.5s ladder assumes the clip
        # is at least count/2 seconds long, so every seek past EOF is dropped and
        # a 0.4s video note yields only the t=0 frame.
        timestamps = [round(i * 0.1, 3) for i in range(count)]

    frames: list[tuple[bytes, dict[str, Any]]] = []
    first_error: Optional[bytes] = None
    for index, timestamp in enumerate(timestamps):
        result = _run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                path,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            timeout=FFMPEG_FRAME_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or not result.stdout:
            # The last iteration is the furthest-past-EOF seek and therefore the
            # least informative; keep the diagnostic from the first real attempt.
            if first_error is None:
                first_error = result.stderr
            continue
        frames.append(
            (
                result.stdout,
                {
                    "frame_index": index,
                    "timestamp_seconds": timestamp,
                    "format": "png",
                    "mime_type": "image/png",
                    "source": "ffmpeg",
                },
            )
        )

    if not frames:
        raise FrameExtractionError(
            f"ffmpeg produced no frames for this media. "
            f"{_safe_stderr(first_error, path)}".strip()
        )
    return frames


def extract_frames(data: bytes, suffix: str, count: int = 4) -> list[tuple[bytes, dict[str, Any]]]:
    """Extract up to ``count`` representative frames from in-memory media bytes.

    Args:
        data: Raw media bytes as downloaded from Telegram.
        suffix: File extension including the dot, e.g. ``.webm``.
        count: Number of frames to aim for (capped at ``MAX_FRAMES``).

    Returns:
        A list of ``(png_bytes, metadata)`` tuples.
    """
    count = max(1, min(int(count), MAX_FRAMES))
    suffix = (suffix or "").lower()
    if suffix not in DECODABLE_SUFFIXES:
        suffix = ".bin"

    if suffix == ".tgs" and not lottie_available():
        raise FrameExtractionError(
            "Animated .tgs stickers are gzipped Lottie vector animations. Install the optional "
            "renderer with: pip install 'telegram-mcp[lottie]'. Without it, use "
            "get_media_thumbnail for the static preview, or get_telegram_frames to capture the "
            "sticker as Telegram Desktop actually plays it."
        )

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        if suffix == ".tgs":
            return _frames_with_lottie(path, count)
        if suffix in PILLOW_ANIMATED_SUFFIXES:
            try:
                return _frames_with_pillow(path, count)
            except FrameExtractionError:
                if suffix not in FFMPEG_SUFFIXES:
                    raise
        return _frames_with_ffmpeg(path, count)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
