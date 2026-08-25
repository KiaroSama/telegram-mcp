"""Resolve a peer's photos from every source Telegram offers them through.

Telegram does not have one photo list. A user's avatar history comes back from
``photos.GetUserPhotos``; a chat or channel has no such call, so its avatar history
has to be read out of the service messages that record each change; and photos
*sent* in a conversation are ordinary messages found with a media filter. Three
shapes, one question, so the difference is answered here rather than at each tool.

Downloads are bounded. Telethon's ``download_media(file=bytes)`` fetches whatever
the peer declares, and a photo identifier is attacker-supplied, so every transfer
here goes through :func:`telegram_mcp.media_transfer._download_size_capped` and
reports an overflow instead of returning a buffer nobody asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from telethon.tl import functions, types
from telethon.tl.types import Channel, Chat, User

from telegram_mcp.media_transfer import _declared_sizes, _download_size_capped

AVATAR_SOURCE = "avatars"
MESSAGE_SOURCE = "messages"
PHOTO_SOURCES = (AVATAR_SOURCE, MESSAGE_SOURCE)

# The widest edge a "thumbnail" may have. Above this a preview stops being cheaper
# than the original, which is the only reason to ask for one.
THUMBNAIL_TARGET_PIXELS = 320


class UnknownPhotoSource(ValueError):
    """A caller asked for a source that does not exist."""


@dataclass(frozen=True)
class PhotoReference:
    """One retrievable photo plus the identifier that opens it again."""

    identifier: int
    photo: Any
    is_current: bool
    taken_at: Optional[Any] = None
    caption: str = ""

    def describe(self) -> dict:
        described = {
            "id": self.identifier,
            "date": self.taken_at,
            "is_current": self.is_current,
        }
        if self.caption:
            described["caption"] = self.caption
        return described


def validate_source(source: str) -> str:
    normalised = (source or AVATAR_SOURCE).strip().lower()
    if normalised not in PHOTO_SOURCES:
        raise UnknownPhotoSource(
            f"Unknown photo source {source!r}. Expected one of: {', '.join(PHOTO_SOURCES)}."
        )
    return normalised


def _current_photo_id(entity: Any) -> Optional[int]:
    return getattr(getattr(entity, "photo", None), "photo_id", None)


def _has_native_avatar_history(entity: Any) -> bool:
    """Only a user has a real avatar-history call; everything else is reconstructed."""
    return isinstance(entity, User)


async def _user_avatars(client, entity, limit: int) -> List[PhotoReference]:
    retrieved = await client(
        functions.photos.GetUserPhotosRequest(user_id=entity, offset=0, max_id=0, limit=limit)
    )
    current = _current_photo_id(entity)
    return [
        PhotoReference(
            identifier=photo.id,
            photo=photo,
            is_current=photo.id == current,
            taken_at=getattr(photo, "date", None),
        )
        for photo in retrieved.photos
    ]


async def _chat_avatars(client, entity, limit: int) -> List[PhotoReference]:
    """A chat's avatar history, rebuilt from the service messages that recorded it.

    There is no ``GetChatPhotos``; each change leaves a service message whose action
    carries the new photo, so the filter is the history.
    """
    service_messages = await client.get_messages(
        entity, limit=limit, filter=types.InputMessagesFilterChatPhotos()
    )
    current = _current_photo_id(entity)

    references: List[PhotoReference] = []
    for message in service_messages:
        changed = getattr(getattr(message, "action", None), "photo", None)
        if changed is None:
            continue
        references.append(
            PhotoReference(
                identifier=changed.id,
                photo=changed,
                is_current=changed.id == current,
                taken_at=getattr(message, "date", None),
            )
        )
    return references


async def _message_photos(client, entity, limit: int) -> List[PhotoReference]:
    """Photos SENT in the conversation - keyed by message id, not by photo id.

    A message id is what a caller already has from the message tools, and it is what
    reopens the photo in context.
    """
    photo_messages = await client.get_messages(
        entity, limit=limit, filter=types.InputMessagesFilterPhotos()
    )
    return [
        PhotoReference(
            identifier=message.id,
            photo=message.photo,
            is_current=False,
            taken_at=getattr(message, "date", None),
            caption=getattr(message, "message", "") or "",
        )
        for message in photo_messages
        if getattr(message, "photo", None) is not None
    ]


async def list_photo_references(client, entity, source: str, limit: int) -> List[PhotoReference]:
    """The newest ``limit`` photos of ``entity`` from the requested source."""
    resolved = validate_source(source)
    if resolved == MESSAGE_SOURCE:
        return await _message_photos(client, entity, limit)
    if _has_native_avatar_history(entity):
        return await _user_avatars(client, entity, limit)
    return await _chat_avatars(client, entity, limit)


async def find_photo_reference(
    client, entity, source: str, identifier: Optional[int], search_depth: int
) -> Optional[PhotoReference]:
    """One photo by identifier, or the current avatar when none is given."""
    references = await list_photo_references(client, entity, source, search_depth)
    if not references:
        return None
    if identifier is None:
        return next((each for each in references if each.is_current), None) or references[0]
    return next((each for each in references if each.identifier == identifier), None)


def _thumbnail_size(photo: Any):
    """The largest declared size still inside the thumbnail budget, or ``None``.

    Returns the size OBJECT rather than its index: Telethon sorts the size list and
    folds video sizes in before indexing, so an index chosen here names a different
    size by the time it is used.
    """
    candidates = [
        size
        for size in _declared_sizes(photo)
        if 0 < (getattr(size, "w", 0) or 0) <= THUMBNAIL_TARGET_PIXELS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda size: getattr(size, "w", 0) or 0)


def _largest_size(photo: Any):
    """The biggest declared size, or ``None`` when the photo declares none."""
    sizes = _declared_sizes(photo)
    if not sizes:
        return None
    return max(sizes, key=lambda size: getattr(size, "w", 0) or 0)


async def download_photo_bytes(
    client, reference: PhotoReference, max_bytes: int, thumbnail: bool = False
) -> tuple:
    """``(bytes, overflowed)`` for one referenced photo, straight to memory.

    Never to disk, and never unbounded: the identifier that chose this photo came
    from the caller, so the transfer is capped and a photo that outgrows the cap is
    abandoned mid-flight rather than buffered and then measured.
    """
    size = _thumbnail_size(reference.photo) if thumbnail else None
    if size is None:
        size = _largest_size(reference.photo)
    if size is None:
        # Nothing declared a size, so there is no bounded way to ask for it.
        return b"", False
    return await _download_size_capped(client, reference.photo, size, max_bytes)


def peer_supports_source(entity: Any, source: str) -> bool:
    """Whether the peer can serve that source at all, so an empty list can say why."""
    if validate_source(source) == MESSAGE_SOURCE:
        return isinstance(entity, (Chat, Channel, User))
    return True


__all__ = [
    "AVATAR_SOURCE",
    "MESSAGE_SOURCE",
    "PHOTO_SOURCES",
    "PhotoReference",
    "UnknownPhotoSource",
    "download_photo_bytes",
    "find_photo_reference",
    "list_photo_references",
    "peer_supports_source",
    "validate_source",
]
