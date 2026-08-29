"""Peer photos: one question, three different Telegram shapes behind it.

Ported from the upstream project rather than merged - the histories are unrelated -
and adapted where this fork's rules differ. The adaptation that matters: upstream
downloads with `download_media(file=bytes)`, which fetches whatever the peer
declares. A photo id here comes from the caller, so every transfer goes through the
capped path instead.
"""

from types import SimpleNamespace

import pytest

from telegram_mcp import photo_source
from telegram_mcp.photo_source import (
    AVATAR_SOURCE,
    MESSAGE_SOURCE,
    PhotoReference,
    UnknownPhotoSource,
    download_photo_bytes,
    find_photo_reference,
    list_photo_references,
    validate_source,
)


def _size(width, type_="x"):
    return SimpleNamespace(w=width, h=width, type=type_, size=width * width)


def _photo(identifier, widths=(160, 640, 1280)):
    return SimpleNamespace(
        id=identifier,
        access_hash=1,
        file_reference=b"ref",
        sizes=[_size(w) for w in widths],
        date="2026-08-24",
    )


class _Client:
    """Enough of a Telethon client for the three shapes, and nothing more."""

    def __init__(self, photos=(), messages=()):
        self._photos = list(photos)
        self._messages = list(messages)
        self.requests = []
        self.filters = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(photos=self._photos)

    async def get_messages(self, entity, limit=None, filter=None):
        self.filters.append(type(filter).__name__)
        return self._messages[:limit]


def test_an_unknown_source_is_named_rather_than_guessed():
    with pytest.raises(UnknownPhotoSource) as caught:
        validate_source("selfies")

    message = str(caught.value)
    assert "selfies" in message
    assert AVATAR_SOURCE in message and MESSAGE_SOURCE in message


@pytest.mark.parametrize(
    "supplied,expected", [(None, AVATAR_SOURCE), ("  AVATARS ", AVATAR_SOURCE)]
)
def test_the_source_defaults_and_normalises(supplied, expected):
    assert validate_source(supplied) == expected


@pytest.mark.asyncio
async def test_a_user_is_asked_for_its_avatar_history_directly(monkeypatch):
    """Only a user has `photos.GetUserPhotos`; using it for a chat returns nothing."""
    from telethon.tl.types import User

    monkeypatch.setattr(photo_source, "User", User)
    entity = User(id=7, photo=SimpleNamespace(photo_id=202))
    client = _Client(photos=[_photo(201), _photo(202)])

    references = await list_photo_references(client, entity, AVATAR_SOURCE, 10)

    assert [r.identifier for r in references] == [201, 202]
    assert [r.is_current for r in references] == [False, True]
    assert client.requests, "the native avatar-history call was never made"


@pytest.mark.asyncio
async def test_a_chat_avatar_history_is_rebuilt_from_its_service_messages():
    """There is no GetChatPhotos, so each change's service message IS the history."""
    changed = [
        SimpleNamespace(action=SimpleNamespace(photo=_photo(301)), date="d1"),
        SimpleNamespace(action=None, date="d2"),  # an unrelated service message
        SimpleNamespace(action=SimpleNamespace(photo=_photo(302)), date="d3"),
    ]
    entity = SimpleNamespace(id=9, photo=SimpleNamespace(photo_id=302))
    client = _Client(messages=changed)

    references = await list_photo_references(client, entity, AVATAR_SOURCE, 10)

    assert [r.identifier for r in references] == [301, 302]
    assert references[1].is_current
    assert client.filters == ["InputMessagesFilterChatPhotos"]


@pytest.mark.asyncio
async def test_message_photos_are_keyed_by_message_id_not_photo_id():
    """A message id is what the message tools already hand out, and what reopens the
    photo in context. Keying by photo id would name something no other tool knows."""
    messages = [
        SimpleNamespace(id=5001, photo=_photo(401), date="d1", message="a caption"),
        SimpleNamespace(id=5002, photo=None, date="d2", message=""),
    ]
    client = _Client(messages=messages)

    references = await list_photo_references(client, SimpleNamespace(id=1), MESSAGE_SOURCE, 10)

    assert [r.identifier for r in references] == [5001]
    assert references[0].caption == "a caption"
    assert client.filters == ["InputMessagesFilterPhotos"]


@pytest.mark.asyncio
async def test_no_identifier_means_the_current_avatar():
    entity = SimpleNamespace(id=9, photo=SimpleNamespace(photo_id=302))
    client = _Client(
        messages=[
            SimpleNamespace(action=SimpleNamespace(photo=_photo(301)), date="d1"),
            SimpleNamespace(action=SimpleNamespace(photo=_photo(302)), date="d2"),
        ]
    )

    found = await find_photo_reference(client, entity, AVATAR_SOURCE, None, 10)

    assert found.identifier == 302 and found.is_current


@pytest.mark.asyncio
async def test_an_identifier_from_the_other_source_finds_nothing():
    """Rather than returning something plausible from the wrong list."""
    entity = SimpleNamespace(id=9, photo=None)
    client = _Client(
        messages=[SimpleNamespace(action=SimpleNamespace(photo=_photo(301)), date="d")]
    )

    assert await find_photo_reference(client, entity, AVATAR_SOURCE, 5001, 10) is None


@pytest.mark.asyncio
async def test_the_download_is_capped_and_reports_an_overflow(monkeypatch):
    """The adaptation this port exists for. Upstream fetches whatever is declared;
    a photo id is caller-supplied, so the transfer has to be bounded and a photo
    that outgrows the cap abandoned rather than buffered and then measured."""
    seen = {}

    async def _capped(client, owner, size, max_bytes):
        seen["max_bytes"] = max_bytes
        seen["width"] = size.w
        return b"", True

    monkeypatch.setattr(photo_source, "_download_size_capped", _capped)
    reference = PhotoReference(identifier=1, photo=_photo(1), is_current=True)

    data, overflowed = await download_photo_bytes(None, reference, max_bytes=1024)

    assert overflowed is True and data == b""
    assert seen["max_bytes"] == 1024, "the cap was not passed to the capped transfer"


@pytest.mark.asyncio
async def test_a_thumbnail_request_picks_the_largest_size_inside_the_budget(monkeypatch):
    chosen = {}

    async def _capped(client, owner, size, max_bytes):
        chosen["width"] = size.w
        return b"bytes", False

    monkeypatch.setattr(photo_source, "_download_size_capped", _capped)
    reference = PhotoReference(
        identifier=1, photo=_photo(1, widths=(90, 320, 800)), is_current=True
    )

    await download_photo_bytes(None, reference, max_bytes=99999, thumbnail=True)
    assert chosen["width"] == 320, "a thumbnail took a size above the thumbnail budget"

    await download_photo_bytes(None, reference, max_bytes=99999, thumbnail=False)
    assert chosen["width"] == 800, "the full request did not take the largest size"


@pytest.mark.asyncio
async def test_a_photo_declaring_no_size_is_refused_not_fetched_unbounded():
    """There is no bounded way to ask for a size that was never declared."""
    reference = PhotoReference(identifier=1, photo=_photo(1, widths=()), is_current=True)

    data, overflowed = await download_photo_bytes(None, reference, max_bytes=1024)

    assert data == b"" and overflowed is False
