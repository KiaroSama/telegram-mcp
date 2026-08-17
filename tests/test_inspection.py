"""Tests for telegram_mcp.tools.inspection helpers.

Covers the two pieces that carry real logic and no network: the capped download
(including the link-preview media that ``iter_download`` cannot resolve on its
own) and the window-title/chat comparison behind ``title_matches_chat``.
"""

from types import SimpleNamespace
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


# --- Nested window metadata must be sanitized too -----------------------------


def test_safe_window_dict_sanitizes_the_nested_title():
    """inspect_message embeds window.to_dict(); the title inside it is user content."""
    from telegram_mcp.tools.inspection import safe_window_dict

    raw = {"hwnd": 1, "title": "Chat\u202ename\nsecond line", "width": 10}
    cleaned = safe_window_dict(dict(raw))

    assert "\u202e" not in cleaned["title"], "bidi override survived into the nested title"
    assert "\n" not in cleaned["title"], "the nested title is still multi-line"
    assert cleaned["hwnd"] == 1 and cleaned["width"] == 10, "unrelated fields were altered"


def test_safe_window_dict_keeps_a_persian_or_emoji_chat_name():
    from telegram_mcp.tools.inspection import safe_window_dict

    title = "\u0645\u06cc\u200c\u06a9\u0646\u062f \U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert safe_window_dict({"title": title})["title"] == title


def test_inspect_message_screen_block_uses_the_shared_helper():
    """The nested title and the top-level title must not diverge again."""
    import inspect as _inspect

    from telegram_mcp.tools import inspection

    source = _inspect.getsource(inspection.inspect_message)
    assert "safe_window_dict(window.to_dict())" in source
    assert "window.to_dict()," not in source, "a raw window dict is still being embedded"


# --- Title matching must normalize both sides the same way -------------------


@pytest.mark.parametrize(
    "label, title",
    [
        ("persian zwnj", "\u0645\u06cc\u200c\u06a9\u0646\u062f"),
        ("emoji zwj family", "Chat \U0001f468\u200d\U0001f469\u200d\U0001f467"),
        (
            "regional flag",
            "Team \U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
        ),
    ],
)
def test_title_matches_chat_survives_compound_unicode(label, title):
    """The window title is normalized with display_name; the chat name must be too.

    sanitize_name strips the ZWNJ/ZWJ from only one side, so the same title
    stopped matching itself.
    """
    from telegram_mcp.tools.inspection import _title_matches_chat
    from telegram_mcp.message_view import display_name

    entity = SimpleNamespace(title=title, username=None, first_name=None, last_name=None)
    window_title = f"{display_name(title)} (3)"  # Telegram appends an unread count

    assert _title_matches_chat(window_title, entity) is True, label


def test_title_matches_chat_still_reports_a_mismatch():
    from telegram_mcp.tools.inspection import _title_matches_chat

    entity = SimpleNamespace(
        title="Project Updates", username=None, first_name=None, last_name=None
    )
    assert _title_matches_chat("Totally Different Chat (2)", entity) is False


def test_chat_names_drops_names_too_short_to_be_evidence():
    from telegram_mcp.tools.inspection import _chat_names

    entity = SimpleNamespace(title="M", username=None, first_name=None, last_name=None)
    assert _chat_names(entity) == []


# --- Adaptive (context-coloured) custom emoji --------------------------------


def _emoji_document(mime="image/webp", **attrs):
    """A stub custom-emoji document with a DocumentAttributeCustomEmoji."""
    attribute = SimpleNamespace(
        alt="\U0001f600", stickerset=SimpleNamespace(short_name="SetName", id=1), **attrs
    )
    return SimpleNamespace(id=42, mime_type=mime, size=1234, attributes=[attribute])


@pytest.mark.asyncio
async def test_custom_emoji_metadata_exposes_text_color_and_free():
    from telegram_mcp.tools.inspection import _custom_emoji_preview

    document = _emoji_document(text_color=True, free=True)

    class _Client:
        def iter_download(self, location):
            return _Iter([], [])  # the preview is irrelevant here; the flags are not

    record, _images = await _custom_emoji_preview(_Client(), document, count=1, max_dimension=64)

    assert record["text_color"] is True
    assert record["free"] is True


@pytest.mark.asyncio
async def test_adaptive_custom_emoji_preview_is_never_called_exact():
    """A context-coloured emoji has no colour of its own; saying otherwise lies."""
    from telegram_mcp.tools.inspection import _custom_emoji_preview

    class _Client:
        def iter_download(self, location):
            return _Iter([], [])

    adaptive, _ = await _custom_emoji_preview(
        _Client(), _emoji_document(text_color=True), count=1, max_dimension=64
    )
    plain, _ = await _custom_emoji_preview(_Client(), _emoji_document(), count=1, max_dimension=64)

    assert adaptive["color_fidelity"] == "context-neutral"
    assert "get_telegram_frames" in adaptive["color_note"]
    assert "NOT the colour" in adaptive["color_note"]
    # A normal emoji carries no colour caveat, so the flag stays meaningful.
    assert "color_fidelity" not in plain
    assert "text_color" not in plain


def test_custom_emoji_docstring_matches_the_lottie_behaviour():
    """MCP agents choose tools from the docstring; a stale one misroutes them."""
    from telegram_mcp.tools import inspection

    doc = inspection.get_custom_emoji.__doc__ or ""
    assert "telegram-mcp[lottie]" in doc
    assert "text_color" in doc
    assert "nothing here rasterises" not in doc, "the old never-renders claim is still there"


# --- Premium sticker effect sampling -----------------------------------------


def _effect_message(with_effect=True, effect_size=4096):
    video_thumbs = [SimpleNamespace(type="v", w=100, h=100, size=10)]
    if with_effect:
        video_thumbs.append(SimpleNamespace(type="f", w=512, h=512, size=effect_size))
    # A real Document always carries the pair that authorises a download; without
    # them the fake cannot exercise the streaming path at all.
    document = SimpleNamespace(
        id=7,
        access_hash=11,
        file_reference=b"ref",
        attributes=[],
        thumbs=[],
        video_thumbs=video_thumbs,
    )
    return SimpleNamespace(id=99, media=object(), document=document, sticker=document, file=None)


@pytest.mark.asyncio
async def test_premium_effect_frames_are_labelled_as_asset_only():
    """The asset is not the composite Telegram draws; saying otherwise would mislead."""
    import json

    from telegram_mcp.tools.inspection import _premium_effect_frames

    msg = _effect_message()

    class _Client:
        def iter_download(self, location):
            assert (
                getattr(location, "thumb_size", None) == "f"
            ), "the effect asset was not requested"
            return _Iter([bytes([0x1F, 0x8B]) + b"lottie-payload"], [])

    async def _fake_frames(fn, raw, suffix, count, max_dimension):
        # Verified against live Telegram data: the type="f" asset is a gzipped
        # Lottie, so asserting .webm here is what locked the bug in.
        assert suffix == ".tgs", f"the effect was decoded as {suffix}, not Lottie"
        return [{"frame_index": 0}], ["image"]

    import telegram_mcp.tools.inspection as inspection

    original = inspection.asyncio.to_thread
    inspection.asyncio.to_thread = _fake_frames
    try:
        result = await _premium_effect_frames(
            _Client(),
            msg,
            {"premium_effect": {"kind": "premium_sticker_effect"}, "kind": "sticker"},
            count=2,
            max_dimension=256,
            max_bytes=50 * 1024 * 1024,
        )
    finally:
        inspection.asyncio.to_thread = original

    payload = json.loads(result[0])
    assert payload["results"][0]["source_asset"] == "premium_effect"
    assert payload["results"][0]["composite_fidelity"] == "asset-only"
    assert "ON ITS OWN" in payload["note"]
    assert "get_telegram_frames" in payload["note"]


@pytest.mark.asyncio
async def test_premium_effect_request_without_an_effect_says_so():
    from telegram_mcp.tools.inspection import _premium_effect_frames

    result = await _premium_effect_frames(
        object(),
        _effect_message(with_effect=False),
        {"kind": "sticker"},
        count=2,
        max_dimension=64,
        max_bytes=50 * 1024 * 1024,
    )

    assert isinstance(result, str)
    assert "no premium sticker effect" in result
    assert "get_media_details" in result


_EFFECT_DETAILS = {"premium_effect": {"kind": "premium_sticker_effect"}, "kind": "sticker"}


class _EffectClient:
    """Records what was requested and returns bytes of a chosen length."""

    def __init__(self, payload=b"webm"):
        self.payload = payload
        self.called = False
        self.requested_thumb = None

    def iter_download(self, location):
        self.called = True
        self.requested_thumb = getattr(location, "thumb_size", None)
        return _Iter([self.payload], [])


@pytest.mark.asyncio
async def test_oversized_effect_is_refused_before_the_transfer():
    """The advertised effect size gates the transfer, so nothing is pulled."""
    from telegram_mcp.tools.inspection import _premium_effect_frames

    client = _EffectClient()
    result = await _premium_effect_frames(
        client,
        _effect_message(effect_size=10_000),
        _EFFECT_DETAILS,
        count=2,
        max_dimension=64,
        max_bytes=1000,
    )

    assert isinstance(result, str) and "above the 1000-byte limit" in result
    assert client.called is False, "the transfer started despite the size gate"


@pytest.mark.asyncio
async def test_an_unadvertised_oversized_effect_is_stopped_mid_transfer():
    """No advertised size means the transfer itself is the only real limit.

    A VideoSize *can* be streamed - it is an ordinary file location carrying a
    thumb type - so the bytes are cut off at the cap instead of being buffered in
    full and measured afterwards.
    """
    from telegram_mcp.tools.inspection import _premium_effect_frames

    delivered = 0

    class _StreamingClient:
        def iter_download(self, target):
            class Chunks:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    nonlocal delivered
                    if delivered >= 5000:
                        raise StopAsyncIteration
                    delivered += 500
                    return b"x" * 500

                async def close(self):
                    pass

            return Chunks()

    result = await _premium_effect_frames(
        _StreamingClient(),
        _effect_message(effect_size=None),
        _EFFECT_DETAILS,
        count=2,
        max_dimension=64,
        max_bytes=1000,
    )

    assert isinstance(result, str)
    assert "larger than the 1000-byte limit" in result
    assert "advertised size was absent" in result
    assert delivered <= 1500, f"{delivered} bytes were pulled past a 1000-byte cap"


@pytest.mark.asyncio
async def test_the_hard_ceiling_still_applies_when_max_bytes_is_raised():
    from telegram_mcp.tools.inspection import MAX_FRAME_SOURCE_BYTES, _premium_effect_frames

    client = _EffectClient()
    result = await _premium_effect_frames(
        client,
        _effect_message(effect_size=MAX_FRAME_SOURCE_BYTES + 1),
        _EFFECT_DETAILS,
        count=2,
        max_dimension=64,
        max_bytes=10 * MAX_FRAME_SOURCE_BYTES,
    )

    assert isinstance(result, str)
    assert f"above the {MAX_FRAME_SOURCE_BYTES}-byte limit" in result


@pytest.mark.asyncio
async def test_an_effect_at_exactly_the_limit_is_accepted():
    from telegram_mcp.tools.inspection import _premium_effect_frames
    import telegram_mcp.tools.inspection as inspection

    client = _EffectClient(payload=b"x" * 1000)

    async def _fake(fn, raw, suffix, count, max_dimension):
        return [{"frame_index": 0}], ["image"]

    original = inspection.asyncio.to_thread
    inspection.asyncio.to_thread = _fake
    try:
        result = await _premium_effect_frames(
            client,
            _effect_message(effect_size=1000),
            _EFFECT_DETAILS,
            count=1,
            max_dimension=64,
            max_bytes=1000,
        )
    finally:
        inspection.asyncio.to_thread = original

    assert not isinstance(result, str), f"the exact limit was refused: {result}"
    assert client.called is True


def test_the_sticker_size_gate_runs_after_the_effect_branch():
    """A large sticker must not veto a small effect, nor a small one admit a large."""
    import inspect as _inspect

    from telegram_mcp.tools import inspection

    source = _inspect.getsource(inspection.get_media_frames)
    effect_branch = source.index("if premium_effect:")
    sticker_gate = source.index('size_bytes = details.get("size_bytes")')
    assert effect_branch < sticker_gate, "the sticker's own size still gates the effect"


# --- The premium effect is a Lottie, not a video -----------------------------


@pytest.mark.asyncio
async def test_a_gzipped_effect_asset_is_decoded_as_lottie():
    """Live Telegram data: VideoSize type="f" carries a .tgs, not a WebM."""
    import json

    import telegram_mcp.tools.inspection as inspection
    from telegram_mcp.tools.inspection import _premium_effect_frames

    seen = {}

    async def _fake(fn, raw, suffix, count, max_dimension):
        seen["suffix"] = suffix
        return [{"frame_index": 0}], ["image"]

    original = inspection.asyncio.to_thread
    inspection.asyncio.to_thread = _fake
    try:
        result = await _premium_effect_frames(
            _EffectClient(payload=b"\x1f\x8b\x08gzipped-lottie"),
            _effect_message(effect_size=64),
            _EFFECT_DETAILS,
            count=2,
            max_dimension=64,
            max_bytes=1024,
        )
    finally:
        inspection.asyncio.to_thread = original

    assert seen["suffix"] == ".tgs"
    assert json.loads(result[0])["results"][0]["asset_format"] == "lottie_tgs"


@pytest.mark.asyncio
async def test_a_non_gzip_effect_asset_still_falls_back_to_video():
    """Trust the bytes: a future format change must not be decoded as Lottie."""
    import json

    import telegram_mcp.tools.inspection as inspection
    from telegram_mcp.tools.inspection import _premium_effect_frames

    seen = {}

    async def _fake(fn, raw, suffix, count, max_dimension):
        seen["suffix"] = suffix
        return [{"frame_index": 0}], ["image"]

    original = inspection.asyncio.to_thread
    inspection.asyncio.to_thread = _fake
    try:
        result = await _premium_effect_frames(
            _EffectClient(payload=b"\x1aE\xdf\xa3webm"),
            _effect_message(effect_size=64),
            _EFFECT_DETAILS,
            count=2,
            max_dimension=64,
            max_bytes=1024,
        )
    finally:
        inspection.asyncio.to_thread = original

    assert seen["suffix"] == ".webm"
    assert json.loads(result[0])["results"][0]["asset_format"] == "video"


def test_max_bytes_is_clamped_before_the_effect_branch():
    """0 and negatives must behave the same on both media paths."""
    import inspect as _inspect

    from telegram_mcp.tools import inspection

    source = _inspect.getsource(inspection.get_media_frames)
    clamp = source.index("max_bytes = max(1, min(")
    branch = source.index("if premium_effect:")
    assert clamp < branch, "the effect path still receives the raw max_bytes"


# --- a thumbnail request must cost a thumbnail --------------------------------


class _CountingClient:
    """Streams a payload, recording every location and every byte delivered."""

    def __init__(self, total=512, chunk=1024, inline=b"inline-bytes"):
        self.total, self.chunk, self.inline = total, chunk, inline
        self.delivered = 0
        self.locations = []
        self.thumbs_asked = []

    def iter_download(self, target):
        self.locations.append(target)
        client = self

        class Chunks:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if client.delivered >= client.total:
                    raise StopAsyncIteration
                size = min(client.chunk, client.total - client.delivered)
                client.delivered += size
                return b"x" * size

            async def close(self):
                pass

        return Chunks()

    async def download_media(self, owner, file=None, thumb=None):
        self.thumbs_asked.append(thumb)
        return self.inline


_SMALL = t.PhotoSize(type="m", w=320, h=320, size=10_000)
_MEDIUM = t.PhotoSize(type="x", w=800, h=800, size=90_000)
_ORIGINAL = t.PhotoSizeProgressive(type="y", w=2560, h=2560, sizes=[2_000_000])


def _photo_with(sizes, video_sizes=None):
    return t.Photo(
        id=1,
        access_hash=2,
        file_reference=b"\x00",
        date=datetime.datetime.now(),
        sizes=list(sizes),
        dc_id=2,
        has_stickers=False,
        video_sizes=video_sizes,
    )


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


# --- one oversized custom emoji must not sink the batch -----------------------


@pytest.mark.asyncio
async def test_custom_emoji_refuses_a_document_over_the_cap(monkeypatch):
    from telegram_mcp.tools import inspection
    from telegram_mcp.tools.inspection import _custom_emoji_preview

    async def _to_thread(fn, *args):
        return [{"frame_index": 0}], ["image"]

    monkeypatch.setattr(inspection.asyncio, "to_thread", _to_thread)

    client = _CountingClient(total=512)
    oversized = SimpleNamespace(id=1, mime_type="image/webp", size=10_000_000, attributes=[])
    ordinary = SimpleNamespace(id=2, mime_type="image/webp", size=1234, attributes=[])

    refused, no_images = await _custom_emoji_preview(client, oversized, 1, 64, 5 * 1024 * 1024)
    rendered, images = await _custom_emoji_preview(client, ordinary, 1, 64, 5 * 1024 * 1024)

    assert "10000000 bytes" in refused["preview_error"]
    assert no_images == []
    assert client.locations == [ordinary], "the oversized document was still downloaded"
    assert images and "preview_error" not in rendered, "one bad emoji sank the whole batch"


@pytest.mark.asyncio
async def test_custom_emoji_transfer_is_bounded_when_no_size_is_advertised():
    """The advertised size is a free refusal, not the limit that counts."""
    from telegram_mcp.tools.inspection import _custom_emoji_preview

    client = _CountingClient(total=10 * 1024 * 1024)
    document = SimpleNamespace(id=3, mime_type="image/webp", size=None, attributes=[])

    record, images = await _custom_emoji_preview(client, document, 1, 64, 4096)

    assert "advertised size was absent" in record["preview_error"]
    assert images == []
    assert client.delivered <= 4096 + 1024, f"{client.delivered} bytes crossed a 4096-byte cap"


# --- Pillow must never decode on the event loop -------------------------------


@pytest.mark.asyncio
async def test_both_thumbnail_encodes_run_off_the_event_loop(monkeypatch):
    """A full-resolution decode plus LANCZOS resize stalls every other tool call."""
    from telegram_mcp.tools import inspection

    threaded = []

    async def _to_thread(fn, *args):
        threaded.append(fn.__name__)
        return [{"width": 1}], ["image"]

    photo = _photo_with([_MEDIUM])
    msg = SimpleNamespace(id=5, document=None, sticker=None, photo=photo, media=object())

    async def _get_message(chat_id, message_id, account=None):
        return _CountingClient(total=512), SimpleNamespace(title="Chat"), msg

    monkeypatch.setattr(inspection.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(inspection, "_get_message", _get_message)
    monkeypatch.setattr(
        inspection, "describe_media", lambda m: {"kind": "photo", "has_thumbnail": True}
    )
    monkeypatch.setattr(inspection, "message_to_dict", lambda m: {})
    monkeypatch.setattr(inspection, "deep_message_dict", lambda *a, **k: {})

    await inspection.get_media_thumbnail(1, 5, account="a")
    assert threaded == ["_encode_one"], f"get_media_thumbnail decoded inline: {threaded}"

    threaded.clear()
    await inspection.inspect_message(1, 5, include_thumbnail=True, account="a")
    assert threaded == ["_encode_one"], f"inspect_message decoded inline: {threaded}"


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


# --- an optional extra must never cost the answer -----------------------------


@pytest.mark.asyncio
async def test_a_broken_thumbnail_does_not_discard_the_whole_message(monkeypatch):
    """inspect_message's comment has always called the thumbnail optional.

    Only the refusal and over-cap paths actually were. An undecodable image or a
    file reference still stale after the retry raised straight past that block,
    and the tool returned a bare error string — throwing away the entire
    structured message the caller came for. The sibling include_screen block has
    always handled it the right way.
    """
    import json

    from telegram_mcp.tools import inspection

    async def _to_thread(fn, *args):
        raise OSError("image file is truncated")

    photo = _photo_with([_MEDIUM])
    msg = SimpleNamespace(id=5, document=None, sticker=None, photo=photo, media=object())

    async def _get_message(chat_id, message_id, account=None):
        return _CountingClient(total=512), SimpleNamespace(title="Chat"), msg

    monkeypatch.setattr(inspection.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(inspection, "_get_message", _get_message)
    monkeypatch.setattr(
        inspection, "describe_media", lambda m: {"kind": "photo", "has_thumbnail": True}
    )
    monkeypatch.setattr(inspection, "message_to_dict", lambda m: {})
    monkeypatch.setattr(
        inspection, "deep_message_dict", lambda *a, **k: {"text_fidelity": "the real answer"}
    )

    result = await inspection.inspect_message(1, 5, include_thumbnail=True, account="a")

    assert isinstance(result, list), f"the message was discarded: {result!r}"
    payload = json.loads(result[0])
    assert payload["results"][0]["text_fidelity"] == "the real answer"
    assert "OSError" in payload["results"][0]["thumbnail_error"]


@pytest.mark.asyncio
async def test_one_unresolvable_emoji_does_not_sink_the_other_nine(monkeypatch):
    """_custom_emoji_preview handles the two errors it expects and no others.

    Anything else — an RPC error, a reference still stale after the retry, a
    Pillow failure escaping the decoder — propagated out of a bare gather and
    sank all ten records, while the other coroutines were abandoned rather than
    cancelled.
    """
    import json

    from telegram_mcp.tools import inspection

    documents = [
        SimpleNamespace(id=1, mime_type="image/webp", size=10, attributes=[]),
        SimpleNamespace(id=2, mime_type="image/webp", size=10, attributes=[]),
    ]

    class _Client:
        async def __call__(self, request):
            return documents

    async def _ensure(client):
        return None

    finished = []

    async def _preview(client, document, count, max_dimension, max_bytes):
        if document.id == 1:
            raise RuntimeError("file reference still stale after the retry")
        finished.append(document.id)
        return {"document_id": document.id, "preview_source": "document"}, []

    monkeypatch.setattr(inspection, "get_client", lambda account=None: _Client())
    monkeypatch.setattr(inspection, "ensure_connected", _ensure)
    monkeypatch.setattr(inspection, "_custom_emoji_preview", _preview)

    result = await inspection.get_custom_emoji([1, 2], account="a")

    assert isinstance(result, list), f"the whole batch was lost: {result!r}"
    records = json.loads(result[0])["results"]
    assert finished == [2], "the surviving document never completed"
    assert [r["document_id"] for r in records] == [1, 2]
    assert "RuntimeError" in records[0]["preview_error"]
    assert records[1]["preview_source"] == "document"


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


@pytest.mark.asyncio
async def test_get_custom_emoji_never_exceeds_the_batch_budget(monkeypatch):
    """The gate is real: concurrent previews never outnumber the derived width."""
    import asyncio as _asyncio

    from telegram_mcp.media_transfer import MAX_BATCH_BYTES
    from telegram_mcp.tools import inspection

    documents = [
        SimpleNamespace(id=i, mime_type="image/webp", size=10, attributes=[]) for i in range(6)
    ]
    # The per-document ceiling would otherwise clamp max_bytes below the point
    # where the gate does anything, and a gate that cannot bite proves nothing.
    monkeypatch.setattr(inspection, "MAX_FRAME_SOURCE_BYTES", MAX_BATCH_BYTES)
    per_document = MAX_BATCH_BYTES // 3  # so at most 3 may be in flight

    class _BatchClient:
        async def __call__(self, request):
            return documents

    async def _ensure(client):
        return None

    live, peak = 0, 0

    async def _preview(client, document, count, max_dimension, max_bytes):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await _asyncio.sleep(0.01)
        live -= 1
        return {"document_id": document.id}, []

    monkeypatch.setattr(inspection, "get_client", lambda account=None: _BatchClient())
    monkeypatch.setattr(inspection, "ensure_connected", _ensure)
    monkeypatch.setattr(inspection, "_custom_emoji_preview", _preview)

    result = await inspection.get_custom_emoji(
        [d.id for d in documents], max_bytes=per_document, account="a"
    )

    assert isinstance(result, list), f"the batch failed outright: {result!r}"
    assert peak <= 3, f"{peak} previews ran at once, above the derived width"
    assert peak > 1, "the batch ran sequentially; concurrency was lost, not bounded"


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
