"""Message-effect resolution and the bounded effect transfer.

The fixtures mirror what a live account actually returns (checked against
`messages.GetAvailableEffects`): one flat document list, most effects with no
animation of their own, and the fallback animation living as a `type="f"`
VideoSize on the preview sticker.
"""

import asyncio

import pytest

from telegram_mcp import effect_catalog
from telegram_mcp.effect_catalog import (
    Catalog,
    load_catalog,
    premium_effect_size,
    resolve_effect,
)
from telegram_mcp.tools.inspection import _download_thumb_capped, _stream_capped


class _VideoSize:
    def __init__(self, type_="f", size=60697, w=512, h=512):
        self.type, self.size, self.w, self.h = type_, size, w, h


class _Doc:
    def __init__(self, id_, mime="application/x-tgsticker", size=1000, video_thumbs=None):
        self.id, self.mime_type, self.size = id_, mime, size
        self.access_hash, self.file_reference = 7, b"ref"
        self.video_thumbs = video_thumbs or []
        self.attributes = []


class _Effect:
    def __init__(self, id_, sticker_id, emoticon="👍", premium=False, icon=None, animation=None):
        self.id, self.effect_sticker_id, self.emoticon = id_, sticker_id, emoticon
        self.premium_required, self.static_icon_id = premium, icon
        self.effect_animation_id = animation


def _catalog():
    """Two effects: one with its own animation, one that must fall back."""
    icon = _Doc(10, mime="image/webp", size=1462)
    own_sticker, own_anim = _Doc(20, size=25835), _Doc(21, size=61628)
    fallback_sticker = _Doc(30, size=14660, video_thumbs=[_VideoSize()])
    docs = {d.id: d for d in (icon, own_sticker, own_anim, fallback_sticker)}
    effects = {
        1: _Effect(1, 20, icon=10, animation=21),
        2: _Effect(2, 30, premium=True, icon=10),
    }
    return Catalog(999, effects, docs, fetched_at=0.0)


# --- resolution -------------------------------------------------------------


def test_effect_with_its_own_animation_reports_that_document():
    info = resolve_effect(_catalog(), 1)
    assert info["animation_source"] == "effect_animation"
    assert info["effect_animation"]["document_id"] == 21
    assert info["effect_animation"]["format"] == "lottie_tgs"
    assert info["static_icon"]["document_id"] == 10
    assert info["preview_sticker"]["document_id"] == 20
    assert info["premium_required"] is False


def test_missing_effect_animation_falls_back_to_the_stickers_premium_effect():
    """574 of 697 live effects take this path, so it is the normal one."""
    info = resolve_effect(_catalog(), 2)
    assert info["animation_source"] == "premium_effect_of_preview_sticker"
    # The fallback is a thumbnail of the sticker, not a document of its own.
    assert info["effect_animation"]["document_id"] == 30
    assert info["effect_animation"]["thumb_type"] == "f"
    assert info["effect_animation"]["size_bytes"] == 60697
    assert info["premium_required"] is True


def test_effect_with_neither_animation_nor_premium_effect_says_none():
    catalog = _catalog()
    catalog.documents[30].video_thumbs = []
    assert resolve_effect(catalog, 2)["animation_source"] == "none"


def test_unknown_effect_id_resolves_to_none():
    assert resolve_effect(_catalog(), 123456) is None


def test_premium_effect_size_ignores_ordinary_video_thumbs():
    doc = _Doc(40, video_thumbs=[_VideoSize(type_="v")])
    assert premium_effect_size(doc) is None


# --- catalogue caching ------------------------------------------------------


class _FakeClient:
    """Counts requests and honours the hash the caller sends back."""

    def __init__(self, catalog_hash=999):
        self.calls = []
        self._hash = catalog_hash

    async def __call__(self, request):
        self.calls.append(request.hash)
        if request.hash == self._hash:

            class NotModified:  # no `effects` attribute, exactly like Telegram's
                pass

            return NotModified()

        class Available:
            hash = self._hash
            effects = [_Effect(1, 20, icon=10, animation=21)]
            documents = [_Doc(10, mime="image/webp"), _Doc(20), _Doc(21)]

        return Available()


@pytest.fixture(autouse=True)
def _clear_catalog():
    effect_catalog._reset_catalog()
    yield
    effect_catalog._reset_catalog()


@pytest.mark.asyncio
async def test_catalogue_is_served_without_touching_telegram_again():
    cl = _FakeClient()
    for _ in range(3):
        await load_catalog(cl)
    assert cl.calls == [0], "inside the window a repeat call must not reach Telegram"


@pytest.mark.asyncio
async def test_stale_window_revalidates_with_the_stored_hash_and_keeps_the_payload():
    cl = _FakeClient()
    catalog, contacted = await load_catalog(cl)
    assert contacted is True, "the first load must reach Telegram"
    catalog.fetched_at -= effect_catalog._REVALIDATE_AFTER_SECONDS + 1

    again, _ = await load_catalog(cl)
    assert cl.calls == [0, 999], "revalidation must send the hash Telegram gave us"
    assert again is catalog, "not-modified means the cache is the only copy"
    assert again.effects, "the payload must survive a not-modified answer"


@pytest.mark.asyncio
async def test_concurrent_first_loads_fetch_the_catalogue_once():
    cl = _FakeClient()
    await asyncio.gather(*(load_catalog(cl) for _ in range(4)))
    assert cl.calls == [0]


@pytest.mark.asyncio
async def test_not_modified_against_an_unknown_hash_refetches_from_scratch():
    """Nothing to serve, and repeating the same hash would repeat the answer."""
    cl = _FakeClient()
    cl._hash = 0  # answers "unchanged" even to a cold hash=0 request

    class _Once(_FakeClient):
        async def __call__(self, request):
            self.calls.append(request.hash)
            if len(self.calls) == 1:

                class NotModified:
                    pass

                return NotModified()
            return await _FakeClient(999).__call__(request)

    cl = _Once()
    catalog, _ = await load_catalog(cl)
    assert cl.calls == [0, 0]
    assert catalog.effects


@pytest.mark.asyncio
async def test_a_doubled_not_modified_answer_does_not_cache_an_empty_catalogue():
    """Caching the empty fall-through would serve "0 effects" as authoritative for an hour."""

    class _AlwaysNotModified:
        def __init__(self):
            self.calls = []

        async def __call__(self, request):
            self.calls.append(request.hash)

            class NotModified:
                pass

            return NotModified()

    cl = _AlwaysNotModified()
    with pytest.raises(RuntimeError, match="not modified"):
        await load_catalog(cl)

    assert cl.calls == [0, 0], "the hash=0 retry never happened"
    assert effect_catalog.cached_catalog(None) is None, "an empty catalogue was cached as fresh"

    with pytest.raises(RuntimeError):
        await load_catalog(cl)
    assert cl.calls == [0, 0, 0, 0], "the next call served the empty catalogue instead of asking"


# --- the bounded effect transfer -------------------------------------------


class _CappedClient:
    """Yields 1 KiB chunks, and records the location it was asked to stream."""

    def __init__(self, total_bytes):
        self.total = total_bytes
        self.location = None
        self.delivered = 0

    def iter_download(self, target):
        self.location = target
        client = self

        class Chunks:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if client.delivered >= client.total:
                    raise StopAsyncIteration
                chunk = b"x" * min(1024, client.total - client.delivered)
                client.delivered += len(chunk)
                return chunk

            async def close(self):
                pass

        return Chunks()


@pytest.mark.asyncio
async def test_oversized_effect_is_stopped_during_transfer_not_after():
    """The whole point: an unknown-size asset must never be buffered in full."""
    cl = _CappedClient(total_bytes=10 * 1024 * 1024)
    doc = _Doc(30, video_thumbs=[_VideoSize(size=None)])

    raw, over_cap = await _download_thumb_capped(cl, doc, doc.video_thumbs[0], 4096)

    assert over_cap is True and raw is None
    assert cl.delivered <= 4096 + 1024, (
        f"transfer continued to {cl.delivered} bytes; it must abort at the cap, "
        "not download 10 MB and measure it afterwards"
    )


@pytest.mark.asyncio
async def test_thumb_transfer_targets_the_video_size_by_type():
    cl = _CappedClient(total_bytes=512)
    doc = _Doc(30, video_thumbs=[_VideoSize()])

    raw, over_cap = await _download_thumb_capped(cl, doc, doc.video_thumbs[0], 4096)

    assert (raw, over_cap) == (b"x" * 512, False)
    assert cl.location.thumb_size == "f", "the effect asset is selected by thumb type"
    assert (cl.location.id, cl.location.file_reference) == (30, b"ref")


@pytest.mark.asyncio
async def test_transfer_of_exactly_the_limit_is_accepted():
    cl = _CappedClient(total_bytes=4096)
    doc = _Doc(30, video_thumbs=[_VideoSize()])

    raw, over_cap = await _download_thumb_capped(cl, doc, doc.video_thumbs[0], 4096)

    assert over_cap is False and len(raw) == 4096


@pytest.mark.asyncio
async def test_one_byte_over_the_limit_is_refused():
    cl = _CappedClient(total_bytes=4097)
    doc = _Doc(30, video_thumbs=[_VideoSize()])

    _, over_cap = await _download_thumb_capped(cl, doc, doc.video_thumbs[0], 4096)

    assert over_cap is True


@pytest.mark.asyncio
async def test_plain_document_stream_is_capped_too():
    cl = _CappedClient(total_bytes=8192)
    _, over_cap = await _stream_capped(cl, _Doc(21), 2048)
    assert over_cap is True
    assert cl.delivered <= 2048 + 1024


# --- the two effects must never be conflated -------------------------------


def test_a_message_effect_and_a_premium_sticker_effect_stay_separate():
    """One message can carry both; reporting either as the other is a lie."""
    from types import SimpleNamespace

    from telegram_mcp.message_view import describe_media, describe_message_effect

    sticker = SimpleNamespace(
        id=7,
        attributes=[],
        thumbs=[],
        video_thumbs=[SimpleNamespace(type="f", w=512, h=512, size=4096)],
    )
    msg = SimpleNamespace(
        id=5,
        media=SimpleNamespace(document=sticker),
        document=sticker,
        sticker=sticker,
        file=None,
        effect=5107584321108051014,
    )

    message_effect = describe_message_effect(msg)
    sticker_effect = (describe_media(msg) or {}).get("premium_effect")

    assert message_effect["kind"] == "message_effect"
    assert message_effect["effect_id"] == 5107584321108051014
    assert sticker_effect["kind"] == "premium_sticker_effect"
    # The sticker's effect has no ID of its own - it is an asset on the document -
    # so an ID appearing on one side must never be attributed to the other.
    assert "effect_id" not in sticker_effect
    assert message_effect["effect_id"] != sticker_effect.get("bytes")
    for note in (message_effect["note"], sticker_effect["note"]):
        assert "get_telegram_frames" in note


def test_each_effect_names_its_own_preview_route():
    from types import SimpleNamespace

    from telegram_mcp.message_view import _describe_premium_effect, describe_message_effect

    sticker_effect = _describe_premium_effect(
        SimpleNamespace(video_thumbs=[SimpleNamespace(type="f", w=1, h=1, size=1)])
    )
    message_effect = describe_message_effect(SimpleNamespace(effect=1))

    assert "get_media_frames(premium_effect=True)" in sticker_effect["note"]
    assert "no preview here" not in sticker_effect["note"], "stale claim resurfaced"
    assert "get_message_effect" in message_effect["note"]


# --- base install, without the optional renderer ----------------------------


@pytest.mark.asyncio
async def test_without_the_lottie_renderer_the_effect_names_the_extra():
    """The base install must explain itself, not fail with a decoder error."""
    from telegram_mcp.visual.frames import FrameExtractionError, extract_frames
    import telegram_mcp.visual.frames as frames

    original = frames.lottie_available
    frames.lottie_available = lambda: False
    try:
        with pytest.raises(FrameExtractionError) as excinfo:
            extract_frames(bytes([0x1F, 0x8B]) + b"payload", ".tgs", 2)
    finally:
        frames.lottie_available = original

    message = str(excinfo.value)
    assert "telegram-mcp[lottie]" in message
    assert "get_telegram_frames" in message


# --- the check epoch, tested where it actually lives ------------------------


class _SlowNotModified:
    """Answers "not modified" after a real suspension, like a round trip."""

    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request.hash)
        await asyncio.sleep(0.01)

        class NotModified:
            pass

        return NotModified()


def _warm_state():
    """A cached snapshot, as if one fetch had already happened."""
    import time

    state = effect_catalog._state(None)
    state.catalog = Catalog(7, {}, {}, time.monotonic(), 1)
    state.generation, state.check_epoch = 1, 1
    state.catalog.checked_epoch = 1
    return state.catalog


@pytest.mark.asyncio
async def test_simultaneous_revalidations_send_one_request():
    """A completed check must satisfy the callers that waited behind it.

    Telegram answers a revalidation with "not modified" and no payload, and the
    generation deliberately does not move for that — it means "these documents
    are current", which the stale-reference refresh depends on. So the generation
    cannot also serve as "somebody already asked": three callers holding one
    snapshot would each find it unchanged and ask again. That is what the epoch is
    for, and the assertion below is 3 without it (see the companion test).
    """
    seen = _warm_state()
    cl = _SlowNotModified()

    await asyncio.gather(
        *(effect_catalog.revalidate_catalog(cl, None, seen, seen.checked_epoch) for _ in range(3))
    )

    assert cl.calls == [7], f"{len(cl.calls)} identical revalidations reached Telegram"


@pytest.mark.asyncio
async def test_deduplicating_on_the_generation_alone_would_not_work():
    """Pins why the epoch exists, by running the rule it replaced.

    Without this the epoch looks like defensive noise: the tool cannot currently
    reach the interleaving, because load_catalog and revalidate_catalog share one
    lock and nothing awaits between them. That is an accident of the current call
    sequence, not a property anyone stated, and one added await reopens it.
    """
    import time

    async def generation_only(cl, account, seen, seen_epoch):
        state = effect_catalog._state(account)
        async with state.lock:
            if state.catalog is not None and state.catalog.generation > seen.generation:
                return state.catalog
            return await effect_catalog._fetch(state, cl, state.catalog.hash, time.monotonic())

    seen = _warm_state()
    cl = _SlowNotModified()

    await asyncio.gather(*(generation_only(cl, None, seen, seen.checked_epoch) for _ in range(3)))

    assert cl.calls == [7, 7, 7], "the rule this replaced would have deduplicated after all"


@pytest.mark.asyncio
async def test_a_not_modified_answer_moves_the_epoch_but_not_the_generation():
    seen = _warm_state()
    cl = _SlowNotModified()

    result = await effect_catalog.revalidate_catalog(cl, None, seen, seen.checked_epoch)

    assert result is seen, "the payload was replaced by an answer that carried none"
    assert result.generation == 1, "a not-modified answer advanced the payload generation"
    assert result.checked_epoch == 2, "the completed check was not recorded"


# --- account identity -------------------------------------------------------


def test_account_key_mirrors_get_client(monkeypatch):
    from telegram_mcp import runtime

    monkeypatch.setattr(runtime, "clients", {"alpha": object(), "beta": object()})
    assert effect_catalog.account_key("ALPHA") == "alpha"
    assert effect_catalog.account_key("alpha") == "alpha"

    monkeypatch.setattr(runtime, "clients", {"alpha": object()})
    # One account: get_client ignores the argument, so the cache must too.
    assert effect_catalog.account_key(None) == "alpha"
    assert effect_catalog.account_key("ALPHA") == "alpha"


# --- one home for the format sniff -----------------------------------------


def test_gzip_bytes_win_over_the_advertised_format():
    """The premium effect was advertised as one thing and arrived as another;
    that is why the bytes decide, not the mime table."""
    from telegram_mcp.effect_catalog import sniff_asset_format

    gzip_bytes = bytes((0x1F, 0x8B, 0x08, 0x00))
    assert sniff_asset_format(gzip_bytes, "video") == (".tgs", "lottie_tgs")
    assert sniff_asset_format(gzip_bytes, "static_image") == (".tgs", "lottie_tgs")


def test_a_non_gzip_asset_falls_back_to_its_advertised_format():
    from telegram_mcp.effect_catalog import sniff_asset_format

    assert sniff_asset_format(b"RIFF....WEBP", "static_image") == (".webp", "static_image")
    assert sniff_asset_format(bytes((0x1A, 0x45, 0xDF, 0xA3)), "video") == (".webm", "video")
    # Unknown means "treat it as the container ffmpeg can probe", not "guess Lottie".
    assert sniff_asset_format(b"\x00\x01\x02\x03") == (".webm", "video")


def test_both_tool_modules_share_one_sniff():
    """Two copies existed, each guarded by only one suite, so one could regress green."""
    import inspect as _inspect

    from telegram_mcp.tools import effects as effects_module
    from telegram_mcp.tools import inspection as inspection_module

    for module in (effects_module, inspection_module):
        source = _inspect.getsource(module)
        assert "sniff_asset_format" in source, f"{module.__name__} does not use the shared sniff"
        assert "x1f" not in source.lower().replace(
            "sniff_asset_format", ""
        ), f"{module.__name__} still carries its own gzip magic check"


# --- the negative cache must outlive the check that validates it -------------


class _NotModifiedClient:
    """Answers every request with AvailableEffectsNotModified (no `effects`)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        return _Namespace()


class _Namespace:
    pass


def _catalog_with_misses(*ids):
    effect_catalog._reset_catalog()
    state = effect_catalog._state("default")
    state.catalog = Catalog(123, {}, {}, 1.0, 1)
    state.catalog.checked_epoch = 0
    for effect_id in ids:
        state.catalog.remember_unknown(effect_id)
    return state


def test_a_not_modified_answer_keeps_the_recorded_misses():
    """ "Not modified" PROVES the content behind every miss is unchanged.

    Clearing the set there made it hold at most one ID — the freshness window
    restarted on the same object each time — so N lookups of N dead IDs cost N
    round trips, which is exactly what the cache exists to prevent.
    """
    state = _catalog_with_misses(901, 902, 903)

    fresh = asyncio.run(
        effect_catalog.revalidate_catalog(_NotModifiedClient(), "default", state.catalog, 0)
    )

    assert fresh.unknown_ids == {901, 902, 903}


def test_a_miss_does_not_survive_a_catalogue_that_actually_changed():
    """The negative result rides on the snapshot; a new payload is a new snapshot."""
    state = _catalog_with_misses(901)
    state.catalog.fetched_at = 0.0  # force a real fetch rather than a cached serve

    class _NewPayload:
        async def __call__(self, request):
            return type("Result", (), {"hash": 999, "effects": [], "documents": []})()

    fresh = asyncio.run(effect_catalog.refresh_catalog(_NewPayload(), "default", state.catalog))

    assert fresh.unknown_ids == set(), "a stale miss survived a changed catalogue"


def test_the_negative_cache_is_bounded_where_ids_are_added():
    """The ID comes from tool input, so the set needs a ceiling — but on the
    addition, not on the revalidation that confirms the snapshot."""
    catalog = Catalog(1, {}, {}, 1.0, 1)

    for effect_id in range(effect_catalog.MAX_UNKNOWN_IDS + 5):
        catalog.remember_unknown(effect_id)

    assert len(catalog.unknown_ids) <= effect_catalog.MAX_UNKNOWN_IDS
