"""Tests for the bounded-transfer helpers behind the inspection tools.

These all exercise ``telegram_mcp/media_transfer.py`` (re-exported through
``telegram_mcp.tools.inspection``): how many bytes a request is allowed to pull,
which size it is allowed to pull, when the transfer aborts, what happens to a
stale file reference, and how wide a batch may run. The tools that *use* those
answers are tested in ``test_inspection.py``.
"""

from types import SimpleNamespace
import datetime

import pytest
from telethon import utils
from telethon.tl import types as t

from helpers_inspection import _CountingClient, _Iter, _MEDIUM, _ORIGINAL, _SMALL, _photo_with
from telegram_mcp.tools.inspection import _download_capped


class _Client:
    def __init__(self, chunks):
        self.chunks, self.log, self.seen = chunks, [], []

    def iter_download(self, media):
        self.seen.append(media)
        return _Iter(self.chunks, self.log)


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


# --- a thumbnail request must cost a thumbnail --------------------------------


def _document_with(thumbs):
    return SimpleNamespace(id=7, access_hash=11, file_reference=b"ref", thumbs=list(thumbs))


def test_a_photo_thumbnail_request_never_selects_the_original():
    """Telethon's own thumb=-1 returns the full-resolution photo; this must not."""
    from telegram_mcp.tools.inspection import DEFAULT_THUMBNAIL_BYTES, _select_thumb

    sizes = [_SMALL, _MEDIUM, _ORIGINAL]
    index, size = _select_thumb(sizes, -1, DEFAULT_THUMBNAIL_BYTES)

    assert size is _MEDIUM, f"selected {getattr(size, 'type', size)!r}, not the largest that fits"
    assert index == 1
    assert size is not _ORIGINAL, "the full-resolution original was offered as a thumbnail"


def test_the_video_size_of_an_animated_photo_is_never_a_thumbnail():
    """Telethon folds photo.video_sizes into the sortable list; the fork must not."""
    from telegram_mcp.tools.inspection import (
        DEFAULT_THUMBNAIL_BYTES,
        _declared_sizes,
        _select_thumb,
    )

    mp4 = t.VideoSize(type="u", w=1280, h=1280, size=3_000_000)
    photo = _photo_with([_SMALL, _MEDIUM, _ORIGINAL], video_sizes=[mp4])

    declared = _declared_sizes(photo)
    assert mp4 not in declared, "the animated photo's mp4 entered the thumbnail vocabulary"

    _, size = _select_thumb(declared, -1, DEFAULT_THUMBNAIL_BYTES)
    assert type(size).__name__ == "PhotoSize"


@pytest.mark.asyncio
async def test_an_over_cap_thumbnail_aborts_during_the_transfer():
    """An unannounced or misreported size must never be buffered in full."""
    from telegram_mcp.tools.inspection import _download_size_capped

    client = _CountingClient(total=10 * 1024 * 1024)
    photo = _photo_with([_MEDIUM])

    raw, over_cap = await _download_size_capped(client, photo, _MEDIUM, 4096)

    assert (raw, over_cap) == (None, True)
    assert client.delivered <= 4096 + 1024, (
        f"{client.delivered} bytes crossed a 4096-byte cap; the transfer must abort at the "
        "limit, not download 10 MB and measure it afterwards"
    )


@pytest.mark.asyncio
async def test_an_inline_thumb_costs_no_request():
    """PhotoStrippedSize pixels already arrived with the message; fetching them is waste."""
    from telegram_mcp.tools.inspection import _download_size_capped

    stripped = t.PhotoStrippedSize(type="i", bytes=b"\x01\x02\x03")
    client = _CountingClient()
    photo = _photo_with([stripped])

    raw, over_cap = await _download_size_capped(client, photo, stripped, 4096)

    assert (raw, over_cap) == (b"inline-bytes", False)
    assert client.locations == [], "an inline thumbnail was turned into a network request"
    assert client.thumbs_asked == [stripped], "download_media was not given the size object"


def test_a_vector_outline_is_never_offered_as_a_thumbnail():
    """PhotoPathSize is an SVG outline; Pillow cannot decode one and Telethon drops it."""
    from telegram_mcp.tools.inspection import DEFAULT_THUMBNAIL_BYTES, _select_thumb

    outline = t.PhotoPathSize(type="j", bytes=b"M0 0")
    sizes = [outline, _MEDIUM]

    _, chosen = _select_thumb(sizes, -1, DEFAULT_THUMBNAIL_BYTES)
    assert chosen is _MEDIUM

    refusal = _select_thumb(sizes, 0, DEFAULT_THUMBNAIL_BYTES)
    assert isinstance(refusal, str), "the vector outline was handed to the decoder"
    assert "PhotoPathSize" in refusal and "carries no picture" in refusal


@pytest.mark.asyncio
async def test_a_photo_thumb_streams_an_input_photo_location():
    """A Photo and a Document need different location types for the same thumb_size."""
    from telethon.tl.types import InputDocumentFileLocation, InputPhotoFileLocation

    from telegram_mcp.tools.inspection import _download_size_capped

    client = _CountingClient(total=512)
    await _download_size_capped(client, _photo_with([_MEDIUM]), _MEDIUM, 4096)

    location = client.locations[0]
    assert isinstance(location, InputPhotoFileLocation)
    assert location.thumb_size == "x"
    assert (location.id, location.file_reference) == (1, b"\x00")

    document_client = _CountingClient(total=512)
    await _download_size_capped(document_client, _document_with([_MEDIUM]), _MEDIUM, 4096)

    assert isinstance(document_client.locations[0], InputDocumentFileLocation)
    assert document_client.locations[0].thumb_size == "x"


# --- a failing cleanup must not replace the failure that matters --------------


@pytest.mark.asyncio
async def test_a_failing_close_does_not_replace_the_real_error():
    """close() reads _sender, which only exists once the lazy _init has run."""
    from telegram_mcp.tools.inspection import _stream_capped

    class _BrokenIter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("dc")

        async def close(self):
            raise AttributeError("_sender")

    class _Client:
        def iter_download(self, target):
            return _BrokenIter()

    with pytest.raises(RuntimeError, match="dc"):
        await _stream_capped(_Client(), object(), 4096)


# --- an expired file reference is recoverable everywhere, not just for effects


@pytest.mark.asyncio
async def test_a_stale_reference_is_refreshed_and_retried_once():
    """The ids stay valid; only the reference expires, and refetching is the cure."""
    from telethon.errors import FileReferenceExpiredError

    from telegram_mcp.tools.inspection import with_reference_retry

    attempts, refreshes = [], []

    async def download(fresh):
        attempts.append(fresh)
        if fresh is None:
            raise FileReferenceExpiredError(request=None)
        return b"payload", False

    async def refresh():
        refreshes.append(1)
        return "fresh-object"

    assert await with_reference_retry(download, refresh) == (b"payload", False)
    assert attempts == [None, "fresh-object"], "the retry did not use the refreshed object"
    assert len(refreshes) == 1


@pytest.mark.asyncio
async def test_a_second_stale_failure_is_not_retried_again():
    """One refresh is the contract; a loop here is indistinguishable from a hang."""
    from telethon.errors import FileReferenceExpiredError

    from telegram_mcp.tools.inspection import with_reference_retry

    attempts = []

    async def download(fresh):
        attempts.append(fresh)
        raise FileReferenceExpiredError(request=None)

    async def refresh():
        return "fresh-object"

    with pytest.raises(FileReferenceExpiredError):
        await with_reference_retry(download, refresh)
    assert len(attempts) == 2, "the retry looped instead of giving up"


@pytest.mark.asyncio
async def test_an_unrelated_error_is_not_treated_as_a_stale_reference():
    from telegram_mcp.tools.inspection import with_reference_retry

    refreshes = []

    async def download(fresh):
        raise RuntimeError("connection reset")

    async def refresh():
        refreshes.append(1)
        return "fresh-object"

    with pytest.raises(RuntimeError):
        await with_reference_retry(download, refresh)
    assert refreshes == [], "an unrelated error triggered a reference refresh"


@pytest.mark.asyncio
async def test_a_source_that_cannot_refresh_reraises_the_original_error():
    """A deleted message has no fresh reference to give; do not dress that up."""
    from telethon.errors import FileReferenceExpiredError

    from telegram_mcp.tools.inspection import with_reference_retry

    async def download(fresh):
        raise FileReferenceExpiredError(request=None)

    async def refresh():
        return None

    with pytest.raises(FileReferenceExpiredError):
        await with_reference_retry(download, refresh)


# --- concurrency must not multiply the memory peak without a ceiling ----------


def test_batch_width_keeps_the_peak_under_the_budget():
    """Peak held in memory is width x max_bytes, so the width comes from the budget.

    Ten documents at the 200 MB per-document ceiling would otherwise hold ~2 GB
    at once, where the sequential version held one buffer.
    """
    from telegram_mcp.media_transfer import (
        MAX_BATCH_BYTES,
        MAX_FRAME_SOURCE_BYTES,
        batch_width,
    )

    for max_bytes in (1, 4096, 5 * 1024 * 1024, MAX_FRAME_SOURCE_BYTES):
        for items in (1, 3, 10, 64):
            width = batch_width(items, max_bytes)
            assert 1 <= width <= items
            assert width * max_bytes <= MAX_BATCH_BYTES, (
                f"{items} items of {max_bytes} bytes would peak at "
                f"{width * max_bytes}, above {MAX_BATCH_BYTES}"
            )

    # The ceiling must actually bite, not just happen to hold at today's values.
    assert batch_width(64, MAX_BATCH_BYTES // 4) == 4
    assert batch_width(64, MAX_BATCH_BYTES) == 1


def test_a_stripped_thumbnail_is_measured_as_it_will_arrive():
    """Telegram strips the JPEG header/footer; every client re-attaches them.

    Reporting the 61 wire bytes made the byte budget compare against a number
    nobody ever receives, and produced a diagnostic blaming Telegram for a
    transfer that never happened.
    """
    from telethon.tl.types import PhotoStrippedSize

    from telegram_mcp.media_transfer import _size_bytes

    wire = bytes([0x01, 40, 40]) + bytes(58)
    stripped = PhotoStrippedSize(type="i", bytes=wire)

    measured = _size_bytes(stripped)

    assert measured > len(wire), "the header Telethon re-attaches was not counted"
    assert measured == len(utils.stripped_photo_to_jpg(wire))
