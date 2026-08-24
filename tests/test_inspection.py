"""Tests for the telegram_mcp.tools.inspection MCP tools.

Covers the pieces that carry real logic and no network: the window-title/chat
comparison behind ``title_matches_chat``, the custom-emoji and premium-effect
previews, and the tool-level behaviour around them (nothing decodes on the event
loop, one bad item never sinks a batch, an optional extra never costs the
answer). The bounded-transfer helpers they call live in
``telegram_mcp/media_transfer.py`` and are tested in
``test_inspection_transfer.py``.
"""

from types import SimpleNamespace

import pytest

from helpers_inspection import _CountingClient, _Iter, _MEDIUM, _photo_with
from telegram_mcp.tools.inspection import _chat_names, _title_matches_chat


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

    def _fake_frames(raw, suffix, count, max_dimension, cancelled=None):
        # Verified against live Telegram data: the type="f" asset is a gzipped
        # Lottie, so asserting .webm here is what locked the bug in.
        assert suffix == ".tgs", f"the effect was decoded as {suffix}, not Lottie"
        return [{"frame_index": 0}], ["image"]

    from telegram_mcp import media_preview

    original = media_preview._encode_frames
    media_preview._encode_frames = _fake_frames
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
        media_preview._encode_frames = original

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
    from telegram_mcp import media_preview

    client = _EffectClient(payload=b"x" * 1000)

    def _fake(raw, suffix, count, max_dimension, cancelled=None):
        return [{"frame_index": 0}], ["image"]

    original = media_preview._encode_frames
    media_preview._encode_frames = _fake
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
        media_preview._encode_frames = original

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

    from telegram_mcp import media_preview
    from telegram_mcp.tools.inspection import _premium_effect_frames

    seen = {}

    def _fake(raw, suffix, count, max_dimension, cancelled=None):
        seen["suffix"] = suffix
        return [{"frame_index": 0}], ["image"]

    original = media_preview._encode_frames
    media_preview._encode_frames = _fake
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
        media_preview._encode_frames = original

    assert seen["suffix"] == ".tgs"
    assert json.loads(result[0])["results"][0]["asset_format"] == "lottie_tgs"


@pytest.mark.asyncio
async def test_a_non_gzip_effect_asset_still_falls_back_to_video():
    """Trust the bytes: a future format change must not be decoded as Lottie."""
    import json

    from telegram_mcp import media_preview
    from telegram_mcp.tools.inspection import _premium_effect_frames

    seen = {}

    def _fake(raw, suffix, count, max_dimension, cancelled=None):
        seen["suffix"] = suffix
        return [{"frame_index": 0}], ["image"]

    original = media_preview._encode_frames
    media_preview._encode_frames = _fake
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
        media_preview._encode_frames = original

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


@pytest.mark.asyncio
async def test_a_message_page_is_built_off_the_event_loop(monkeypatch):
    """inspect_messages builds up to 50 deep views, each a full character-by-character
    pass over the message text. Done inline, every other tool call on the server waits
    for the whole page."""
    import threading

    from telegram_mcp.tools import inspection

    build_threads = []

    def _deep(m, base, chat=None, link_domain=None):
        build_threads.append(threading.get_ident())
        return {"id": getattr(m, "id", 0)}

    class _Client:
        async def get_messages(self, entity, **kwargs):
            return [SimpleNamespace(id=index) for index in range(50)]

    async def _resolve(chat_id, client):
        return SimpleNamespace(title="Chat")

    monkeypatch.setattr(inspection, "get_client", lambda account=None: _Client())
    monkeypatch.setattr(inspection, "resolve_entity", _resolve)
    monkeypatch.setattr(inspection, "message_to_dict", lambda m: {})
    monkeypatch.setattr(inspection, "deep_message_dict", _deep)

    caller = threading.get_ident()
    result = await inspection.inspect_messages(1, limit=50, account="a")

    assert len(build_threads) == 50, f"built {len(build_threads)} of 50"
    assert caller not in build_threads, "the page was built on the event loop thread"
    assert '"id": 49' in result or '"id":49' in result


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

    async def _preview(client, document, count, max_dimension, max_bytes, ledger=None):
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

    async def _preview(client, document, count, max_dimension, max_bytes, ledger=None):
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
