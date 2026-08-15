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
        async def download_media(self, *args, **kwargs):
            return None  # the preview is irrelevant here; the flags are not

    record, _images = await _custom_emoji_preview(_Client(), document, count=1, max_dimension=64)

    assert record["text_color"] is True
    assert record["free"] is True


@pytest.mark.asyncio
async def test_adaptive_custom_emoji_preview_is_never_called_exact():
    """A context-coloured emoji has no colour of its own; saying otherwise lies."""
    from telegram_mcp.tools.inspection import _custom_emoji_preview

    class _Client:
        async def download_media(self, *args, **kwargs):
            return None

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
    document = SimpleNamespace(id=7, attributes=[], thumbs=[], video_thumbs=video_thumbs)
    return SimpleNamespace(id=99, media=object(), document=document, sticker=document, file=None)


@pytest.mark.asyncio
async def test_premium_effect_frames_are_labelled_as_asset_only():
    """The asset is not the composite Telegram draws; saying otherwise would mislead."""
    import json

    from telegram_mcp.tools.inspection import _premium_effect_frames

    msg = _effect_message()

    class _Client:
        async def download_media(self, document, file=None, thumb=None):
            assert thumb is not None and thumb.type == "f", "the effect asset was not requested"
            return bytes([0x1F, 0x8B]) + b"lottie-payload"

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

    async def download_media(self, document, file=None, thumb=None):
        self.called = True
        return self.payload


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
async def test_an_unadvertised_oversized_effect_is_caught_after_the_transfer():
    """Telethon cannot stream a VideoSize, so the delivered bytes are re-checked."""
    from telegram_mcp.tools.inspection import _premium_effect_frames

    message = _effect_message(effect_size=None)
    client = _EffectClient(payload=b"x" * 5000)

    result = await _premium_effect_frames(
        client, message, _EFFECT_DETAILS, count=2, max_dimension=64, max_bytes=1000
    )

    assert isinstance(result, str) and "turned out to be 5000 bytes" in result


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
