"""The three photo tools as a caller meets them.

Patched at `download_photo_bytes` rather than at Telethon's streaming API: the
tools' job is orchestration - choosing a source, labelling a sheet, explaining a
missing id - and whether the transfer itself is bounded is settled in
`test_photo_source.py` against the capped path directly.
"""

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from telegram_mcp.photo_source import PhotoReference
from telegram_mcp.tools import photos as photo_tools


def _png(size=(160, 160), colour=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _unwrap(text):
    payload = json.loads(text)
    if "accounts" in payload:
        payload = next(iter(payload["accounts"].values()))
    return payload.get("results", payload)


@pytest.fixture
def _wired(monkeypatch):
    """A peer with three avatars, the newest current."""
    entity = SimpleNamespace(id=42, photo=SimpleNamespace(photo_id=903))
    references = [
        PhotoReference(identifier=901, photo=SimpleNamespace(id=901), is_current=False),
        PhotoReference(identifier=902, photo=SimpleNamespace(id=902), is_current=False),
        PhotoReference(identifier=903, photo=SimpleNamespace(id=903), is_current=True),
    ]

    async def _list(client, ent, source, limit):
        return references[:limit]

    async def _resolve(chat_id, client):
        return entity

    async def _ensure(client):
        return None

    monkeypatch.setattr(photo_tools, "get_client", lambda account=None: object())
    monkeypatch.setattr(photo_tools, "resolve_entity", _resolve)
    monkeypatch.setattr(photo_tools, "ensure_connected", _ensure)
    monkeypatch.setattr(photo_tools, "list_photo_references", _list)

    async def _find(client, ent, source, identifier, depth):
        if identifier is None:
            return next(r for r in references if r.is_current)
        return next((r for r in references if r.identifier == identifier), None)

    monkeypatch.setattr(photo_tools, "find_photo_reference", _find)
    return references


@pytest.mark.asyncio
async def test_listing_reports_the_ids_the_other_tools_take_back(_wired, monkeypatch):
    result = _unwrap(await photo_tools.list_photos(chat_id=42, account="default"))

    assert result["count"] == 3
    assert [p["id"] for p in result["photos"]] == [901, 902, 903]
    assert [p["id"] for p in result["photos"] if p["is_current"]] == [903]
    # The caller has to be able to tell what it asked for from what it got.
    assert result["requested_limit"] == 20 and result["effective_limit"] == 20


@pytest.mark.asyncio
async def test_a_sheet_labels_every_cell_with_the_id_that_opens_it(_wired, monkeypatch):
    """The label is the whole point: a reader picks a cell off the picture and then
    asks `open_photo` for that id."""

    async def _download(client, reference, max_bytes, thumbnail=False):
        assert thumbnail is True, "sheet tiles must be fetched as thumbnails"
        return _png(colour=(reference.identifier % 256, 0, 0)), False

    monkeypatch.setattr(photo_tools, "download_photo_bytes", _download)

    result = await photo_tools.get_photo_sheet(chat_id=42, limit=3, account="default")

    meta = _unwrap(result[0])
    assert [cell["label"] for cell in meta["cells"]] == ["id=901", "id=902", "id=903 current"]
    assert meta["placed"] == 3
    sheet = Image.open(io.BytesIO(result[1].data))
    assert sheet.width > 0 and sheet.height > 0


@pytest.mark.asyncio
async def test_a_tile_that_will_not_come_down_is_named_rather_than_dropped(_wired, monkeypatch):
    """A sheet quietly missing a photo is worse than one that says which is absent."""

    async def _download(client, reference, max_bytes, thumbnail=False):
        if reference.identifier == 902:
            return b"", True  # over the tile cap
        return _png(), False

    monkeypatch.setattr(photo_tools, "download_photo_bytes", _download)

    result = await photo_tools.get_photo_sheet(chat_id=42, limit=3, account="default")

    meta = _unwrap(result[0])
    assert meta["skipped_ids"] == [902]
    assert [cell["label"] for cell in meta["cells"]] == ["id=901", "id=903 current"]


@pytest.mark.asyncio
async def test_opening_without_an_id_returns_the_current_avatar(_wired, monkeypatch):
    async def _download(client, reference, max_bytes, thumbnail=False):
        assert reference.identifier == 903
        return _png(), False

    monkeypatch.setattr(photo_tools, "download_photo_bytes", _download)

    result = await photo_tools.open_photo(chat_id=42, account="default")

    assert _unwrap(result[0])["photo"]["id"] == 903
    assert Image.open(io.BytesIO(result[1].data)).size[0] > 0


@pytest.mark.asyncio
async def test_an_unknown_id_says_to_check_the_source_too(_wired, monkeypatch):
    """The commonest way to get this wrong is passing a `messages` id to `avatars`,
    and the id alone cannot tell them apart - so the message has to."""
    result = await photo_tools.open_photo(chat_id=42, photo_id=999999, account="default")

    message = result[0]
    assert "999999" in message
    assert "list_photos" in message
    assert "source" in message


@pytest.mark.asyncio
async def test_a_photo_over_the_cap_reports_the_cap_not_a_generic_failure(_wired, monkeypatch):
    async def _download(client, reference, max_bytes, thumbnail=False):
        return b"", True

    monkeypatch.setattr(photo_tools, "download_photo_bytes", _download)

    result = await photo_tools.open_photo(chat_id=42, photo_id=902, account="default")

    assert "max_bytes" in result[0]
    assert str(photo_tools.DEFAULT_PHOTO_BYTES) in result[0]


@pytest.mark.asyncio
async def test_the_byte_cap_is_never_raised_above_the_transfer_ceiling(_wired, monkeypatch):
    """A caller may lower it; nobody may raise it past what the transfer layer allows."""
    from telegram_mcp.media_transfer import MAX_FRAME_SOURCE_BYTES

    assert photo_tools._cap(10**12) == MAX_FRAME_SOURCE_BYTES
    assert photo_tools._cap(None) == photo_tools.DEFAULT_PHOTO_BYTES
    assert photo_tools._cap(0) == photo_tools.DEFAULT_PHOTO_BYTES
    assert photo_tools._cap(4096) == 4096
