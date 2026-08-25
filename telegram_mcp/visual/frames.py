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
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Optional

from telegram_mcp.visual.bounded_process import (
    Completed,
    ProcessBudgetExhausted,
    ProcessCancelled,
    ProcessError,
    run_bounded,
)

FFPROBE_TIMEOUT_SECONDS = 15
FFMPEG_FRAME_TIMEOUT_SECONDS = 30

# The ceiling for ONE request, across every probe and every frame it runs.
#
# Per-call timeouts alone do not bound a request: ten frames each allowed 30s,
# after a 15s probe, is 315 seconds of decoding that one caller can ask for -
# and the event loop is not the thing waiting, a worker thread is, so nothing
# upstream notices. Every subprocess below takes the smaller of its own timeout
# and what is left of this.
FFMPEG_REQUEST_BUDGET_SECONDS = 60

# The longest side ffmpeg is allowed to emit. Without it ffmpeg encoded a PNG at
# the SOURCE resolution and the tool layer downscaled afterwards - so an 8K video
# built an 8K PNG in memory first, per frame, purely to throw it away. Telegram's
# own preview ceiling is smaller than this, so nothing legitimate is lost.
FFMPEG_MAX_EMITTED_SIDE = 2048

# Below this a preview stops being one. A caller asking for 8 pixels has made a
# mistake, and honouring it would produce an image nothing can read.
MIN_EMITTED_SIDE = 64

# The whole request's decoded-frame ceiling, before anything downstream resizes.
# Per-frame limits do not bound a request: MAX_DECODED_PIXELS caps ONE frame, and
# MAX_FRAMES caps the count, so their product was the real ceiling and it is far
# larger than anything a reply can carry. 96 MB comfortably holds ten 2048-wide
# PNGs, which is the largest thing any decoder here is allowed to emit.
MAX_TOTAL_FRAME_BYTES = 96 * 1024 * 1024

# What one decoder call may hand back. Peak memory here is bounded by the input
# rather than by the pipe: measured on this ffmpeg, ffprobe's JSON is ~0.3x the
# size of the file it describes (400 audio streams in a 120 KB mkv produce 38 KB
# of output), and the ffmpeg calls emit a single -frames:v 1 PNG clamped to
# FFMPEG_MAX_EMITTED_SIDE. This ceiling states that invariant instead of leaving
# it to hold by accident, so a future flag change fails here rather than in the
# allocator.
MAX_DECODER_OUTPUT_BYTES = 32 * 1024 * 1024

# Enough diagnostic to identify a decoder failure, and no more: stderr is
# attacker-influenced text that ends up in a log line.
MAX_DECODER_STDERR_BYTES = 64 * 1024

# How often a running decode is re-checked for cancellation.
#
# Cancelling the coroutine cannot kill the worker thread the decode runs on -
# Python has no way to stop a thread from outside, and a started
# concurrent.futures job cannot be cancelled - so the thread has to look for
# itself. 100ms is well below anything a caller notices and costs nothing: the
# wait happens inside communicate(), not in a spin.
DECODER_POLL_SECONDS = 0.1
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


class DecodingCancelled(FrameExtractionError):
    """The caller stopped waiting before the decode finished.

    Its own type so the tool layer can tell 'the caller went away' apart from
    'this media could not be decoded' - only the second is a fault worth
    reporting. It subclasses FrameExtractionError so nothing that already
    handles a decode failure stops handling this one.
    """


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


def _emitted_side(max_side: Optional[int]) -> int:
    """The largest side a decoder should emit for this request.

    Extraction used to emit at FFMPEG_MAX_EMITTED_SIDE regardless and let the
    tool layer shrink afterwards, so asking for a 256px preview still paid for a
    2048px decode, a 2048px PNG encode, and a 2048px image held in a list -
    then threw most of it away. Lowering the public parameter has to lower the
    cost, or it is a formatting option pretending to be a budget.

    Never raises the ceiling: a caller asking for 4096 still gets 2048.
    """
    if not max_side or max_side <= 0:
        return FFMPEG_MAX_EMITTED_SIDE
    return max(MIN_EMITTED_SIDE, min(FFMPEG_MAX_EMITTED_SIDE, int(max_side)))


class _Budget:
    """One clock, one byte ceiling and one cancel flag for a whole request.

    Each decoder used to start its own ``FFMPEG_REQUEST_BUDGET_SECONDS`` clock on
    entry, so the ceiling was per-decoder rather than per-request: a .gif that
    Pillow refused and ffmpeg then retried got two full budgets in series, and the
    Pillow path had no clock at all. Building this once in ``extract_frames`` and
    passing it down is what makes the budget end-to-end.

    ``emitted`` is the second half of the ceiling. Time alone does not bound
    memory: ten frames that each decode quickly at 50 megapixels are within every
    timeout and still hold ~1.5 GB of PNG in a list before anything downstream
    gets a chance to shrink them.
    """

    __slots__ = ("deadline", "cancelled", "emitted")

    def __init__(self, deadline: float, cancelled: Optional[threading.Event] = None) -> None:
        self.deadline = deadline
        self.cancelled = cancelled
        self.emitted = 0

    @classmethod
    def for_request(cls, cancelled: Optional[threading.Event] = None) -> "_Budget":
        return cls(time.monotonic() + FFMPEG_REQUEST_BUDGET_SECONDS, cancelled)

    def check(self, done: int, total: int) -> None:
        """Stop between frames when the caller left or the request ran out of time.

        In-process decoders (Pillow, rlottie) cannot be interrupted mid-frame the
        way a subprocess can be killed, so a frame boundary is the finest
        granularity available. It is enough: the cost being avoided is the
        REMAINING frames.
        """
        if self.cancelled is not None and self.cancelled.is_set():
            raise DecodingCancelled(
                f"Frame extraction was cancelled after {done} of {total} frames."
            )
        if time.monotonic() > self.deadline:
            raise FrameExtractionError(
                f"Frame extraction passed its {FFMPEG_REQUEST_BUDGET_SECONDS}s budget after "
                f"{done} of {total} frames. Ask for fewer frames."
            )

    def charge(self, data: bytes) -> None:
        """Account for one emitted frame, refusing once the request total is reached."""
        self.emitted += len(data)
        if self.emitted > MAX_TOTAL_FRAME_BYTES:
            raise FrameExtractionError(
                f"Decoded frames total {self.emitted} bytes, above the "
                f"{MAX_TOTAL_FRAME_BYTES}-byte budget for one request. Ask for fewer frames, "
                "or a smaller max_dimension."
            )


def _run(
    command: list[str],
    timeout: int,
    deadline: Optional[float] = None,
    cancelled: Optional[threading.Event] = None,
) -> Completed:
    """Run a decoder, bounded by its own timeout, the request budget, and the caller.

    ``deadline`` is a ``time.monotonic()`` value; passing it is what stops N frames
    from costing N times the per-call timeout.

    ``cancelled`` is how a caller that has gone away reaches this. The decode runs
    on a worker thread and the subprocess is its child, so a cancelled coroutine
    frees the caller while leaving both running - the process kept burning CPU
    until its own timeout fired, which is up to the whole request budget after
    anyone stopped waiting for the answer.

    The mechanics live in :mod:`telegram_mcp.visual.bounded_process`, which the
    window capture uses too; this is the translation into the error types the tool
    layer already handles. The one behaviour that changed in the move is where the
    byte ceiling applies: ``communicate()`` handed back the whole reply and the
    length was compared afterwards, so a decoder that wrote 500 MB inside its time
    limit had already been given 500 MB by the time the check ran.
    """
    label = os.path.basename(command[0])
    try:
        return run_bounded(
            command,
            label=label,
            timeout=timeout,
            deadline=deadline,
            cancelled=cancelled,
            max_output_bytes=MAX_DECODER_OUTPUT_BYTES,
            # Truncated at the boundary rather than at each use, so no later
            # caller can forget and log the whole thing.
            max_stderr_bytes=MAX_DECODER_STDERR_BYTES,
            poll_seconds=DECODER_POLL_SECONDS,
        )
    except ProcessBudgetExhausted as error:
        raise FrameExtractionError(
            f"Frame extraction ran out of its {FFMPEG_REQUEST_BUDGET_SECONDS}s budget "
            "for this request. Ask for fewer frames."
        ) from error
    except ProcessCancelled as error:
        raise DecodingCancelled(str(error)) from error
    except ProcessError as error:
        raise FrameExtractionError(str(error)) from error


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
            "-show_entries",
            "format=duration:stream=avg_frame_rate,codec_name",
            "-of",
            "json",
            path,
        ],
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
        # ffprobe reports a rational, e.g. "30000/1001". A still image reports
        # "0/0", which must not become a division by zero.
        if codec is None:
            codec = stream.get("codec_name") or None
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


def _frames_with_pillow(
    path: str,
    count: int,
    budget: Optional["_Budget"] = None,
    max_side: Optional[int] = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    """Evenly spaced frames from an animated GIF/WebP/APNG using Pillow."""
    budget = budget or _Budget.for_request()
    side = _emitted_side(max_side)
    from PIL import Image, ImageSequence

    from telegram_mcp.visual.images import MAX_DECODED_PIXELS, ImageError, encode_image

    try:
        source = Image.open(path)
    except Exception as error:
        # UnidentifiedImageError and friends are neither FrameExtractionError nor
        # ImageError, so without this they escape every handler in the tool layer
        # and surface as an opaque internal error.
        raise DecoderMismatch(f"Pillow could not decode this media: {type(error).__name__}.")

    with source:
        # Frame COUNT was bounded below; frame SIZE was not, and this path opens
        # the file directly rather than through open_image_bytes, so its
        # MAX_DECODED_PIXELS ceiling never applied here. A 20000x20000 animated
        # WebP therefore decoded at 400 megapixels per frame.
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
        if total <= 1:
            raise FrameExtractionError("File is not animated; a single frame is all there is.")
        wanted = min(count, total)
        indexes = (
            sorted({round(i * (total - 1) / max(1, wanted - 1)) for i in range(wanted)})
            if wanted > 1
            else [0]
        )

        frames: list[tuple[bytes, dict[str, Any]]] = []
        try:
            for index, frame in enumerate(ImageSequence.Iterator(source)):
                # Checked before the skip, not after. ImageSequence.Iterator
                # DECODES every frame it walks past, so selecting 10 of 3000 still
                # costs 3000 decodes - and the check used to run only on the 10
                # that were kept. A cancelled caller waited out the other 2990.
                budget.check(len(frames), wanted)
                if index not in indexes:
                    continue
                data, meta = encode_image(
                    frame.convert("RGB"), image_format="png", max_dimension=side
                )
                budget.charge(data)
                meta.update({"frame_index": index, "frame_count": total, "source": "pillow"})
                frames.append((data, meta))
                if len(frames) >= wanted:
                    break
        except (FrameExtractionError, ImageError):
            raise
        except Exception as error:
            # A truncated animation opens cleanly and fails while decoding a LATER
            # frame, so wrapping only Image.open was not enough: a raw OSError
            # ("image file is truncated") and, at another truncation point, a bare
            # SyntaxError from Pillow's PNG plugin both escaped every handler in
            # the tool layer. Both are reachable from the wire — the suffix comes
            # from the sender's mime_type, so the sender picks the decoder.
            raise FrameExtractionError(
                f"Pillow failed after {len(frames)} frame(s) while decoding this animation: "
                f"{type(error).__name__}. The file is most likely truncated or corrupt."
            )
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
        )
        if result.returncode != 0 or not result.stdout:
            # The last iteration is the furthest-past-EOF seek and therefore the
            # least informative; keep the diagnostic from the first real attempt.
            if first_error is None:
                first_error = result.stderr
            continue
        budget.charge(result.stdout)
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


def extract_frames(
    data: bytes,
    suffix: str,
    count: int = 4,
    cancelled: Optional[threading.Event] = None,
    deadline: Optional[float] = None,
    max_side: Optional[int] = None,
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
        _Budget(deadline, cancelled) if deadline is not None else _Budget.for_request(cancelled)
    )

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = handle.name
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
        try:
            os.unlink(path)
        except OSError:
            pass
