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
from telegram_mcp.visual.frames import FrameExtractionError, extract_frames, lottie_available
from telegram_mcp.visual.images import ImageError, encode_image, open_image_bytes

from mcp.server.fastmcp import Image

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


def _encode_one(raw: bytes, max_dimension: int) -> tuple:
    """One still image as ``([metadata], [Image])``. Blocking: call in a thread."""
    png, meta = encode_image(open_image_bytes(raw), max_dimension=max_dimension)
    return [meta], [Image(data=png, format="png")]


def _encode_frames(
    raw: bytes,
    suffix: str,
    count: int,
    max_dimension: int,
    cancelled: Optional[threading.Event] = None,
) -> tuple:
    """Frames of an animation as ``([metadata], [Image])``. Blocking: call in a thread."""
    metas, images = [], []
    for png, meta in extract_frames(raw, suffix, count, cancelled):
        encoded, encoded_meta = encode_image(open_image_bytes(png), max_dimension=max_dimension)
        metas.append({**meta, **encoded_meta})
        images.append(Image(data=encoded, format="png"))
    return metas, images


async def encode_frames_cancellable(
    raw: bytes, suffix: str, count: int, max_dimension: int
) -> tuple:
    """Extract and encode frames off the event loop, and let cancellation reach them.

    ``asyncio.to_thread`` alone is not enough. Cancelling the awaiting coroutine
    raises in the caller and frees it, but the worker thread keeps running: Python
    cannot stop a thread from outside, and a ``concurrent.futures`` job that has
    already started cannot be cancelled either. So the decoders kept going - and
    ffmpeg kept burning CPU - until their own timeouts fired, long after anyone was
    left to read the answer.

    The event is how the thread gets told. Every caller goes through here rather
    than reaching for ``asyncio.to_thread`` itself, so this exists once instead of
    at six call sites each free to forget it.
    """
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(
            _encode_frames, raw, suffix, count, max_dimension, cancelled
        )
    except asyncio.CancelledError:
        cancelled.set()
        raise


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
    cl, document, count: int, max_dimension: int, max_bytes: int = DEFAULT_EMOJI_BYTES
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
                raw, suffix, count, max_dimension
            )
        else:
            record["preview"], images = await asyncio.to_thread(_encode_one, raw, max_dimension)
        record["preview_source"] = (
            "rlottie" if render_lottie else "thumbnail" if thumb_only else "document"
        )
    except (FrameExtractionError, ImageError) as error:
        # One unrenderable emoji must not sink the other nine in the batch.
        record["preview_error"] = str(error)
        return record, []
    return record, images
