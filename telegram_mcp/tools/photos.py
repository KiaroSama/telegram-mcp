"""Peer photo MCP tools: index them, open one, or see them all at once."""

from mcp.server.mcpserver import Image

from telegram_mcp.contact_sheet import ContactSheetError, MAXIMUM_TILES, compose_contact_sheet
from telegram_mcp.media_preview import _encode_one
from telegram_mcp.media_transfer import MAX_FRAME_SOURCE_BYTES, batch_width
from telegram_mcp.paging import LIMITS, bounded
from telegram_mcp.photo_source import (
    AVATAR_SOURCE,
    PHOTO_SOURCES,
    UnknownPhotoSource,
    download_photo_bytes,
    find_photo_reference,
    list_photo_references,
    peer_supports_source,
)
from telegram_mcp.runtime import *

# One photo, unless the caller says otherwise. Generous enough for an avatar at
# full resolution and far below the transfer ceiling, because a peer declares the
# size and a caller naming a photo id is choosing what to fetch.
DEFAULT_PHOTO_BYTES = 20 * 1024 * 1024

# Tiles for a sheet are downloaded as thumbnails, so the per-tile cost is small and
# the ceiling that matters is the number of them.
SHEET_TILE_BYTES = 4 * 1024 * 1024


def _cap(max_bytes) -> int:
    """The effective byte ceiling, never above the transfer limit."""
    if not max_bytes:
        return DEFAULT_PHOTO_BYTES
    return max(1, min(int(max_bytes), MAX_FRAME_SOURCE_BYTES))


@mcp.tool(annotations=ToolAnnotations(title="List Photos", openWorldHint=True, readOnlyHint=True))
@with_account(readonly=True)
@validate_id("chat_id")
async def list_photos(
    chat_id: Union[int, str],
    source: str = AVATAR_SOURCE,
    limit: int = 20,
    account: str = None,
) -> str:
    """
    Index a peer's photos, so one of them can then be opened by id.

    Telegram has no single photo list, and the difference is visible here:

    - `avatars` is the profile-picture history. For a user it comes from Telegram's
      own call and is ordered newest first as *profile* order, which is not the
      same as chronological. For a group or channel there is no such call, so the
      history is rebuilt from the service messages that recorded each change.
    - `messages` is photos SENT in the conversation, keyed by MESSAGE id.

    The id in each record is what `open_photo` and `get_photo_sheet` take back.

    Args:
        chat_id: The chat, channel or user.
        source: `avatars` (default) or `messages`.
        limit: How many to index (1-100; a larger value is served as 100).
    """
    try:
        bound = bounded(limit, LIMITS["list_photos"])
        if bound.error:
            return bound.error
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        references = await list_photo_references(cl, entity, source, bound.value)
        if not references and not peer_supports_source(entity, source):
            return (
                f"This peer has no '{source}' photos to index. Sources: "
                f"{', '.join(PHOTO_SOURCES)}."
            )
        return format_tool_result(
            {
                "source": source,
                "requested_limit": limit,
                "effective_limit": bound.value,
                "count": len(references),
                "photos": [reference.describe() for reference in references],
            }
        )
    except UnknownPhotoSource as error:
        return str(error)
    except Exception as e:
        return log_and_format_error("list_photos", e)


@mcp.tool(annotations=ToolAnnotations(title="Open Photo", openWorldHint=True, readOnlyHint=True))
@with_account(readonly=True)
@validate_id("chat_id")
async def open_photo(
    chat_id: Union[int, str],
    photo_id: Optional[int] = None,
    source: str = AVATAR_SOURCE,
    max_dimension: int = 1024,
    max_bytes: int = None,
    search_depth: int = 50,
    account: str = None,
) -> list:
    """
    Open one of a peer's photos as an image.

    With no `photo_id` this returns the current avatar. With one, it returns that
    photo from the same source `list_photos` indexed it under - an id from one
    source does not name anything in the other.

    The transfer is capped: a photo that outgrows `max_bytes` is abandoned in
    flight rather than fetched and then measured.

    Args:
        chat_id: The chat, channel or user.
        photo_id: The id from `list_photos`; omitted means the current avatar.
        source: `avatars` (default) or `messages`; must match how the id was listed.
        max_dimension: Longest edge of the returned image.
        max_bytes: Transfer ceiling for the download.
        search_depth: How far back to look for the id (1-100).
    """
    try:
        depth = bounded(search_depth, LIMITS["list_photos"])
        if depth.error:
            return [depth.error]
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        reference = await find_photo_reference(cl, entity, source, photo_id, depth.value)
        if reference is None:
            if photo_id is None:
                return ["This peer has no photo to open."]
            return [
                f"No photo with id {photo_id} in this peer's '{source}' photos within the "
                f"newest {depth.value}. Use list_photos to see what is there, and check the "
                "source - an id from 'messages' does not name anything in 'avatars'."
            ]

        cap = _cap(max_bytes)
        raw, overflowed = await download_photo_bytes(cl, reference, cap)
        if overflowed:
            return [
                f"This photo is larger than the {cap}-byte limit (max_bytes). The transfer "
                f"was stopped once it crossed that; raise max_bytes up to the "
                f"{MAX_FRAME_SOURCE_BYTES}-byte ceiling."
            ]
        if not raw:
            return ["Telegram returned no data for this photo."]

        records, images = await encode_still_cancellable(raw, max_dimension)
        return [
            format_tool_result(
                {"source": source, "photo": reference.describe(), "encoded": records}
            ),
            *images,
        ]
    except UnknownPhotoSource as error:
        return [str(error)]
    except Exception as e:
        return [log_and_format_error("open_photo", e)]


@mcp.tool(
    annotations=ToolAnnotations(title="Get Photo Sheet", openWorldHint=True, readOnlyHint=True)
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_photo_sheet(
    chat_id: Union[int, str],
    source: str = AVATAR_SOURCE,
    limit: int = 6,
    columns: Optional[int] = None,
    account: str = None,
) -> list:
    """
    One labelled grid of a peer's photos, instead of one image block each.

    Every cell carries the id `open_photo` takes, so a reader can pick from the
    sheet and then ask for that one at full size. Tiles are fetched as thumbnails,
    and a tile that will not decode keeps its label rather than losing the sheet.

    Args:
        chat_id: The chat, channel or user.
        source: `avatars` (default) or `messages`.
        limit: How many photos to place (1-24).
        columns: Grid width; omitted picks a near-square layout.
    """
    try:
        bound = bounded(limit, LIMITS["get_photo_sheet"])
        if bound.error:
            return [bound.error]
        wanted = min(bound.value, MAXIMUM_TILES)
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        references = await list_photo_references(cl, entity, source, wanted)
        if not references:
            return [f"This peer has no '{source}' photos to compose."]

        # There is no ordering between the FETCHES - only between the tiles once
        # they are placed - so a sheet of 24 used to pay 24 sequential round trips
        # for the one tool whose whole purpose is seeing them all at once.
        # get_custom_emoji made this same change and measured the serial version
        # at "ten round trips end to end for work with no ordering between the
        # items". The width comes from the byte budget so that concurrency cannot
        # multiply peak memory past MAX_BATCH_BYTES.
        gate = asyncio.Semaphore(batch_width(len(references), SHEET_TILE_BYTES))

        async def _fetch(reference):
            async with gate:
                return await download_photo_bytes(cl, reference, SHEET_TILE_BYTES, thumbnail=True)

        fetched = await asyncio.gather(
            *(_fetch(reference) for reference in references),
            return_exceptions=True,
        )

        tiles = []
        skipped = []
        # Zipped against `references`, so placement order is the reference order
        # however the downloads happened to finish.
        for reference, outcome in zip(references, fetched):
            if isinstance(outcome, asyncio.CancelledError):
                # A real cancellation of this tool, not one tile failing.
                raise outcome
            if isinstance(outcome, BaseException):
                skipped.append(reference.identifier)
                continue
            raw, overflowed = outcome
            if overflowed or not raw:
                skipped.append(reference.identifier)
                continue
            label = f"id={reference.identifier}"
            if reference.is_current:
                label += " current"
            tiles.append((raw, label))

        if not tiles:
            return [f"None of this peer's '{source}' photos could be fetched for a sheet."]

        encoded, meta = await asyncio.to_thread(compose_contact_sheet, tiles, columns)
        meta.update({"source": source, "requested_limit": limit, "placed": len(tiles)})
        if skipped:
            meta["skipped_ids"] = skipped
        return [format_tool_result(meta), Image(data=encoded, format="png")]
    except (ContactSheetError, UnknownPhotoSource) as error:
        return [str(error)]
    except Exception as e:
        return [log_and_format_error("get_photo_sheet", e)]


async def encode_still_cancellable(raw: bytes, max_dimension: int) -> tuple:
    """One still image, encoded off the event loop."""
    return await asyncio.to_thread(_encode_one, raw, max_dimension)


__all__ = ["get_photo_sheet", "list_photos", "open_photo"]
