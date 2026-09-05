"""What one decode may spend, and the runner that enforces it.

Split out of :mod:`telegram_mcp.visual.frames` because the three decoders there
(Pillow, rlottie, ffmpeg) share exactly one thing, and it is this: a
request-wide clock, a request-wide byte allowance, a cancellation flag, and a
runner that hands every child the smaller of every bound in play. The decoders
have nothing else in common - different formats, different child processes,
different failure vocabularies - so the part they do share is worth naming.

The distinction this file is built around is that a per-call timeout bounds ONE
subprocess and nothing more. Ten frames each allowed 30 seconds, after a 15
second probe, is 315 seconds a single caller can ask for; ten frames that each
decode quickly at 50 megapixels sit inside every timeout and still hold ~1.5 GB
of PNG in a list before anything downstream gets to shrink them. Both ceilings
therefore belong to the REQUEST - built once in ``extract_frames``, passed down,
and consulted by every subprocess along the way.

What belongs here: anything that decides what a decode may consume, what ends
it, and how much of what it says back may reach the caller. What does not: what
a particular format decodes into. That stays with its decoder.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from typing import Optional

from telegram_mcp.visual.bounded_process import (
    Completed,
    ProcessBudgetExhausted,
    ProcessCancelled,
    ProcessError,
    run_bounded,
)

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


class FrameExtractionError(RuntimeError):
    """Raised when frames cannot be extracted from a media file."""


class DecodingCancelled(FrameExtractionError):
    """The caller stopped waiting before the decode finished.

    Its own type so the tool layer can tell 'the caller went away' apart from
    'this media could not be decoded' - only the second is a fault worth
    reporting. It subclasses FrameExtractionError so nothing that already
    handles a decode failure stops handling this one.
    """


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

    ``allowance`` is that ceiling, and it is a parameter rather than a constant
    because a call covering ten documents has to divide one pool between them.
    Left out it is the per-request maximum, which is what a single-document call
    is entitled to.
    """

    __slots__ = ("allowance", "deadline", "cancelled", "emitted")

    def __init__(
        self,
        deadline: float,
        cancelled: Optional[threading.Event] = None,
        allowance: Optional[int] = None,
    ) -> None:
        self.deadline = deadline
        self.cancelled = cancelled
        self.emitted = 0
        self.allowance = min(
            MAX_TOTAL_FRAME_BYTES, MAX_TOTAL_FRAME_BYTES if allowance is None else int(allowance)
        )

    @classmethod
    def for_request(
        cls,
        cancelled: Optional[threading.Event] = None,
        allowance: Optional[int] = None,
    ) -> "_Budget":
        return cls(time.monotonic() + FFMPEG_REQUEST_BUDGET_SECONDS, cancelled, allowance)

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.allowance - self.emitted)

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
        """Account for one emitted frame, refusing once the allowance is reached."""
        self.emitted += len(data)
        if self.emitted > self.allowance:
            raise FrameExtractionError(
                f"Decoded frames total {self.emitted} bytes, above the "
                f"{self.allowance}-byte budget for one request. Ask for fewer frames, "
                "or a smaller max_dimension."
            )


def _run(
    command: list[str],
    timeout: int,
    deadline: Optional[float] = None,
    cancelled: Optional[threading.Event] = None,
    max_output_bytes: Optional[int] = None,
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
            # What this CALL has left, not a constant. The fixed ceiling let every
            # decoder in a batch write up to 32 MB each, whatever the call's own
            # reservation had already committed - so the per-call budget bounded
            # what the workers were asked for and not what the pipe would accept.
            max_output_bytes=(
                MAX_DECODER_OUTPUT_BYTES
                if max_output_bytes is None
                else max(1, min(MAX_DECODER_OUTPUT_BYTES, int(max_output_bytes)))
            ),
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
