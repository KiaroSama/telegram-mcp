"""Turning one Telegram asset into preview images and an honest record.

Everything here answers the same question — *what does this asset look like, and
what is the picture actually worth?* — for the three assets whose answer is not
simply "the file": a still, an animation, and the two Telegram ships alongside
something else (a premium sticker's separate effect, a custom emoji document).

The honesty is the point, and it is why these live together. Each record says
what its picture is NOT: `composite_fidelity: "asset-only"` for an effect
Telegram draws over a sticker, `color_fidelity: "context-neutral"` for an emoji
Telegram recolours to match surrounding text, `preview_source: "thumbnail"` when
the animation could not be rendered at all. A caller that mistakes any of these
for the finished appearance has been misled, so the labels travel with the bytes
rather than being added by whichever tool happened to ask.

Separate from ``tools/`` because ``tools/effects.py`` needs the encoders too, and
a tool module importing another tool module's privates is how that used to be
spelled. The split mirrors the two already in place: ``text_fidelity`` holds the
string rules under ``message_view``, ``media_transfer`` holds the bounded
download under these previews.
"""

import threading
import time

from telegram_mcp.runtime import *
from telegram_mcp.effect_catalog import sniff_asset_format
from telegram_mcp.media_transfer import (
    MAX_FRAME_SOURCE_BYTES,
    _declared_sizes,
    _download_size_capped,
    _download_thumb_capped,
    _download_whole_capped,
    _select_thumb,
    with_reference_retry,
)
from telegram_mcp.message_view import display_name
from telegram_mcp.visual.bounded_process import run_cancellable
from telegram_mcp.visual.frames import (
    FFMPEG_REQUEST_BUDGET_SECONDS,
    DecodingCancelled,
    FrameExtractionError,
    extract_frames,
    extract_still,
    lottie_available,
)
from telegram_mcp.visual.images import (  # noqa: F401
    ImageError,
    bounded_dimension,
    encode_image,
    open_image_bytes,
)

from mcp.server.fastmcp import Image

# One item's own share of the clock, when it is not running under a call ledger.
PER_ITEM_SECONDS = FFMPEG_REQUEST_BUDGET_SECONDS

# Fallbacks for media whose Telethon-reported extension is empty; ``mimetypes``
# does not know Telegram's own sticker types.
_MIME_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/x-tgsticker": ".tgs",
}

# Per document, not per call: one call resolves up to MAX_CUSTOM_EMOJI_IDS of them.
DEFAULT_EMOJI_BYTES = 5 * 1024 * 1024


def _media_suffix(details: dict) -> str:
    """File suffix (with dot) for in-memory media bytes, for the frame extractor."""
    extension = details.get("extension")
    if extension:
        return extension if extension.startswith(".") else f".{extension}"
    return _MIME_SUFFIXES.get((details.get("mime_type") or "").lower(), ".bin")


def _encode_one(
    raw: bytes,
    max_dimension: int,
    deadline: Optional[float] = None,
    allowance: Optional[int] = None,
    cancelled: Optional[threading.Event] = None,
) -> tuple:
    """One still image as ``([metadata], [Image])``. Blocking: call in a thread.

    The decode itself is a child process now (``extract_still``). It used to be
    ``open_image_bytes`` + ``encode_image`` right here on the worker thread, with
    no deadline and no way to terminate it - so a Pillow call that did not return
    held the thread for the life of the server.
    """
    png, meta = extract_still(
        raw,
        cancelled=cancelled,
        deadline=deadline if deadline is not None else time.monotonic() + PER_ITEM_SECONDS,
        # Clamped here rather than after the decode. The decoder emits at this
        # size, so an unclamped value would come back larger than anything this
        # server is allowed to return and there is no second pass to shrink it.
        max_side=bounded_dimension(max_dimension),
        allowance=allowance,
    )
    return [meta], [Image(data=png, format="png")]


def _encode_frames(
    raw: bytes,
    suffix: str,
    count: int,
    max_dimension: int,
    deadline: Optional[float] = None,
    allowance: Optional[int] = None,
    cancelled: Optional[threading.Event] = None,
) -> tuple:
    """Frames of an animation as ``([metadata], [Image])``. Blocking: call in a thread.

    One clock covers decoding AND this re-encode, and ``deadline`` now arrives
    from the call rather than being started here: a decode that spends the whole
    budget must not then hand back ten frames to resize on a fresh one, a caller
    who stopped waiting should not be resized for either, and ten documents must
    not each get their own copy of the request budget.

    The frames come back already encoded at ``max_dimension`` from the decoder's
    own child process, so nothing is re-decoded here.
    """
    if deadline is None:
        deadline = time.monotonic() + FFMPEG_REQUEST_BUDGET_SECONDS
    metas, images = [], []
    for png, meta in extract_frames(
        raw,
        suffix,
        count,
        cancelled,
        deadline=deadline,
        max_side=bounded_dimension(max_dimension),
        allowance=allowance,
    ):
        if cancelled is not None and cancelled.is_set():
            raise DecodingCancelled(
                f"Encoding was cancelled after {len(images)} of {count} frames."
            )
        if time.monotonic() > deadline:
            raise FrameExtractionError(
                f"Preview passed its budget while collecting frame {len(images) + 1}. "
                "Ask for fewer frames, or a smaller max_dimension."
            )
        metas.append(meta)
        images.append(Image(data=png, format="png"))
    return metas, images


# What ONE call may hand back in decoded previews, however many documents it
# covers. The existing batch gate bounds concurrency by SOURCE bytes, which is a
# different quantity: a 300 KB .tgs becomes ten 512x512 RGBA frames, and a small
# video becomes ten PNGs at the emitted ceiling. Ten documents each inside the
# per-request frame budget could therefore hold ten times it, so the per-request
# ceiling was not a ceiling on the call.
MAX_CALL_PREVIEW_BYTES = 128 * 1024 * 1024

# And the same for time. Every decode is bounded, and every document used to get
# its own FFMPEG_REQUEST_BUDGET_SECONDS, so a ten-document call could spend ten
# of them - each stage inside its bound and the call inside none. This is the
# clock the whole call shares; three minutes covers ten documents comfortably
# while still ending a call that has stopped being worth waiting for.
MAX_CALL_PREVIEW_SECONDS = 180.0


class Reservation:
    """One document's share of the call, taken before its decode starts.

    ``allowance`` is handed down into the decoder as its own byte ceiling, which
    is what makes the reservation binding rather than advisory: the work cannot
    produce more than was set aside for it, so the sum over a batch is the pool.
    """

    __slots__ = ("allowance", "_ledger", "_settled")

    def __init__(self, ledger: "PreviewLedger", allowance: int) -> None:
        self.allowance = allowance
        self._ledger = ledger
        self._settled = False

    def settle(self, produced: int) -> None:
        """Book what was really produced and return the rest of the share."""
        if self._settled:
            return
        self._settled = True
        self._ledger._release(self.allowance, produced)
        if produced > self.allowance:
            raise FrameExtractionError(
                f"This document produced {produced} bytes of preview, above the "
                f"{self.allowance}-byte share of this call's budget. Ask for fewer items, "
                "fewer frames, or a smaller max_dimension."
            )


class PreviewLedger:
    """A decoded-output allowance shared by everything in one call.

    Charged AFTER each document, which is what made it not a budget. The tool runs
    its documents through ``asyncio.gather``, so with a batch every document
    produced its bytes before any of them was accounted for, and the ceiling only
    described what had already been allocated. Reproduced against that version:
    two 8-byte outputs against a 10-byte ceiling ended at 16.

    So a share is taken up front and the decoder is given it as its own ceiling.
    ``shares`` is how many documents the call covers: with one document the pool
    is the pool, and with ten each gets a tenth, which is the honest answer when
    all ten start before any finishes. What a document does not spend goes back,
    so anything that starts later can still use it.

    Still not a lock or a queue - the batch already limits how many downloads run
    at once, and this only has to bound the TOTAL handed back. The arithmetic runs
    on the event loop thread, where there is no interleaving to protect against.

    **What this budget is, exactly.** It counts the ENCODED preview bytes one call
    hands back, and nothing else. Every other cost in the same path has its own
    explicit ceiling, because one number covering all of them would have to be the
    largest and would therefore bound none of them:

    * decoder subprocess stdout - the smaller of ``MAX_DECODER_OUTPUT_BYTES`` and
      whatever THIS call's reservation still has, passed down per run;
    * decoder stderr - ``MAX_DECODER_STDERR_BYTES``, truncated at the boundary;
    * a probe's reply - ``MAX_PROBE_OUTPUT_BYTES``, which is metadata, not a decode;
    * frames held inside one decode - ``MAX_TOTAL_FRAME_BYTES`` via ``_Budget``;
    * the window capture reply - ``MAX_CAPTURE_RESPONSE_BYTES``, checked BEFORE each
      frame is appended rather than after;
    * the source transfer - ``MAX_FRAME_SOURCE_BYTES``, enforced mid-download.

    Two costs are deliberately outside all of these and are stated rather than
    silently absorbed. Pillow's decoded surface is bounded by pixels, not bytes:
    ``open_image_bytes`` refuses a declared pixel count above
    ``MAX_DECODED_PIXELS`` before anything is allocated. And the MCP reply carries
    images base64-encoded, so what leaves this process is about four thirds of
    what is counted here - the ratio is fixed and known, which is why it is
    applied to the ceiling by whoever sets it rather than tracked per byte.
    """

    __slots__ = ("deadline", "reserved", "shares", "spent", "total")

    def __init__(
        self,
        shares: int = 1,
        total: int = MAX_CALL_PREVIEW_BYTES,
        deadline: Optional[float] = None,
    ) -> None:
        self.total = total
        self.shares = max(1, int(shares))
        self.spent = 0
        self.reserved = 0
        # One clock for the whole call, not one per document. Ten documents each
        # inside their own FFMPEG_REQUEST_BUDGET_SECONDS is ten times the budget,
        # and nothing upstream sees it.
        self.deadline = (
            deadline if deadline is not None else time.monotonic() + MAX_CALL_PREVIEW_SECONDS
        )

    @property
    def available(self) -> int:
        return max(0, self.total - self.spent - self.reserved)

    def reserve(self) -> Reservation:
        """Set aside this document's share before any work begins."""
        allowance = min(max(1, self.total // self.shares), self.available)
        if allowance <= 0:
            raise FrameExtractionError(
                f"This call has already committed its {self.total}-byte preview budget. "
                "Ask for fewer items, fewer frames, or a smaller max_dimension."
            )
        self.reserved += allowance
        return Reservation(self, allowance)

    def check_deadline(self) -> None:
        """Refuse before starting work the call no longer has time for."""
        if time.monotonic() > self.deadline:
            raise FrameExtractionError(
                f"This call passed its {MAX_CALL_PREVIEW_SECONDS}s preview budget before this "
                "item started. Ask for fewer items, fewer frames, or a smaller max_dimension."
            )

    def _release(self, allowance: int, produced: int) -> None:
        self.reserved = max(0, self.reserved - allowance)
        self.spent += min(produced, allowance)


async def encode_frames_cancellable(
    raw: bytes,
    suffix: str,
    count: int,
    max_dimension: int,
    ledger: Optional["PreviewLedger"] = None,
) -> tuple:
    """Extract and encode frames off the event loop, and let cancellation reach them.

    ``asyncio.to_thread`` alone is not enough. Cancelling the awaiting coroutine
    raises in the caller and frees it, but the worker thread keeps running: Python
    cannot stop a thread from outside, and a ``concurrent.futures`` job that has
    already started cannot be cancelled either. So the decoders kept going - and
    ffmpeg kept burning CPU - until their own timeouts fired, long after anyone was
    left to read the answer.

    The event is how the thread gets told, and the drain below is how the caller
    learns it was heard. Setting the flag and re-raising immediately - which is what
    this did - reports a cancellation that has not happened yet: the decoder is
    still mid-frame and its ffmpeg child is still running, so 'cancelled' meant
    'asked to stop', and anything that then counted processes or cleaned a
    directory raced a worker that was still using it.

    The wait is bounded and best-effort on purpose. A drain that could block
    forever would hand a wedged decoder the power to hold up the canceller too,
    which is the failure being cancelled in the first place.

    Every caller goes through here rather than reaching for ``asyncio.to_thread``
    itself, so this exists once instead of at six call sites each free to forget it.
    """
    return await _off_loop(_encode_frames, (raw, suffix, count, max_dimension), ledger)


async def encode_still_cancellable(
    raw: bytes,
    max_dimension: int,
    ledger: Optional["PreviewLedger"] = None,
) -> tuple:
    """Decode and encode ONE still off the event loop, cancellably and on budget.

    The still path used to be a bare ``asyncio.to_thread(_encode_one, ...)`` at
    four call sites, which is the same thread with none of the protections: no
    deadline, no cancellation reaching the decoder, and no share of the call's
    byte budget. Routing it through the same helper as the animated path is what
    stops that being four places free to forget it.
    """
    return await _off_loop(_encode_one, (raw, max_dimension), ledger)


async def _off_loop(work, arguments: tuple, ledger: Optional["PreviewLedger"]) -> tuple:
    """Run one blocking decode off the event loop, under the call's budget.

    The cancellation mechanics are shared with the window capture and live in
    :func:`telegram_mcp.visual.bounded_process.run_cancellable`; what belongs here
    is the budget. The share is taken BEFORE the work starts, because charging
    afterwards is what let a parallel batch produce everything first and account
    for it second - reproduced against that version, two 8-byte outputs against a
    10-byte ceiling ended at 16.

    Settled on every exit, including the ones that produced nothing: a
    reservation that is never released is a slow leak of the call's budget.
    """
    reservation = None
    if ledger is not None:
        ledger.check_deadline()
        reservation = ledger.reserve()

    try:
        metas, images = await run_cancellable(
            work,
            *arguments,
            ledger.deadline if ledger is not None else None,
            reservation.allowance if reservation is not None else None,
        )
    except BaseException:
        if reservation is not None:
            reservation.settle(0)
        raise
    if reservation is not None:
        reservation.settle(sum(len(image.data) for image in images))
    return metas, images


async def _premium_effect_frames(
    cl, msg, details: dict, count: int, max_dimension: int, max_bytes: int, refresh=None
):
    """Frames of a premium sticker's separate effect animation.

    Telegram ships the effect as a ``VideoSize`` of type ``"f"`` alongside the
    sticker, and composites it over the sticker in the chat. Sampling the asset
    shows what the effect *is*; it is emphatically not what the reader sees, so
    every record says so rather than letting a caller assume otherwise.
    """
    if not details.get("premium_effect"):
        return (
            "This message has no premium sticker effect. get_media_details reports one under "
            "'premium_effect' when it exists; drop premium_effect=True to sample the sticker."
        )

    document = getattr(msg, "document", None) or getattr(msg, "sticker", None)
    effect = next(
        (
            v
            for v in getattr(document, "video_thumbs", None) or []
            if getattr(v, "type", None) == "f"
        ),
        None,
    )
    if effect is None:
        return "The premium effect was reported but its asset is missing from this document."

    # The effect asset carries its own size; the sticker's size says nothing about
    # it. The advertised figure is a free early refusal, not the limit that counts:
    # it can be absent or wrong, so the transfer itself is bounded below.
    limit = min(max_bytes, MAX_FRAME_SOURCE_BYTES)
    advertised = getattr(effect, "size", None)
    if advertised is not None and advertised > limit:
        return (
            f"The premium effect asset is {advertised} bytes, above the {limit}-byte limit "
            f"(hard ceiling {MAX_FRAME_SOURCE_BYTES}). Raise max_bytes up to that ceiling."
        )

    async def _fetch_effect(fresh_msg):
        # Same asymmetry the caller's non-premium branch never had: within one
        # tool, premium_effect=False recovered from a stale file reference and
        # premium_effect=True did not.
        target = document
        if fresh_msg is not None:
            target = getattr(fresh_msg, "document", None) or getattr(fresh_msg, "sticker", None)
        return await _download_thumb_capped(cl, target or document, effect, limit)

    if refresh is None:
        raw, over_cap = await _fetch_effect(None)
    else:
        raw, over_cap = await with_reference_retry(_fetch_effect, refresh)
    if over_cap:
        return (
            f"The premium effect asset is larger than the {limit}-byte limit. The transfer was "
            f"aborted once it crossed that, so the rest was never fetched — its advertised size "
            f"was {'absent' if advertised is None else f'{advertised} bytes, which was wrong'}. "
            f"Raise max_bytes up to the {MAX_FRAME_SOURCE_BYTES}-byte ceiling."
        )
    if not raw:
        return "Telegram returned no data for the premium effect asset."

    # Verified against live Telegram data: the type="f" asset is a gzipped Lottie
    # (.tgs), the same format as an animated sticker — not a WebM video, which is
    # what this used to assume. The sniff lives in effect_catalog because the same
    # decision existed here and in tools/effects.py with two slightly different
    # rules, each guarded by only one of the two suites.
    suffix, asset_format = sniff_asset_format(raw)
    records, images = await encode_frames_cancellable(raw, suffix, count, max_dimension)
    for record in records:
        record["source_asset"] = "premium_effect"
        record["composite_fidelity"] = "asset-only"
        record["asset_format"] = asset_format
    return [
        format_tool_result(
            records,
            {
                "message_id": msg.id,
                "media_kind": details.get("kind"),
                "source_bytes": len(raw),
                "note": (
                    "These are frames of the premium effect asset ON ITS OWN. Telegram composites "
                    "this animation over the sticker in the chat, so the finished appearance is "
                    "neither these frames nor the sticker alone. Use get_telegram_frames while the "
                    "effect plays for the real composite."
                ),
            },
        ),
        *images,
    ]


async def _custom_emoji_preview(
    cl,
    document,
    count: int,
    max_dimension: int,
    max_bytes: int = DEFAULT_EMOJI_BYTES,
    ledger: Optional["PreviewLedger"] = None,
) -> tuple:
    """Metadata and preview image(s) for one custom emoji document."""
    mime = (getattr(document, "mime_type", None) or "").lower()
    record: Dict[str, Any] = {
        "document_id": document.id,
        "mime_type": mime or None,
        "size_bytes": getattr(document, "size", None),
    }
    for attribute in getattr(document, "attributes", None) or []:
        alt = getattr(attribute, "alt", None)
        if alt:
            record["placeholder"] = display_name(alt)
        sticker_set = getattr(attribute, "stickerset", None)
        short_name = getattr(sticker_set, "short_name", None)
        set_id = getattr(sticker_set, "id", None)
        if short_name:
            record["sticker_set"] = short_name
        elif set_id is not None:
            # Custom emoji reference their set by InputStickerSetID; the short
            # name costs a separate GetStickerSet call per set, so report the ID.
            record["sticker_set_id"] = set_id
        if getattr(attribute, "w", None):
            record["width"], record["height"] = attribute.w, attribute.h
        # DocumentAttributeCustomEmoji only.
        if getattr(attribute, "free", False):
            record["free"] = True  # usable without a Premium subscription
        if getattr(attribute, "text_color", False):
            # Telegram recolours this emoji to match the surrounding text, so its
            # real appearance depends on where it is shown.
            record["text_color"] = True

    if not mime:
        # DocumentEmpty: Telegram accepted the ID but knows no such emoji.
        record["preview_error"] = (
            "Telegram has no custom emoji with this document ID (it returned an empty "
            "document). Check the ID against the 'custom_emoji' block of inspect_message."
        )
        return record, []

    is_lottie = mime == "application/x-tgsticker"
    render_lottie = is_lottie and lottie_available()
    if is_lottie:
        record["animation_format"] = "lottie_tgs"
        record["animation_note"] = (
            "Vector (Lottie) animation rendered with rlottie: the images below are real frames "
            "of the animation."
            if render_lottie
            else "Vector (Lottie) animation: the image below is Telegram's static thumbnail, not "
            "the animation. Install the renderer with pip install 'telegram-mcp[lottie]', or "
            "play it in Telegram Desktop and call get_telegram_frames."
        )

    if record.get("text_color"):
        # An adaptive emoji has no colour of its own: Telegram paints it in the
        # colour of the text around it, which this renderer cannot know. Saying the
        # preview is exact would be a lie, so say precisely what it is instead.
        record["color_fidelity"] = "context-neutral"
        record["color_note"] = (
            "This emoji is context-coloured (text_color): Telegram recolours it to match the "
            "surrounding text, and that colour is not part of the document. The preview below "
            "shows the shape and motion in the renderer's own default colour, NOT the colour a "
            "reader sees. For the exact appearance, capture it in place with get_telegram_frames."
        )

    # A free early refusal, so one oversized emoji costs nothing and the other nine
    # in the batch still resolve. It is not the limit that counts: the advertised
    # size can be absent or wrong, so the transfer itself is bounded below.
    advertised = record["size_bytes"]
    if advertised is not None and advertised > max_bytes:
        record["preview_error"] = (
            f"This emoji document is {advertised} bytes, above the {max_bytes}-byte "
            f"per-document limit (hard ceiling {MAX_FRAME_SOURCE_BYTES}). Raise max_bytes up "
            "to that ceiling."
        )
        return record, []

    try:
        # Only the un-renderable Lottie path settles for the thumbnail.
        thumb_only = is_lottie and not render_lottie
        if thumb_only:
            selection = _select_thumb(_declared_sizes(document), -1, max_bytes)
            if isinstance(selection, str):
                record["preview_error"] = selection
                return record, []
            _, size = selection

            async def _fetch(fresh):
                return await _download_size_capped(cl, fresh or document, size, max_bytes)

        else:

            async def _fetch(fresh):
                return await _download_whole_capped(cl, fresh or document, max_bytes)

        async def _refetch_emoji():
            # A custom emoji document is produced by exactly one call, so that is
            # where a fresh file reference comes from.
            refreshed = await cl(
                functions.messages.GetCustomEmojiDocumentsRequest(document_id=[document.id])
            )
            return next((d for d in refreshed or [] if d.id == document.id), None)

        raw, over_cap = await with_reference_retry(_fetch, _refetch_emoji)
        if over_cap:
            claim = "absent" if advertised is None else f"{advertised} bytes, which was wrong"
            record["preview_error"] = (
                f"This emoji document is larger than the {max_bytes}-byte per-document limit. "
                "The transfer was aborted once it crossed that, so the rest was never fetched "
                f"— its advertised size was {claim}. Raise max_bytes up to the "
                f"{MAX_FRAME_SOURCE_BYTES}-byte ceiling."
            )
            return record, []
        if not raw:
            record["preview_error"] = "Telegram returned no preview data for this document."
            return record, []
        record["source_bytes"] = len(raw)
        if render_lottie or mime.startswith("video/"):
            suffix = ".tgs" if render_lottie else _MIME_SUFFIXES.get(mime, ".webm")
            record["preview"], images = await encode_frames_cancellable(
                raw, suffix, count, max_dimension, ledger
            )
        else:
            # A still costs the call too - ten large stickers add up the same way,
            # and its share is reserved before the decode starts.
            record["preview"], images = await encode_still_cancellable(raw, max_dimension, ledger)
        record["preview_source"] = (
            "rlottie" if render_lottie else "thumbnail" if thumb_only else "document"
        )
    except (FrameExtractionError, ImageError) as error:
        # One unrenderable emoji must not sink the other nine in the batch.
        record["preview_error"] = str(error)
        return record, []
    return record, images
