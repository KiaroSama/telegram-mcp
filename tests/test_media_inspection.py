"""Media inspection: thumbnails, video frames, premium effects, custom emoji.

Split from ``test_inspection.py`` alongside the module it covers. The tests here
patch ``media_inspection``, NOT ``inspection``: a function resolves globals from
its OWN module, so a patch aimed at the module a function USED to live in keeps
succeeding against a name nothing reads. That is how the previous split in this
repository silently stopped testing four things.
"""

from types import SimpleNamespace

import pytest

from telegram_mcp import media_preview

from helpers_inspection import _CountingClient, _Iter

_EFFECT_DETAILS = {"premium_effect": {"kind": "premium_sticker_effect"}, "kind": "sticker"}


def _emoji_document(mime="image/webp", **attrs):
    """A stub custom-emoji document with a DocumentAttributeCustomEmoji."""
    attribute = SimpleNamespace(
        alt="\U0001f600", stickerset=SimpleNamespace(short_name="SetName", id=1), **attrs
    )
    return SimpleNamespace(id=42, mime_type=mime, size=1234, attributes=[attribute])


@pytest.mark.asyncio
async def test_custom_emoji_metadata_exposes_text_color_and_free():
    from telegram_mcp.tools.media_inspection import _custom_emoji_preview

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
    from telegram_mcp.tools.media_inspection import _custom_emoji_preview

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
    from telegram_mcp.tools import media_inspection as inspection

    doc = inspection.get_custom_emoji.__doc__ or ""
    assert "telegram-mcp[lottie]" in doc
    assert "text_color" in doc
    assert "nothing here rasterises" not in doc, "the old never-renders claim is still there"


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

    from telegram_mcp.tools.media_inspection import _premium_effect_frames

    msg = _effect_message()

    class _Client:
        def iter_download(self, location):
            assert (
                getattr(location, "thumb_size", None) == "f"
            ), "the effect asset was not requested"
            return _Iter([bytes([0x1F, 0x8B]) + b"lottie-payload"], [])

    def _fake_frames(raw, suffix, count, max_dimension, *_bounds):
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
    from telegram_mcp.tools.media_inspection import _premium_effect_frames

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
    from telegram_mcp.tools.media_inspection import _premium_effect_frames

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
    from telegram_mcp.tools.media_inspection import _premium_effect_frames

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
    from telegram_mcp.tools.media_inspection import MAX_FRAME_SOURCE_BYTES, _premium_effect_frames

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
    from telegram_mcp.tools.media_inspection import _premium_effect_frames
    from telegram_mcp import media_preview

    client = _EffectClient(payload=b"x" * 1000)

    def _fake(raw, suffix, count, max_dimension, *_bounds):
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

    from telegram_mcp.tools import media_inspection as inspection

    source = _inspect.getsource(inspection.get_media_frames)
    effect_branch = source.index("if premium_effect:")
    sticker_gate = source.index('size_bytes = details.get("size_bytes")')
    assert effect_branch < sticker_gate, "the sticker's own size still gates the effect"


@pytest.mark.asyncio
async def test_a_gzipped_effect_asset_is_decoded_as_lottie():
    """Live Telegram data: VideoSize type="f" carries a .tgs, not a WebM."""
    import json

    from telegram_mcp import media_preview
    from telegram_mcp.tools.media_inspection import _premium_effect_frames

    seen = {}

    def _fake(raw, suffix, count, max_dimension, *_bounds):
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
    from telegram_mcp.tools.media_inspection import _premium_effect_frames

    seen = {}

    def _fake(raw, suffix, count, max_dimension, *_bounds):
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

    from telegram_mcp.tools import media_inspection as inspection

    source = _inspect.getsource(inspection.get_media_frames)
    clamp = source.index("max_bytes = max(1, min(")
    branch = source.index("if premium_effect:")
    assert clamp < branch, "the effect path still receives the raw max_bytes"


@pytest.mark.asyncio
async def test_custom_emoji_refuses_a_document_over_the_cap(monkeypatch):
    from telegram_mcp.tools.media_inspection import _custom_emoji_preview

    monkeypatch.setattr(
        media_preview, "_encode_one", lambda *a, **k: ([{"frame_index": 0}], ["image"])
    )

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
    from telegram_mcp.tools.media_inspection import _custom_emoji_preview

    client = _CountingClient(total=10 * 1024 * 1024)
    document = SimpleNamespace(id=3, mime_type="image/webp", size=None, attributes=[])

    record, images = await _custom_emoji_preview(client, document, 1, 64, 4096)

    assert "advertised size was absent" in record["preview_error"]
    assert images == []
    assert client.delivered <= 4096 + 1024, f"{client.delivered} bytes crossed a 4096-byte cap"


@pytest.mark.asyncio
async def test_one_unresolvable_emoji_does_not_sink_the_other_nine(monkeypatch):
    """_custom_emoji_preview handles the two errors it expects and no others.

    Anything else — an RPC error, a reference still stale after the retry, a
    Pillow failure escaping the decoder — propagated out of a bare gather and
    sank all ten records, while the other coroutines were abandoned rather than
    cancelled.
    """
    import json

    from telegram_mcp.tools import media_inspection as inspection

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


@pytest.mark.asyncio
async def test_get_custom_emoji_never_exceeds_the_batch_budget(monkeypatch):
    """The gate is real: concurrent previews never outnumber the derived width."""
    import asyncio as _asyncio

    from telegram_mcp.media_transfer import MAX_BATCH_BYTES
    from telegram_mcp.tools import media_inspection as inspection

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


@pytest.mark.asyncio
async def test_a_custom_emoji_reports_the_pack_it_came_from(monkeypatch):
    """Which pack, by name and by link - not just an opaque set id.

    `_custom_emoji_preview` read `stickerset.id` off the document attribute and
    dropped the `access_hash` sitting beside it, then reported the bare id
    because "the short name costs a separate GetStickerSet call per set". The
    hash is what makes that call possible at all, and one call per DISTINCT set
    is cheap next to the image bytes this tool already moves.
    """
    import json

    from telegram_mcp.tools import media_inspection as inspection

    def _emoji(document_id, set_id, access_hash):
        return SimpleNamespace(
            id=document_id,
            mime_type="image/webp",
            size=10,
            attributes=[
                SimpleNamespace(
                    alt="😀",
                    stickerset=SimpleNamespace(id=set_id, access_hash=access_hash),
                )
            ],
        )

    # Two emoji from ONE set: the set must be resolved once, not twice.
    documents = [_emoji(1, 555, 99), _emoji(2, 555, 99)]
    resolved = []

    class _Client:
        async def __call__(self, request):
            name = type(request).__name__
            if name == "GetCustomEmojiDocumentsRequest":
                return documents
            resolved.append(request.stickerset.id)
            return SimpleNamespace(
                set=SimpleNamespace(
                    short_name="MyPack",
                    title="My Pack",
                    count=2,
                    animated=False,
                    videos=False,
                    emojis=True,
                    masks=False,
                ),
                documents=[],
            )

    async def _ensure(client):
        return None

    async def _preview(client, document, count, max_dimension, max_bytes, ledger=None):
        return {
            "document_id": document.id,
            "sticker_set_id": 555,
            "sticker_set_access_hash": 99,
        }, []

    monkeypatch.setattr(inspection, "get_client", lambda account=None: _Client())
    monkeypatch.setattr(inspection, "ensure_connected", _ensure)
    monkeypatch.setattr(inspection, "_custom_emoji_preview", _preview)

    records = json.loads((await inspection.get_custom_emoji([1, 2], account="a"))[0])["results"]

    assert resolved == [555], f"the shared set was resolved {len(resolved)} times"
    assert records[0]["sticker_set"] == "MyPack"
    assert records[0]["sticker_set_title"] == "My Pack"
    assert records[0]["sticker_set_link"] == "https://t.me/addemoji/MyPack"
    assert records[1]["sticker_set"] == "MyPack"


@pytest.mark.asyncio
async def test_an_unresolvable_pack_does_not_sink_the_emoji(monkeypatch):
    """The picture is the point; the pack name is a bonus. A set that cannot be
    resolved - deleted, or an access hash this account was never given - must
    leave the record intact and say so."""
    import json

    from telegram_mcp.tools import media_inspection as inspection

    document = SimpleNamespace(id=1, mime_type="image/webp", size=10, attributes=[])

    class _Client:
        async def __call__(self, request):
            if type(request).__name__ == "GetCustomEmojiDocumentsRequest":
                return [document]
            raise RuntimeError("STICKERSET_INVALID")

    async def _ensure(client):
        return None

    async def _preview(client, doc, count, max_dimension, max_bytes, ledger=None):
        return {"document_id": doc.id, "sticker_set_id": 7, "sticker_set_access_hash": 8}, []

    monkeypatch.setattr(inspection, "get_client", lambda account=None: _Client())
    monkeypatch.setattr(inspection, "ensure_connected", _ensure)
    monkeypatch.setattr(inspection, "_custom_emoji_preview", _preview)

    record = json.loads((await inspection.get_custom_emoji([1], account="a"))[0])["results"][0]

    assert record["document_id"] == 1
    assert record["sticker_set_id"] == 7
    assert "sticker_set" not in record
    assert "STICKERSET_INVALID" in record["sticker_set_error"]


@pytest.mark.asyncio
async def test_the_set_access_hash_is_kept_not_discarded():
    """The one-line half of the bridge, at the layer that reads the document."""
    from telegram_mcp.tools.media_inspection import _custom_emoji_preview

    document = SimpleNamespace(
        id=1,
        mime_type="",
        size=0,
        attributes=[SimpleNamespace(alt="😀", stickerset=SimpleNamespace(id=555, access_hash=99))],
    )

    record, _images = await _custom_emoji_preview(None, document, count=1, max_dimension=64)

    assert record["sticker_set_id"] == 555
    assert record["sticker_set_access_hash"] == 99
