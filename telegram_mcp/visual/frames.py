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
What those bounds ARE - the one clock, the one byte allowance and the runner that
hands each child the smaller of every limit in play - is
:mod:`telegram_mcp.visual.decode_budget`. This file is the decoders and the
ladder that picks between them.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Optional

from telegram_mcp.visual.decode_budget import (
    FrameExtractionError,
    _Budget,
    _emitted_side,
    _run,
    _safe_stderr,
)

# Re-exported rather than used here: frames.py is the import site the tool layer
# and the tests already reach for, and moving a name is not a reason to move
# every caller with it.
from telegram_mcp.visual.decode_budget import (  # noqa: F401
    FFMPEG_REQUEST_BUDGET_SECONDS,
    MAX_DECODER_OUTPUT_BYTES,
    DecodingCancelled,
)
from telegram_mcp.visual.images import MAX_DECODED_PIXELS

FFPROBE_TIMEOUT_SECONDS = 15
FFMPEG_FRAME_TIMEOUT_SECONDS = 30

# ffprobe answers with a few hundred bytes of JSON. Handing it the decoder ceiling
# described a budget nobody meant: 32 MB of metadata is not a thing that happens,
# and a ceiling that can never be reached bounds nothing.
MAX_PROBE_OUTPUT_BYTES = 1024 * 1024

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


class DecoderMismatch(FrameExtractionError):
    """Raised when *this* decoder cannot open the file — try the next one.

    The distinction matters because ``.gif`` is the one suffix both Pillow and
    ffmpeg claim. Catching the base class to decide whether to fall through
    treats "I refuse to decode this" as "I cannot read this format", so the
    frame-count refusal below was silently downgraded into a full ffmpeg decode
    of the very file it had just refused — and Pillow's accurate "not animated"
    diagnosis was replaced by ffmpeg's vaguer one. Only a decode *capability*
    failure justifies the fallback; a policy refusal is a final answer.
    """


def ffmpeg_available() -> bool:
    """Whether ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> tuple[Optional[int], Optional[int]]:
    """``(width, height)`` from a PNG's IHDR, without decoding a single pixel.

    The frame metadata has always carried the dimensions; they used to come from
    a second full decode-and-re-encode pass in the preview layer, which is the
    unbounded in-process Pillow work this whole change is removing. IHDR is the
    first chunk by specification, so 24 bytes answer the same question for free.
    """
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None, None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _probe(
    path: str,
    deadline: Optional[float] = None,
    cancelled: Optional[threading.Event] = None,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """``(duration_seconds, frame_rate, codec)`` from one ffprobe call.

    The frame rate comes from the same subprocess the duration already costs, and
    it is what makes the sampling ladder in :func:`_frames_with_ffmpeg` land on
    frames that exist: a container's duration includes the last frame's display
    time, so "just under the duration" can still be past the last real frame.

    The codec rides along for the same reason - free, from a call already being
    made - and decides whether the extractor has to name a decoder. ``None`` for
    anything that could not be determined.
    """
    if shutil.which("ffprobe") is None:
        return None, None, None
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            # Video streams only. Without it `streams` is every track in the
            # container in container order, and the codec below was taken off the
            # first one - so a VP9 webm that happens to carry sound reported
            # "opus", the `-c:v libvpx-vp9` in `_frames_with_ffmpeg` was not
            # named, and the sticker's alpha was dropped with nothing reporting a
            # problem. Measured on two files sharing one video stream: audio-first
            # came back RGB and opaque, video-only came back RGBA. It also shrinks
            # the reply, which is what MAX_PROBE_OUTPUT_BYTES exists to bound.
            "-select_streams",
            "v",
            "-show_entries",
            "format=duration:stream=avg_frame_rate,codec_name",
            "-of",
            "json",
            path,
        ],
        max_output_bytes=MAX_PROBE_OUTPUT_BYTES,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        deadline=deadline,
        cancelled=cancelled,
    )
    if result.returncode != 0:
        return None, None, None
    try:
        payload = json.loads(result.stdout or b"{}")
        raw_duration = payload.get("format", {}).get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None, None, None

    rate = None
    codec = None
    for stream in payload.get("streams") or []:
        if codec is None:
            codec = stream.get("codec_name") or None
        # ffprobe reports a rational, e.g. "30000/1001". A still image reports
        # "0/0", which must not become a division by zero.
        numerator, _, denominator = str(stream.get("avg_frame_rate") or "").partition("/")
        try:
            top, bottom = float(numerator), float(denominator or 1)
        except ValueError:
            continue
        if top > 0 and bottom > 0:
            rate = top / bottom
            break
    return duration, rate, codec


def probe_duration(path: str) -> Optional[float]:
    """Media duration in seconds via ffprobe, or ``None`` when unavailable."""
    return _probe(path)[0]


# Covers interpreter startup, Pillow's import and every requested frame in one
# child, so it is a whole-decode ceiling rather than a per-frame one. The request
# budget still caps it from above via _run's deadline.
PILLOW_DECODE_TIMEOUT_SECONDS = 45

# Mirrors pillow_worker's exit codes. Duplicated rather than imported so the
# parent never has to load the worker module to read its own child's result.
PILLOW_EXIT_MISMATCH = 3
PILLOW_EXIT_REFUSED = 4


def _pillow_worker_command(job: str, arguments: list) -> list[str]:
    """argv for one pillow_worker run.

    ``-m`` rather than a path so the child resolves ``telegram_mcp`` the same way
    the parent did, whether this is an installed package or a source checkout.
    Its own function because a test has to be able to substitute a child that
    never returns; that is the behaviour under test, and it cannot be provoked
    with a real decoder.
    """
    return [sys.executable, "-m", "telegram_mcp.visual.pillow_worker", job, *arguments]


def _pillow_limits_or_raise(source) -> int:
    """The two refusals that must happen on the declared size, before decoding.

    Kept in the parent as well as the worker so the rule has one statement a test
    can reach without spawning anything. ``Image.open`` reads the header only, so
    these numbers are available before the allocation they are guarding against.
    """
    width, height = source.size
    if width * height > MAX_DECODED_PIXELS:
        raise FrameExtractionError(
            f"Animation frames are {width}x{height} ({width * height} pixels), above "
            f"the {MAX_DECODED_PIXELS}-pixel decode limit; refusing to decode it. "
            "Use get_media_thumbnail for a static preview."
        )
    total = getattr(source, "n_frames", 1)
    if total > MAX_ANIMATION_FRAMES:
        raise FrameExtractionError(
            f"Animation declares {total} frames, above the {MAX_ANIMATION_FRAMES} limit; "
            "refusing to decode it. Use get_media_thumbnail for a static preview."
        )
    return total


def _pillow_reply(result, path: str, expect: str) -> list[tuple[bytes, dict[str, Any]]]:
    """Split the worker's header-plus-payload reply into ``(bytes, metadata)``."""
    if result.returncode == PILLOW_EXIT_MISMATCH:
        # Only "Pillow cannot read this format" is worth a second decoder. A
        # policy refusal is a decision about the content and a final answer.
        raise DecoderMismatch(
            _safe_stderr(result.stderr, path) or f"Pillow could not read this {expect}."
        )
    if result.returncode != 0:
        raise FrameExtractionError(
            _safe_stderr(result.stderr, path)
            or f"Pillow failed while decoding this {expect} (exit {result.returncode})."
        )

    header_line, _, payload = (result.stdout or b"").partition(b"\n")
    try:
        header = json.loads(header_line or b"{}")
        metas = list(header["metas"])
        total = int(header["total"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise FrameExtractionError("The image decoder returned a reply this build cannot read.")

    decoded: list[tuple[bytes, dict[str, Any]]] = []
    offset = 0
    for meta in metas:
        size = int(meta.get("bytes") or 0)
        chunk = payload[offset : offset + size]
        if len(chunk) != size:
            raise FrameExtractionError(
                f"The image decoder returned {len(payload)} bytes for {len(metas)} image(s); "
                "the decode was cut short."
            )
        offset += size
        meta.setdefault("frame_count", total)
        decoded.append((chunk, meta))
    if not decoded:
        raise FrameExtractionError(f"Pillow decoded nothing from this {expect}.")
    return decoded


def _frames_with_pillow(
    path: str,
    count: int,
    budget: Optional["_Budget"] = None,
    max_side: Optional[int] = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    """Evenly spaced frames of an animation, decoded in a process that can be killed.

    Pillow used to run here, on the caller's worker thread. Checking the budget
    between frames was the finest granularity available and it was not enough:
    ``ImageSequence.Iterator`` DECODES every frame it walks past, and one native
    decode that does not return never reaches the next boundary - so a single
    frame held the request past every deadline with the cancellation flag unread,
    and Python has no way to stop a thread from outside.

    The animation arrives from the wire, and the suffix comes from the sender's
    mime_type, so the sender chooses this decoder. Same conclusion as .tgs: run it
    where a hang is a PID.
    """
    budget = budget or _Budget.for_request()
    side = _emitted_side(max_side)
    result = _run(
        _pillow_worker_command(
            "frames",
            [path, str(count), str(side), str(budget.remaining_bytes), str(MAX_ANIMATION_FRAMES)],
        ),
        timeout=PILLOW_DECODE_TIMEOUT_SECONDS,
        deadline=budget.deadline,
        cancelled=budget.cancelled,
        max_output_bytes=budget.remaining_bytes,
    )
    decoded = _pillow_reply(result, path, "animation")
    for data, _meta in decoded:
        budget.charge(data)
    return decoded


def still_with_pillow(
    path: str,
    budget: Optional["_Budget"] = None,
    max_side: Optional[int] = None,
    image_format: str = "png",
) -> tuple[bytes, dict[str, Any]]:
    """One still image, decoded and re-encoded in a process that can be killed.

    The same argument as the animated path, and it applied here first: a still
    ran under ``asyncio.to_thread`` with no deadline and no way to terminate it,
    so a decoder that did not return held a thread for the life of the process.
    """
    budget = budget or _Budget.for_request()
    side = _emitted_side(max_side)
    result = _run(
        _pillow_worker_command(
            "still", [path, str(side), image_format, str(budget.remaining_bytes)]
        ),
        timeout=PILLOW_DECODE_TIMEOUT_SECONDS,
        deadline=budget.deadline,
        cancelled=budget.cancelled,
        max_output_bytes=budget.remaining_bytes,
    )
    data, meta = _pillow_reply(result, path, "image")[0]
    budget.charge(data)
    meta.pop("frame_count", None)
    return data, meta


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

# Covers interpreter startup plus every requested frame in one child, so it is
# a whole-render ceiling rather than a per-frame one. The request budget still
# caps it from above via _run's deadline.
LOTTIE_RENDER_TIMEOUT_SECONDS = 45

# Mirrors lottie_worker.EXIT_CANNOT_OPEN. Duplicated rather than imported: the
# worker is deliberately standalone so spawning it costs only the renderer.
LOTTIE_EXIT_CANNOT_OPEN = 3


def _frames_with_lottie(
    path: str,
    count: int,
    budget: Optional["_Budget"] = None,
    max_side: Optional[int] = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    """Rasterise a .tgs (gzipped Lottie) with rlottie, in a process that can be killed.

    The render runs in :mod:`telegram_mcp.visual.lottie_worker` rather than here.
    rlottie is native code, and native code called on a worker thread cannot be
    interrupted by anything Python can do - so a single frame that does not return
    used to hold the decode past every deadline, with the caller's cancellation
    event never read. Checking the budget between frames does not help when
    control never reaches the next frame.

    That is reachable from the wire: a Lottie repeater multiplies its group at
    render time and repeaters nest, so cost grows as ``copies ** depth`` while the
    file stays tiny. Measured with rlottie 1.3.8, a 332-byte .tgs holding three
    nested 150x repeaters kept one 512x512 frame busy for 12.1 seconds; a fourth
    level reaches half an hour.

    As a child process it is a PID, and :func:`_run` already bounds one, kills the
    whole thing and reaps it. The render size stays fixed at 512x512, so the
    worker's raw-RGBA reply is exactly 1 MiB per frame.
    """
    budget = budget or _Budget.for_request()
    # Telegram renders .tgs at 512; asking rlottie for less is cheaper, asking for
    # more only invents detail the vector never had.
    side = min(LOTTIE_RENDER_SIZE, _emitted_side(max_side))

    from telegram_mcp.visual.images import encode_image

    result = _run(
        [
            sys.executable,
            str(Path(__file__).with_name("lottie_worker.py")),
            path,
            str(count),
            str(side),
        ],
        timeout=LOTTIE_RENDER_TIMEOUT_SECONDS,
        deadline=budget.deadline,
        cancelled=budget.cancelled,
        max_output_bytes=budget.remaining_bytes,
    )
    if result.returncode == LOTTIE_EXIT_CANNOT_OPEN:
        raise FrameExtractionError(
            f"rlottie could not open this .tgs animation. "
            f"{_safe_stderr(result.stderr, path)}".strip()
        )
    if result.returncode != 0:
        raise FrameExtractionError(
            f"rlottie could not render this .tgs animation. "
            f"{_safe_stderr(result.stderr, path)}".strip()
        )

    header_line, _, payload = (result.stdout or b"").partition(b"\n")
    try:
        header = json.loads(header_line or b"{}")
        total = int(header["total"])
        indexes = [int(i) for i in header["indexes"]]
        frame_rate = float(header.get("framerate") or 0)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise FrameExtractionError("The .tgs renderer returned a reply this build cannot read.")

    if not total:
        # rlottie does not raise on garbage: it returns an animation reporting
        # totalframe=0, framerate=0. `or 1` turned that "I parsed nothing" signal
        # into a legitimate-looking one-frame animation and rendered a fully
        # transparent canvas, which the tool layer then labelled a successful
        # frame. A blank picture presented as the emoji's real content is worse
        # than an error, because nothing downstream can tell it is wrong.
        raise FrameExtractionError(
            "rlottie parsed no animation from this .tgs payload (it reports zero frames), so "
            "it is not a valid gzipped Lottie file. Any frame rendered from it would be blank."
        )

    stride = side * side * 4
    if len(payload) != stride * len(indexes):
        raise FrameExtractionError(
            f"The .tgs renderer returned {len(payload)} bytes for {len(indexes)} frame(s); "
            f"{stride * len(indexes)} were expected. The render was cut short."
        )

    from PIL import Image

    frames: list[tuple[bytes, dict[str, Any]]] = []
    for position, index in enumerate(indexes):
        budget.check(len(frames), len(indexes))
        rendered = Image.frombytes(
            "RGBA",
            (side, side),
            payload[position * stride : (position + 1) * stride],
        )
        # An empty frame is normal here and needs saying so. A message effect
        # BEGINS and ENDS transparent - measured on Telegram's own fire effect,
        # frame 0 of 181 has zero visible pixels and frame 180 has eight - so an
        # evenly spaced ladder legitimately lands on blank canvases at both ends.
        # Without this flag a caller sees a blank image next to full ones and
        # concludes the render failed, which is the wrong conclusion about a
        # correct render. getbbox() on the alpha channel is a C-level scan and
        # returns None only when every pixel is fully transparent.
        blank = rendered.getchannel("A").getbbox() is None
        data, meta = encode_image(rendered, image_format="png")
        budget.charge(data)
        meta.update(
            {
                "frame_index": index,
                "frame_count": total,
                "source": "rlottie",
                "animation_format": "lottie_tgs",
            }
        )
        if blank:
            meta["blank"] = True
            meta["blank_note"] = (
                "This frame is fully transparent, and that is the animation's own content at "
                "frame {index} of {total} - not a failed render. Effects and stickers commonly "
                "start and end on an empty canvas.".format(index=index, total=total)
            )
        if frame_rate:
            meta["timestamp_seconds"] = round(index / frame_rate, 3)
        frames.append((data, meta))

    if not frames:
        raise FrameExtractionError("rlottie decoded no frames from this .tgs animation.")
    return frames


def _frames_with_ffmpeg(
    path: str,
    count: int,
    budget: Optional["_Budget"] = None,
    max_side: Optional[int] = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    """Evenly spaced frames from a video file using ffmpeg input seeking.

    One budget covers the probe and every frame: the per-call timeouts bound a
    single subprocess, and a request asking for ten frames used to be able to
    spend all of them in series.
    """
    budget = budget or _Budget.for_request()
    side = _emitted_side(max_side)
    if not ffmpeg_available():
        raise FrameExtractionError(
            "ffmpeg is required to extract frames from video media but was not found on PATH. "
            "Install ffmpeg, or use get_media_thumbnail for a static preview."
        )

    duration, frame_rate, codec = _probe(
        path, deadline=budget.deadline, cancelled=budget.cancelled
    )
    if duration and duration > 0:
        # Sample inside the clip: the very first and last frames are often black.
        #
        # The ladder is spread over the last frame's start, not over the whole
        # duration. A container's duration includes how long the final frame is
        # DISPLAYED, so `duration * (count - 0.5) / count` can sit past the last
        # frame's PTS; that seek returns nothing, and the shortfall was silent —
        # 8 frames requested, 7 delivered, with no metadata saying which sample
        # was lost. Measured: a 1.2s 10fps clip lost its last sample every time.
        last_frame_start = duration - (1.0 / frame_rate) if frame_rate else duration
        span = max(0.0, last_frame_start)
        timestamps = [round(span * (i + 0.5) / count, 3) for i in range(count)]
    else:
        # Without ffprobe the duration is unknown. A 0.5s ladder assumes the clip
        # is at least count/2 seconds long, so every seek past EOF is dropped and
        # a 0.4s video note yields only the t=0 frame.
        timestamps = [round(i * 0.1, 3) for i in range(count)]

    # A VP9 WebM keeps its alpha in a SEPARATE layer, and ffmpeg's default vp9
    # decoder drops it silently - a transparent video sticker comes back as an
    # opaque square with no error anywhere. Naming libvpx-vp9 before -i keeps it.
    # Conditional because -c:v applies to every input, and this same function
    # extracts from h264 video notes, which libvpx-vp9 cannot decode at all.
    decoder = ["-c:v", "libvpx-vp9"] if codec == "vp9" else []

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
                *decoder,
                "-i",
                path,
                "-frames:v",
                "1",
                # Scale before the PNG encoder, not after. The box is clamped to the
                # SOURCE size with min(): force_original_aspect_ratio=decrease fits
                # to the box, so a bare 2048 box upscaled a 64x64 sticker to 2048
                # and destroyed its alpha test. Clamped, a small source is untouched.
                "-vf",
                (
                    f"scale=w='min(iw,{side})':h='min(ih,{side})'"
                    ":force_original_aspect_ratio=decrease"
                ),
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            timeout=FFMPEG_FRAME_TIMEOUT_SECONDS,
            deadline=budget.deadline,
            cancelled=budget.cancelled,
            max_output_bytes=budget.remaining_bytes,
        )
        if result.returncode != 0 or not result.stdout:
            # The last iteration is the furthest-past-EOF seek and therefore the
            # least informative; keep the diagnostic from the first real attempt.
            if first_error is None:
                first_error = result.stderr
            continue
        budget.charge(result.stdout)
        width, height = _png_size(result.stdout)
        frames.append(
            (
                result.stdout,
                {
                    "frame_index": index,
                    "timestamp_seconds": timestamp,
                    "format": "png",
                    "mime_type": "image/png",
                    "source": "ffmpeg",
                    "width": width,
                    "height": height,
                    "bytes": len(result.stdout),
                },
            )
        )

    if not frames:
        raise FrameExtractionError(
            f"ffmpeg produced no frames for this media. "
            f"{_safe_stderr(first_error, path)}".strip()
        )
    return frames


# How long to keep trying to delete a spooled file. On Windows a just-killed
# child can still hold the handle for a moment, and os.unlink fails with
# ERROR_SHARING_VIOLATION rather than waiting - so a single attempt swallowed by
# `except OSError` left the file behind on exactly the paths (timeout,
# cancellation) where cleanup matters most.
_UNSPOOL_SECONDS = 5.0
_UNSPOOL_POLL_SECONDS = 0.05


def _spool(data: bytes, suffix: str) -> str:
    """Write ``data`` where a decoder subprocess can open it by name."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        return handle.name


def _unspool(path: str) -> None:
    """Delete a spooled file, retrying briefly while a dying child still holds it."""
    deadline = time.monotonic() + _UNSPOOL_SECONDS
    while True:
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                return
            time.sleep(_UNSPOOL_POLL_SECONDS)


def extract_frames(
    data: bytes,
    suffix: str,
    count: int = 4,
    cancelled: Optional[threading.Event] = None,
    deadline: Optional[float] = None,
    max_side: Optional[int] = None,
    allowance: Optional[int] = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    """Extract up to ``count`` representative frames from in-memory media bytes.

    Args:
        data: Raw media bytes as downloaded from Telegram.
        suffix: File extension including the dot, e.g. ``.webm``.
        count: Number of frames to aim for (capped at ``MAX_FRAMES``).
        cancelled: Set by the caller when it stops waiting, so a decode that is
            no longer wanted stops instead of running to completion on a worker
            thread nobody is reading from.
        deadline: A ``time.monotonic()`` value shared with the caller, so decoding
            and whatever the caller does with the frames run under ONE clock. Left
            out, decoding gets its own ``FFMPEG_REQUEST_BUDGET_SECONDS``.
        max_side: The longest side the caller will actually keep. Decoding larger
            and shrinking afterwards is work and memory spent on pixels that are
            discarded, so this lowers the cost rather than only the output.
        allowance: Bytes of decoded output this call may produce. A call covering
            several documents divides one pool between them, so the ceiling has to
            arrive from above rather than be assumed to be the per-request maximum.

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

    # Built once, here: a .gif that Pillow refuses is retried with ffmpeg below,
    # and two decoders each starting their own clock gave that one request twice
    # the budget it is allowed.
    budget = (
        _Budget(deadline, cancelled, allowance)
        if deadline is not None
        else _Budget.for_request(cancelled, allowance)
    )

    path = _spool(data, suffix)
    try:
        if suffix == ".tgs":
            return _frames_with_lottie(path, count, budget, max_side)
        if suffix in PILLOW_ANIMATED_SUFFIXES:
            try:
                return _frames_with_pillow(path, count, budget, max_side)
            except DecoderMismatch:
                # Only "Pillow cannot read this format" is worth a second decoder.
                # A frame-count refusal or "not animated" is a decision about the
                # content, and .gif is in both suffix sets, so catching the base
                # class here handed the refused file straight to ffmpeg.
                if suffix not in FFMPEG_SUFFIXES:
                    raise
        return _frames_with_ffmpeg(path, count, budget, max_side)
    finally:
        _unspool(path)


def extract_still(
    data: bytes,
    cancelled: Optional[threading.Event] = None,
    deadline: Optional[float] = None,
    max_side: Optional[int] = None,
    allowance: Optional[int] = None,
    image_format: str = "png",
) -> tuple[bytes, dict[str, Any]]:
    """One still image from in-memory bytes, decoded where a hang can be killed.

    The still path is the one that had no bound at all: ``open_image_bytes`` and
    ``encode_image`` ran under ``asyncio.to_thread``, so a decoder that did not
    return held a worker thread for the life of the process with no deadline, no
    cancellation and nothing able to stop it.
    """
    budget = (
        _Budget(deadline, cancelled, allowance)
        if deadline is not None
        else _Budget.for_request(cancelled, allowance)
    )
    # The suffix is only a hint to Pillow, which sniffs the content anyway; a
    # fixed one keeps sender-controlled text out of a real filename.
    path = _spool(data, ".img")
    try:
        return still_with_pillow(path, budget, max_side, image_format)
    finally:
        _unspool(path)
