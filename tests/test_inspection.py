"""Tests for telegram_mcp.tools.inspection helpers.

Covers the two pieces that carry real logic and no network: the capped download
(including the link-preview media that ``iter_download`` cannot resolve on its
own) and the window-title/chat comparison behind ``title_matches_chat``.
"""

import datetime

import pytest
from telethon import utils
from telethon.tl import types as t

from telegram_mcp.tools.inspection import _chat_names, _download_capped, _title_matches_chat


class _Iter:
    def __init__(self, chunks, log):
        self._chunks, self._log = chunks, log

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()

    async def close(self):
        self._log.append("closed")


class _Client:
    def __init__(self, chunks):
        self.chunks, self.log, self.seen = chunks, [], []

    def iter_download(self, media):
        self.seen.append(media)
        return _Iter(self.chunks, self.log)


class _LegacyClient:
    """Telethon old enough to have no iter_download."""

    def __init__(self, blob):
        self.blob = blob

    async def download_media(self, msg, file=None):
        return self.blob


class _Msg:
    def __init__(self, media, document=None, photo=None):
        self.media, self.document, self.photo = media, document, photo


def _photo():
    return t.Photo(
        id=1,
        access_hash=2,
        file_reference=b"\x00",
        date=datetime.datetime.now(),
        sizes=[t.PhotoSize(type="y", w=800, h=800, size=90000)],
        dc_id=2,
        has_stickers=False,
    )


@pytest.mark.asyncio
async def test_download_capped_returns_bytes_under_the_cap():
    client = _Client([b"a" * 10, b"b" * 10])
    data, over_cap = await _download_capped(client, _Msg(object()), 100)
    assert data == b"a" * 10 + b"b" * 10
    assert over_cap is False
    assert client.log == ["closed"]


@pytest.mark.asyncio
async def test_download_capped_aborts_and_still_closes_the_iterator():
    client = _Client([b"a" * 10, b"b" * 10])
    data, over_cap = await _download_capped(client, _Msg(object()), 15)
    assert (data, over_cap) == (None, True)
    # Breaking out of the async-for must not leave the borrowed DC sender open.
    assert client.log == ["closed"]


@pytest.mark.asyncio
async def test_download_capped_allows_media_of_exactly_the_cap():
    client = _Client([b"a" * 20])
    data, over_cap = await _download_capped(client, _Msg(object()), 20)
    assert (len(data), over_cap) == (20, False)


@pytest.mark.asyncio
async def test_download_capped_falls_back_without_iter_download():
    data, over_cap = await _download_capped(_LegacyClient(b"x" * 50), _Msg(object()), 10)
    assert (data, over_cap) == (b"x" * 50, True)


@pytest.mark.asyncio
async def test_download_capped_unwraps_a_link_preview():
    """``describe_media`` reports a link preview as a downloadable photo, but
    ``iter_download`` cannot cast ``MessageMediaWebPage`` to a file location —
    only the photo inside it."""
    photo = _photo()
    media = t.MessageMediaWebPage(
        webpage=t.WebPage(id=1, url="u", display_url="u", hash=0, type="photo", photo=photo)
    )
    with pytest.raises(TypeError):
        utils.get_input_location(media)

    client = _Client([b"jpegbytes"])
    data, over_cap = await _download_capped(client, _Msg(media, photo=photo), 1000)
    assert (data, over_cap) == (b"jpegbytes", False)
    assert client.seen == [photo]
    utils.get_input_location(client.seen[0])  # resolvable, unlike the wrapper


@pytest.mark.asyncio
async def test_download_capped_passes_ordinary_media_through_unchanged():
    photo = _photo()
    media = t.MessageMediaPhoto(photo=photo)
    client = _Client([b"z"])
    await _download_capped(client, _Msg(media, photo=photo), 1000)
    assert utils.get_input_location(client.seen[0]) == utils.get_input_location(media)


class _Entity:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.mark.parametrize(
    "title, entity, expected",
    [
        ("(2) Persian Meme", _Entity(title="Persian Meme"), True),
        ("M (3374824)", _Entity(title="Persian Meme"), False),
        ("Telegram", _Entity(title="Persian Meme"), False),
        ("Durov (12) - Telegram", _Entity(username="durov"), True),
        ("Ada Lovelace", _Entity(first_name="Ada", last_name="Lovelace"), True),
        # A one-character chat name matches almost any title, so it is no hint.
        ("M (3374824)", _Entity(title="M"), None),
        ("", _Entity(title="Persian Meme"), None),
        ("Telegram", _Entity(), None),
    ],
)
def test_title_matches_chat(title, entity, expected):
    assert _title_matches_chat(title, entity) is expected


def test_chat_names_drops_names_too_short_to_mean_anything():
    assert _chat_names(_Entity(title="M")) == []
    assert _chat_names(_Entity(title="Persian Meme")) == ["Persian Meme"]
