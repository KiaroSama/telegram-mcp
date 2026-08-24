"""Getting bytes out of Telegram, bounded, and choosing which bytes to get.

Two rules shape everything here. **The transfer is the limit, not the result**:
Telegram does not always advertise a size, so a cap checked after the fact is no
cap at all — every path streams and aborts at the ceiling. And **a thumbnail
request must stay a thumbnail request**: ``thumb=-1`` fetches a photo's largest
size, which for a photo is the original, so the size is selected explicitly
against a byte budget rather than by index.

A file reference authorises exactly one download and expires on Telegram's
schedule; :func:`with_reference_retry` refreshes from whatever produced it and
retries once.

Separate from ``tools/`` because both tool modules need it, and a tool module
importing another tool module's privates is how that dependency used to be
spelled.
"""

import os
from pathlib import Path
from typing import Optional

from telegram_mcp.runtime import logger

from telethon import utils
from telethon.errors import (
    FileReferenceEmptyError,
    FileReferenceExpiredError,
    FileReferenceInvalidError,
)
from telethon.tl.types import InputDocumentFileLocation, InputPhotoFileLocation

# A PhotoPathSize is an SVG outline of an animated sticker, not a picture, and
# Pillow cannot decode one; Telethon drops it for the same reason. A
# PhotoSizeEmpty carries nothing at all.
_UNRENDERABLE_SIZES = ("PhotoPathSize", "PhotoSizeEmpty")

# The frame extractor takes bytes, so the source media reaches memory in full
# before a single frame comes out; max_bytes is clamped to this no matter what
# the caller asks for, and the transfer is aborted once it is exceeded.
MAX_FRAME_SOURCE_BYTES = 200 * 1024 * 1024

# A batch tool downloads its items concurrently, so the peak held in memory is
# `width * max_bytes`, not one buffer. This is the ceiling on that product.
#
# 512 MiB, not the 2 GiB this used to be. The old figure was a ceiling nothing
# legitimate approached and every hostile batch could: ten custom emoji at the
# 200 MiB per-asset limit fitted inside it, so one request could hold 2 GiB of
# raw bytes before a decoder had allocated anything. The server also runs
# alongside whatever else the machine is doing, and a worker that survives its
# own peak but starves its neighbour has not been bounded, only moved.
MAX_BATCH_BYTES = 512 * 1024 * 1024


def batch_width(item_count: int, max_bytes: int) -> int:
    """How many items may be in flight at once without crossing MAX_BATCH_BYTES.

    Concurrency bought real latency, but it multiplied the peak by the batch
    size. Deriving the width from the budget keeps that peak bounded no matter
    what the caller asks for, and keeps it bounded if either constant grows
    later — which is the part a fixed number would silently get wrong.
    """
    return max(1, min(item_count, MAX_BATCH_BYTES // max(1, max_bytes)))


async def _stream_capped(cl, target, max_bytes: int) -> tuple:
    """Stream anything ``iter_download`` accepts, aborting past ``max_bytes``.

    Returns ``(data, over_cap)``. Telegram does not always advertise a size, and
    without a cap on the transfer itself an unannounced 2 GB file is pulled in
    full before anyone can object. This stops at the first chunk over the limit.
    The bytes still land in memory because the frame extractor takes bytes — but
    never more than ``max_bytes`` plus the chunk that crossed it (Telethon's
    chunks are at most 1 MiB), and that buffer is then dropped.
    """
    buffer = bytearray()
    chunks = cl.iter_download(target)
    try:
        async for chunk in chunks:
            buffer += chunk
            if len(buffer) > max_bytes:
                return None, True
    finally:
        # Breaking out early leaves the borrowed DC sender open; RequestIter is
        # not a generator, so nothing else releases it.
        close = getattr(chunks, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as error:
                # close() reads self._sender, which RequestIter only defines once
                # _init has run, and _init runs lazily inside the async-for. A
                # sender that could not be borrowed for a foreign DC therefore
                # raises AttributeError from in here and replaces the failure that
                # actually matters — including the stale-reference error that
                # effects.py retries on.
                logger.debug("iter_download close() failed: %s: %s", type(error).__name__, error)
    return bytes(buffer), False


async def _download_capped(cl, msg, max_bytes: int) -> tuple:
    """A message's media, with the transfer itself bounded by ``max_bytes``."""
    # download_media() unwraps a link preview to the photo/document inside it;
    # iter_download() does not, and get_input_location cannot cast a
    # MessageMediaWebPage. Message.document/.photo already do that unwrapping,
    # and for ordinary media they resolve to the identical InputFileLocation.
    media = getattr(msg, "document", None) or getattr(msg, "photo", None) or msg.media
    return await _stream_capped(cl, media, max_bytes)


async def _download_thumb_capped(cl, document, video_size, max_bytes: int) -> tuple:
    """One of a document's thumbnails, with the transfer bounded.

    ``iter_download`` takes no ``thumb`` argument, which used to be read here as
    "a VideoSize cannot be streamed" — so the effect asset was pulled whole with
    ``download_media`` and only measured afterwards. It can be streamed: a thumb
    download is an ordinary file location carrying a ``thumb_size``, which is
    exactly what ``download_media`` builds internally before handing it to the
    same downloader. Building it here bounds the transfer instead of the result.
    """
    location = InputDocumentFileLocation(
        id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
        thumb_size=getattr(video_size, "type", ""),
    )
    return await _stream_capped(cl, location, max_bytes)


async def _download_whole_capped(cl, target, max_bytes: int) -> tuple:
    """A whole file, with the transfer bounded."""
    return await _stream_capped(cl, target, max_bytes)


# A file reference authorises one download and Telegram expires it on its own
# schedule. The ids stay valid; only the reference does not, and the cure is
# always to fetch the object again from whatever produced it. It must be told
# apart from ordinary RPC trouble, where refetching would fix nothing and a blind
# retry would hide a real failure.
_STALE_REFERENCE = (
    FileReferenceEmptyError,
    FileReferenceExpiredError,
    FileReferenceInvalidError,
)


async def with_reference_retry(download, refresh):
    """Run ``download``; on a stale file reference, ``refresh`` once and retry.

    ``download`` takes the object to download and returns ``(data, over_cap)``;
    ``refresh`` returns a freshly fetched replacement for that object, or ``None``
    when the source can no longer produce one. Exactly one retry — a second
    failure is a real one, and a loop here would be indistinguishable from a hang.

    This lived only in ``tools/effects.py``. Message media and custom emoji have
    the same expiry and had no recovery at all: the transfer simply failed, with
    an error naming a cause the caller could do nothing about.
    """
    try:
        return await download(None)
    except _STALE_REFERENCE:
        fresh = await refresh()
        if fresh is None:
            raise
        return await download(fresh)


def _thumb_owner(msg):
    """The object whose declared sizes ``get_media_details`` enumerated.

    Mirrors describe_media. Message.document/.sticker/.photo also unwrap a link
    preview, which get_input_location cannot cast.
    """
    return (
        getattr(msg, "document", None)
        or getattr(msg, "sticker", None)
        or getattr(msg, "photo", None)
    )


def _declared_sizes(owner) -> list:
    """The size list ``_describe_thumbnails`` indexed, in the same order."""
    return list(getattr(owner, "thumbs", None) or getattr(owner, "sizes", None) or [])


def _size_bytes(owner_size) -> Optional[int]:
    """A declared size's byte count, as the caller will actually receive it."""
    passes = getattr(owner_size, "sizes", None)  # PhotoSizeProgressive: one per pass
    if passes:
        return max(passes)
    inline = getattr(owner_size, "bytes", None)  # PhotoStrippedSize / PhotoCachedSize
    if inline is not None:
        if type(owner_size).__name__ == "PhotoStrippedSize":
            # Telegram strips the JPEG header and footer from this one and every
            # client re-attaches them, so 61 bytes on the wire materialise as 683.
            # Reporting the wire figure made the byte budget compare against a
            # number nobody ever receives, and produced a diagnostic blaming
            # Telegram for a transfer that never happened.
            try:
                return len(utils.stripped_photo_to_jpg(bytes(inline)))
            except Exception:
                # A fake or a malformed payload: the wire length is still a fact.
                return len(inline)
        return len(inline)
    return getattr(owner_size, "size", None)


def _select_thumb(sizes: list, thumb_index: int, max_bytes: int):
    """``(index, size)`` for a thumbnail request, or a string explaining why not.

    Telethon sorts the list before indexing it and folds photo.video_sizes in, so
    handing it a bare negative index selects a photo's full-resolution original —
    or the mp4 of an animated photo — and index N names a different size than the
    N that get_media_details printed. Resolving the object here and handing
    _get_thumb a size object, which it accepts directly, is what makes both
    promises true. Compared by class name, not isinstance, so the tests' fakes
    stay usable.
    """
    candidates = [
        (index, size)
        for index, size in enumerate(sizes)
        if type(size).__name__ not in _UNRENDERABLE_SIZES
    ]
    if not candidates:
        return "This media has no renderable server-side thumbnail size."
    available = [index for index, _ in candidates]

    if thumb_index >= 0:
        if thumb_index >= len(sizes):
            return f"thumb_index {thumb_index} is out of range. Available thumbnails: {available}."
        chosen = sizes[thumb_index]
        if type(chosen).__name__ in _UNRENDERABLE_SIZES:
            return (
                f"thumb_index {thumb_index} is a {type(chosen).__name__}, which carries no "
                f"picture. Available thumbnails: {available}."
            )
        return thumb_index, chosen

    measured = [(index, size, _size_bytes(size)) for index, size in candidates]
    fitting = [item for item in measured if item[2] is not None and item[2] <= max_bytes]
    if fitting:
        index, size, _ = max(fitting, key=lambda item: item[2])
        return index, size

    known = [count for _, _, count in measured if count is not None]
    if not known:
        return (
            "None of this media's server-side thumbnail sizes advertises a byte count, so "
            "none can be chosen against a byte budget. Pass an explicit thumb_index from "
            "get_media_details — the transfer is capped either way."
        )
    return (
        f"The smallest server-side thumbnail is {min(known)} bytes, above the {max_bytes}-byte "
        f"limit for this request (hard ceiling {MAX_FRAME_SOURCE_BYTES}). Raise max_bytes up to "
        "that ceiling, use get_media_frames to render an animation, or download_media to save "
        "the original to disk."
    )


async def _download_size_capped(cl, owner, size, max_bytes: int) -> tuple:
    """One declared size of a photo or a document, with the transfer bounded.

    PhotoStrippedSize and PhotoCachedSize carry their pixels inside the message
    Telegram already sent, and Telethon returns them without a single request.
    Building a location for those would turn a free inline thumb into an RPC, so
    they keep going through download_media — with the size OBJECT, which
    _get_thumb accepts directly, never an index.
    """
    if getattr(size, "bytes", None) is not None:
        data = await cl.download_media(owner, file=bytes, thumb=size)
        return data, bool(data) and len(data) > max_bytes

    # A Document carries `thumbs` and never `sizes`, so this tells the two apart
    # without isinstance and keeps the tests' SimpleNamespace fakes working.
    location_type = (
        InputPhotoFileLocation
        if getattr(owner, "sizes", None) is not None
        else InputDocumentFileLocation
    )
    location = location_type(
        id=owner.id,
        access_hash=owner.access_hash,
        file_reference=owner.file_reference,
        thumb_size=getattr(size, "type", ""),
    )
    return await _stream_capped(cl, location, max_bytes)


# Two tools now write a caller-named file to disk, and neither may overwrite one
# that is already there. Their default names are only second-precise, so two
# saves in the same second collide on their own without anyone being hostile.
NAME_ATTEMPTS = 100


def reserve_free_path(target: Path) -> Optional[Path]:
    """Create and return an unused path near ``target``, or None if there is none.

    O_EXCL is the reservation: it fails if the name appeared between the check and
    the create, so the caller owns the name it gets back rather than merely having
    seen it free a moment ago.

    Lives here rather than in either tool module because both need it, and a tool
    module importing another tool module's privates is the dependency this module
    exists to break.
    """
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for attempt in range(NAME_ATTEMPTS):
        candidate = parent / (f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt}{suffix}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return candidate
        except FileExistsError:
            continue
    return None
