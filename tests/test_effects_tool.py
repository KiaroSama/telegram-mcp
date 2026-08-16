"""Tool-level tests for get_message_effect.

Helper-level tests cannot see the parts that only exist in the tool: the forced
refresh for an unknown ID, the one-shot retry after a file reference expires, and
the icon fallback. Everything here goes through the tool itself.
"""

import json

import pytest
from telethon.errors import FileReferenceExpiredError

from telegram_mcp import effect_catalog
import telegram_mcp.tools.effects as effects_tool
from telegram_mcp.tools.effects import get_message_effect


class _VideoSize:
    def __init__(self, size=60697):
        self.type, self.size, self.w, self.h = "f", size, 512, 512


class _Doc:
    def __init__(
        self, id_, mime="application/x-tgsticker", size=1000, video_thumbs=None, ref=b"r1"
    ):
        self.id, self.mime_type, self.size = id_, mime, size
        self.access_hash, self.file_reference = 7, ref
        self.video_thumbs = video_thumbs or []
        self.attributes = []


class _Effect:
    def __init__(self, id_, sticker_id, emoticon="👍", premium=False, icon=None, animation=None):
        self.id, self.effect_sticker_id, self.emoticon = id_, sticker_id, emoticon
        self.premium_required, self.static_icon_id = premium, icon
        self.effect_animation_id = animation


def _documents(ref=b"r1"):
    return [
        _Doc(10, mime="image/webp", size=1462, ref=ref),
        _Doc(20, size=25835, ref=ref),
        _Doc(21, size=61628, ref=ref),
        _Doc(30, size=14660, video_thumbs=[_VideoSize()], ref=ref),
    ]


def _effects(include_new=False):
    items = [_Effect(1, 20, icon=10, animation=21), _Effect(2, 30, premium=True, icon=10)]
    if include_new:
        items.append(_Effect(3, 20, emoticon="🎉", icon=10, animation=21))
    items.append(_Effect(4, 20))  # no static_icon_id: the emoticon is the icon
    return items


class _ToolClient:
    """A catalogue source and a byte source, with controllable staleness."""

    def __init__(self, *, new_effect_after_refresh=False, stale_refs=0, payload_size=2048):
        self.catalogue_calls = []
        self.downloads = 0
        self.stale_refs = stale_refs  # how many downloads raise before succeeding
        self.payload_size = payload_size
        self._new_after_refresh = new_effect_after_refresh
        self.current_hash = None
        self.payloads_sent = []

    async def __call__(self, request):
        self.catalogue_calls.append(request.hash)
        refreshed = len(self.catalogue_calls) > 1
        include_new = self._new_after_refresh and refreshed

        # A hash Telegram recognises means "nothing changed" and no payload at
        # all; that is the difference between a revalidation and a full download.
        if request.hash == self.current_hash and not include_new:

            class NotModified:
                pass

            self.payloads_sent.append(False)
            return NotModified()

        self.current_hash = 2 if refreshed else 1
        self.payloads_sent.append(True)

        class Available:
            hash = 2 if refreshed else 1
            effects = _effects(include_new)
            # A fresh payload carries fresh references; that is the whole point.
            documents = _documents(b"r2" if refreshed else b"r1")

        return Available()

    def iter_download(self, target):
        self.downloads += 1
        client = self

        class Chunks:
            def __init__(self):
                self.sent = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if client.stale_refs > 0:
                    client.stale_refs -= 1
                    raise FileReferenceExpiredError(request=None)
                if self.sent >= client.payload_size:
                    raise StopAsyncIteration
                chunk = b"\x1f\x8b" + b"x" * 510
                self.sent += len(chunk)
                return chunk

            async def close(self):
                pass

        return Chunks()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    effect_catalog._reset_catalog()

    async def _no_connect(cl):
        return None

    async def _to_thread(fn, *args):
        return [{"frame_index": 0}], ["image"]

    monkeypatch.setattr(effects_tool, "ensure_connected", _no_connect)
    monkeypatch.setattr(effects_tool.asyncio, "to_thread", _to_thread)
    yield
    effect_catalog._reset_catalog()


def _use(monkeypatch, client, **by_account):
    clients = {"default": client, **by_account}
    monkeypatch.setattr(
        effects_tool, "get_client", lambda account=None: clients[account or "default"]
    )
    return client


async def _call(effect_id, account="default", **kwargs):
    return await get_message_effect(effect_id, account=account, **kwargs)


def _payload(result):
    assert not isinstance(result, str), f"expected a tool payload, got: {result}"
    return json.loads(result[0])


# --- revalidation cadence ---------------------------------------------------


def test_the_catalogue_is_not_revalidated_before_an_hour():
    """Telegram asks for hourly at most; the unknown-ID path is the exception."""
    assert effect_catalog._REVALIDATE_AFTER_SECONDS >= 3600


@pytest.mark.asyncio
async def test_repeated_calls_inside_the_window_fetch_the_catalogue_once(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    for _ in range(3):
        await _call(1)
    assert client.catalogue_calls == [0]


# --- an unknown ID is the documented exception ------------------------------


@pytest.mark.asyncio
async def test_an_unknown_id_forces_a_refresh_and_then_resolves(monkeypatch):
    """A fresh cache lacking the ID usually means a NEW effect, not a retired one."""
    client = _use(monkeypatch, _ToolClient(new_effect_after_refresh=True))
    await _call(1)  # warm the cache; effect 3 is not in it
    assert client.catalogue_calls == [0]

    payload = _payload(await _call(3))

    assert client.catalogue_calls == [0, 1], (
        "the unknown ID must bypass the freshness window, and do it with the stored hash "
        "rather than buying the whole catalogue again"
    )
    assert payload["results"][0]["effect_id"] == 3
    assert payload["results"][0]["emoticon"] == "🎉"


@pytest.mark.asyncio
async def test_an_id_still_unknown_after_the_refresh_reports_not_found_once(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    result = await _call(999999)

    assert isinstance(result, str)
    assert "not in Telegram's current effect catalogue" in result
    assert "checked against Telegram for this lookup" in result
    # Cold cache: the very first request already returned the newest catalogue,
    # so a second one could only learn the same thing.
    assert client.catalogue_calls == [0], "the cold fetch was repeated to answer one lookup"


# --- expired file references ------------------------------------------------


@pytest.mark.parametrize(
    "effect_id, asset, label",
    [
        (1, "animation", "effect_animation document"),
        (1, "icon", "static icon"),
        (1, "sticker", "preview sticker"),
        (2, "animation", "fallback premium-effect thumbnail"),
    ],
)
@pytest.mark.asyncio
async def test_a_stale_file_reference_refreshes_the_catalogue_and_retries(
    monkeypatch, effect_id, asset, label
):
    client = _use(monkeypatch, _ToolClient(stale_refs=1))
    payload = _payload(await _call(effect_id, asset=asset))

    assert client.catalogue_calls == [0, 0], f"{label}: the catalogue was not refetched"
    assert client.downloads == 2, f"{label}: the download was not retried"
    assert payload["asset"] == asset


@pytest.mark.asyncio
async def test_a_second_failure_is_not_retried_again(monkeypatch):
    """One refresh is the contract; a second failure is a real one."""
    client = _use(monkeypatch, _ToolClient(stale_refs=5))

    result = await _call(1, asset="animation")

    assert isinstance(result, str) and "Error resolving effect 1" in result
    assert client.downloads == 2, "the retry looped instead of giving up"
    assert len(client.catalogue_calls) == 2


@pytest.mark.asyncio
async def test_an_unrelated_rpc_error_is_not_treated_as_a_stale_reference(monkeypatch):
    class _Broken(_ToolClient):
        def iter_download(self, target):
            self.downloads += 1
            raise RuntimeError("connection reset")

    client = _use(monkeypatch, _Broken())
    result = await _call(1, asset="animation")

    assert isinstance(result, str) and "connection reset" in result
    assert client.downloads == 1, "an unrelated error triggered a reference refresh"
    assert len(client.catalogue_calls) == 1, "the catalogue was refetched for nothing"


@pytest.mark.asyncio
async def test_the_byte_cap_still_applies_on_the_retry(monkeypatch):
    """A refreshed reference must not become a way around max_bytes."""
    client = _use(monkeypatch, _ToolClient(stale_refs=1, payload_size=50_000))

    result = await _call(1, asset="animation", max_bytes=1024)

    assert isinstance(result, str) and "larger than the 1024-byte limit" in result
    assert client.downloads == 2, "the retry did happen"


# --- the icon fallback ------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_reports_the_emoticon_as_the_icon_when_there_is_no_icon(monkeypatch):
    _use(monkeypatch, _ToolClient())
    payload = _payload(await _call(4))

    info = payload["results"][0]
    assert info["icon_source"] == "emoticon"
    assert info["static_icon"] is None
    assert info["emoticon"] == "👍"


@pytest.mark.asyncio
async def test_metadata_reports_a_real_icon_document_as_the_icon(monkeypatch):
    _use(monkeypatch, _ToolClient())
    info = _payload(await _call(1))["results"][0]
    assert info["icon_source"] == "static_icon"
    assert info["static_icon"]["document_id"] == 10


@pytest.mark.asyncio
async def test_asking_for_an_absent_icon_never_substitutes_the_sticker(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    result = await _call(4, asset="icon")

    assert isinstance(result, str)
    assert "icon_source='emoticon'" in result
    assert "👍" in result
    assert client.downloads == 0, "the preview sticker was downloaded as a stand-in"


# --- the ladder still works -------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_downloads_nothing(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    payload = _payload(await _call(1))

    assert client.downloads == 0
    assert payload["results"][0]["animation_source"] == "effect_animation"


@pytest.mark.parametrize("asset", ["icon", "sticker", "animation"])
@pytest.mark.asyncio
async def test_each_download_rung_returns_an_image_marked_asset_only(monkeypatch, asset):
    _use(monkeypatch, _ToolClient())
    result = await _call(1, asset=asset)
    payload = _payload(result)

    assert len(result) == 2, "the image block is missing"
    assert payload["results"][0]["composite_fidelity"] == "asset-only"
    assert payload["results"][0]["source_asset"] == f"message_effect_{asset}"
    assert "get_telegram_frames" in payload["note"]


@pytest.mark.asyncio
async def test_the_fallback_rung_reports_which_route_it_took(monkeypatch):
    _use(monkeypatch, _ToolClient())
    payload = _payload(await _call(2, asset="animation"))
    assert payload["results"][0]["animation_source"] == "premium_effect_of_preview_sticker"


@pytest.mark.asyncio
async def test_an_unknown_asset_name_is_refused_before_any_call(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    result = await _call(1, asset="bogus")

    assert isinstance(result, str) and "asset must be one of" in result
    assert client.catalogue_calls == [], "a bad argument still cost a catalogue fetch"


# --- refresh semantics: revalidate vs. hard refresh -------------------------


@pytest.mark.asyncio
async def test_a_cold_cache_unknown_id_costs_exactly_one_full_download(monkeypatch):
    """The first fetch already returned the newest catalogue; asking again is waste."""
    client = _use(monkeypatch, _ToolClient())

    result = await _call(999999)

    assert isinstance(result, str) and "not in Telegram's current effect catalogue" in result
    assert client.catalogue_calls == [0], "the cold fetch was immediately repeated"
    assert client.payloads_sent == [True], "more than one full payload crossed the wire"


@pytest.mark.asyncio
async def test_an_unknown_id_in_a_warm_cache_revalidates_by_hash_not_by_hash_zero(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    await _call(1)  # warm the cache

    await _call(999999)

    assert client.catalogue_calls == [0, 1], "the revalidation sent hash=0, buying the catalogue"
    assert client.payloads_sent == [True, False], "a payload was downloaded to answer a lookup"


@pytest.mark.asyncio
async def test_repeating_a_still_unknown_id_stops_contacting_telegram(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    await _call(1)

    for _ in range(4):
        result = await _call(999999)
        assert isinstance(result, str)

    assert client.catalogue_calls == [0, 1], "every repeat of the same dead ID hit Telegram"


@pytest.mark.asyncio
async def test_a_changed_catalogue_can_still_surface_a_new_effect(monkeypatch):
    """The negative result must not outlive the snapshot it was learned from."""
    client = _use(monkeypatch, _ToolClient(new_effect_after_refresh=True))
    await _call(1)

    payload = _payload(await _call(3))

    assert payload["results"][0]["effect_id"] == 3
    assert client.payloads_sent == [True, True], "the changed catalogue was not delivered"


@pytest.mark.asyncio
async def test_concurrent_unknown_id_calls_collapse_into_one_revalidation(monkeypatch):
    import asyncio as _asyncio

    client = _use(monkeypatch, _ToolClient())
    await _call(1)

    await _asyncio.gather(*(_call(999999) for _ in range(5)))

    assert client.catalogue_calls == [0, 1], "each concurrent miss revalidated separately"


# --- per-account isolation --------------------------------------------------


@pytest.mark.asyncio
async def test_two_accounts_keep_separate_catalogues_and_references(monkeypatch):
    """A file reference authorises a download for the session that fetched it."""
    first = _ToolClient()
    second = _ToolClient()
    _use(monkeypatch, first, second=second)

    await _call(1, account="default")
    await _call(1, account="second")

    assert first.catalogue_calls == [0], "account 'second' consumed account 'default' cache"
    assert second.catalogue_calls == [0], "account 'second' never fetched its own catalogue"

    a = effect_catalog.cached_catalog("default")
    b = effect_catalog.cached_catalog("second")
    assert a is not b
    assert a.documents[21] is not b.documents[21], "both accounts share one Document object"


@pytest.mark.asyncio
async def test_one_accounts_hard_refresh_leaves_the_other_untouched(monkeypatch):
    first = _ToolClient()
    second = _ToolClient(stale_refs=1)
    _use(monkeypatch, first, second=second)

    await _call(1, account="default")
    before = effect_catalog.cached_catalog("default")

    await _call(1, asset="animation", account="second")  # forces a hard refresh

    assert effect_catalog.cached_catalog("default") is before, "account A's cache was replaced"
    assert first.catalogue_calls == [0], "account B's refresh reached account A's client"
    assert second.catalogue_calls == [0, 0], "the stale reference did not force hash=0"


# --- one hard refresh per stale generation ----------------------------------


@pytest.mark.asyncio
async def test_concurrent_stale_failures_buy_one_catalogue_between_them(monkeypatch):
    """Three downloads that fail on the SAME snapshot must share one refresh.

    The barrier is the whole test: without it the tasks run to completion one
    after another, the second legitimately holds the catalogue the first just
    fetched, and its refresh is correct rather than a stampede.
    """
    import asyncio as _asyncio

    started = _asyncio.Event()
    arrived = []

    class _Barrier(_ToolClient):
        def iter_download(self, target):
            self.downloads += 1
            client = self

            class Chunks:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if client.stale_refs > 0:
                        # Hold every first attempt until all three are here, so
                        # they are all still looking at generation 1.
                        arrived.append(1)
                        if len(arrived) >= 3:
                            started.set()
                        await started.wait()
                        client.stale_refs -= 1
                        raise FileReferenceExpiredError(request=None)
                    raise StopAsyncIteration

                async def close(self):
                    pass

            return Chunks()

    client = _use(monkeypatch, _Barrier(stale_refs=3))
    await _call(1)  # warm the cache so all three start on the same generation

    await _asyncio.gather(
        _call(1, asset="animation"), _call(1, asset="icon"), _call(1, asset="sticker")
    )

    hard = [h for h in client.catalogue_calls[1:] if h == 0]
    assert len(hard) == 1, f"{len(hard)} full catalogue downloads for one stale generation"


# --- unresolved references are not the emoticon fallback --------------------


class _BrokenCatalogueClient(_ToolClient):
    """Names documents it then fails to include — a catalogue inconsistency."""

    async def __call__(self, request):
        self.catalogue_calls.append(request.hash)
        self.payloads_sent.append(True)

        class Available:
            hash = 1
            effects = [_Effect(5, 20, icon=10, animation=21)]
            documents = []  # every reference dangles

        return Available()


@pytest.mark.asyncio
async def test_a_dangling_icon_reference_is_not_reported_as_the_emoticon_rule(monkeypatch):
    """static_icon_id is SET here, so Telegram never chose the emoticon."""
    _use(monkeypatch, _BrokenCatalogueClient())
    info = _payload(await _call(5))["results"][0]

    assert info["icon_source"] == "unresolved_reference"
    assert info["static_icon"]["unresolved"] is True
    assert info["static_icon"]["document_id"] == 10, "the referenced ID was thrown away"


@pytest.mark.asyncio
async def test_dangling_sticker_and_animation_references_keep_their_ids(monkeypatch):
    _use(monkeypatch, _BrokenCatalogueClient())
    info = _payload(await _call(5))["results"][0]

    assert info["preview_sticker"]["document_id"] == 20
    assert info["preview_sticker"]["unresolved"] is True
    assert info["effect_animation"]["document_id"] == 21
    assert (
        info["animation_source"] == "unresolved_reference"
    ), "a dangling animation silently fell back to the sticker's premium effect"


# --- one hash revalidation for many simultaneous unknown lookups ------------


@pytest.mark.asyncio
async def test_simultaneous_unknown_lookups_send_one_hash_request(monkeypatch):
    """End to end, a burst of lookups for one unknown ID costs one request.

    Three things combine to give that, and only one of them is this round's: the
    shared per-account lock, the negative result recorded on the snapshot, and the
    check epoch. The epoch's own contribution is pinned directly at the helper
    level in test_effects.py, because the tool cannot currently reach the
    interleaving it guards — see the note there.
    """
    import asyncio as _asyncio

    client = _use(monkeypatch, _ToolClient())
    await _call(1)  # warm the cache: one hash=0 fetch

    results = await _asyncio.gather(*(_call(999999) for _ in range(5)))

    hashed = [h for h in client.catalogue_calls if h != 0]
    assert len(hashed) == 1, f"{len(hashed)} hash revalidations for one unknown ID"
    assert all(isinstance(r, str) for r in results)


@pytest.mark.asyncio
async def test_a_completed_revalidation_does_not_advance_the_payload_generation(monkeypatch):
    """The epoch and the generation mean different things and must stay apart."""
    client = _use(monkeypatch, _ToolClient())
    await _call(1)
    before = effect_catalog.cached_catalog("default")
    generation, epoch = before.generation, before.checked_epoch

    await _call(999999)  # unknown -> a revalidation that changes nothing

    after = effect_catalog.cached_catalog("default")
    assert after.generation == generation, (
        "a not-modified answer advanced the payload generation, which is what a "
        "stale-reference refresh uses to decide the documents are current"
    )
    assert after.checked_epoch > epoch, "the completed check was not recorded"
    assert client.payloads_sent == [True, False]


# --- account identity must match the client the runtime picks ---------------


@pytest.fixture
def _two_accounts(monkeypatch):
    from telegram_mcp import runtime

    monkeypatch.setattr(runtime, "clients", {"alpha": object(), "beta": object()})
    return None


@pytest.fixture
def _one_account(monkeypatch):
    from telegram_mcp import runtime

    monkeypatch.setattr(runtime, "clients", {"alpha": object()})
    return None


@pytest.mark.asyncio
async def test_case_variants_of_a_label_share_one_cache(monkeypatch, _two_accounts):
    """get_client lowercases the label, so two spellings are one client."""
    client = _ToolClient()
    _use(monkeypatch, client, alpha=client, ALPHA=client)

    await _call(1, account="alpha")
    await _call(1, account="ALPHA")

    assert client.catalogue_calls == [0], "the second spelling built its own cache"
    assert effect_catalog.cached_catalog("ALPHA") is effect_catalog.cached_catalog("alpha")


@pytest.mark.asyncio
async def test_implicit_single_account_shares_the_cache_with_its_label(monkeypatch, _one_account):
    """With one account configured, get_client ignores the argument entirely."""
    client = _ToolClient()
    _use(monkeypatch, client, alpha=client)

    await _call(1, account=None)
    await _call(1, account="alpha")

    assert client.catalogue_calls == [0], "None and the real label built two caches"
    assert effect_catalog.cached_catalog(None) is effect_catalog.cached_catalog("alpha")


@pytest.mark.asyncio
async def test_genuinely_different_accounts_still_never_share_documents(
    monkeypatch, _two_accounts
):
    alpha, beta = _ToolClient(), _ToolClient()
    _use(monkeypatch, alpha, alpha=alpha, beta=beta)

    await _call(1, account="alpha")
    await _call(1, account="beta")

    a = effect_catalog.cached_catalog("alpha")
    b = effect_catalog.cached_catalog("beta")
    assert a is not b
    assert a.documents[21] is not b.documents[21], "one Document object served two sessions"


# --- unresolved references, per rung ---------------------------------------


@pytest.mark.asyncio
async def test_an_unresolved_icon_rung_names_the_document_not_the_emoticon_rule(monkeypatch):
    _use(monkeypatch, _BrokenCatalogueClient())
    result = await _call(5, asset="icon")

    assert isinstance(result, str)
    assert "names document 10 as its static icon" in result
    assert "emoticon" not in result, "a catalogue fault was reported as Telegram's icon rule"


@pytest.mark.asyncio
async def test_an_unresolved_sticker_rung_names_its_document(monkeypatch):
    _use(monkeypatch, _BrokenCatalogueClient())
    result = await _call(5, asset="sticker")

    assert isinstance(result, str)
    assert "names document 20 as its preview sticker" in result


@pytest.mark.asyncio
async def test_an_unresolved_animation_rung_names_the_animation_document(monkeypatch):
    _use(monkeypatch, _BrokenCatalogueClient())
    result = await _call(5, asset="animation")

    assert isinstance(result, str)
    assert "names document 21 as its effect animation" in result


class _MissingStickerClient(_ToolClient):
    """No effect_animation_id, and the sticker it would fall back to is absent."""

    async def __call__(self, request):
        self.catalogue_calls.append(request.hash)
        self.payloads_sent.append(True)

        class Available:
            hash = 1
            effects = [_Effect(6, 30, icon=10)]
            documents = [_Doc(10, mime="image/webp")]

        return Available()


@pytest.mark.asyncio
async def test_a_missing_fallback_sticker_is_not_called_a_missing_animation(monkeypatch):
    """There was no effect_animation_id, so blaming one would be a fabrication."""
    _use(monkeypatch, _MissingStickerClient())
    result = await _call(6, asset="animation")

    assert isinstance(result, str)
    assert "names document 30 as its preview sticker" in result
    assert "no effect_animation_id of its own" in result
    assert "effect animation" not in result.split("preview sticker")[0]


# --- a static asset is renderable on every rung -----------------------------


class _StaticAssetClient(_ToolClient):
    """Every asset of effect 7 is a static WebP, and the bytes are not gzip."""

    chunk_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 500

    async def __call__(self, request):
        self.catalogue_calls.append(request.hash)
        self.payloads_sent.append(True)

        class Available:
            hash = 1
            effects = [_Effect(7, 20, icon=10, animation=21)]
            documents = [
                _Doc(10, mime="image/webp", size=1462),
                _Doc(20, mime="image/webp", size=2048),
                _Doc(21, mime="image/webp", size=2048),
            ]

        return Available()

    def iter_download(self, target):
        self.downloads += 1
        client = self

        class Chunks:
            def __init__(self):
                self.sent = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent >= client.payload_size:
                    raise StopAsyncIteration
                self.sent += len(client.chunk_bytes)
                return client.chunk_bytes

            async def close(self):
                pass

        return Chunks()


class _GzipStaticAssetClient(_StaticAssetClient):
    """Static mime, gzip payload: the bytes are what decide, not the metadata."""

    chunk_bytes = b"\x1f\x8b" + b"x" * 510


def _records_encoder(monkeypatch):
    """Replace the to_thread stub with one that remembers which encoder it got."""
    used = []

    async def _record(fn, *args):
        used.append(fn.__name__)
        return [{"frame_index": 0}], ["image"]

    monkeypatch.setattr(effects_tool.asyncio, "to_thread", _record)
    return used


@pytest.mark.parametrize("asset", ["sticker", "animation"])
@pytest.mark.asyncio
async def test_a_static_preview_sticker_renders_as_one_image(monkeypatch, asset):
    """extract_frames refuses a still image, so choosing it by rung name kills the rung."""
    used = _records_encoder(monkeypatch)
    _use(monkeypatch, _StaticAssetClient())

    payload = _payload(await _call(7, asset=asset))

    assert used == ["_encode_one"], f"a static {asset} was sent to the frame extractor: {used}"
    assert payload["asset"] == asset
    assert payload["results"][0]["composite_fidelity"] == "asset-only"


@pytest.mark.asyncio
async def test_a_gzip_payload_is_still_decoded_as_lottie_whatever_the_mime_says(monkeypatch):
    """Bytes beat metadata: a mislabelled Lottie must not be called a still image."""
    used = _records_encoder(monkeypatch)
    _use(monkeypatch, _GzipStaticAssetClient())

    _payload(await _call(7, asset="sticker"))

    assert used == ["_encode_frames"], f"a gzipped Lottie was treated as static: {used}"


# --- the negative cache must die with the SNAPSHOT, not with the window ------


@pytest.mark.asyncio
async def test_a_recorded_miss_survives_a_not_modified_revalidation(monkeypatch):
    """A refreshed window is not the snapshot's death — the opposite, in fact.

    This test previously asserted that a miss is discarded whenever the freshness
    window is refreshed, on the premise that "the set is justified by dying with
    the snapshot; a refreshed window is that death". Measured, that premise was
    wrong twice over: a "not modified" answer means the snapshot LIVES, and the
    window restarted on the same object every time, so the set never held more
    than one ID and N lookups of N dead IDs cost N round trips.

    The memory justification is unchanged, and still holds — the ceiling now sits
    on the addition (`remember_unknown`) instead of on the one event that proves
    every recorded miss still valid.
    """
    client = _use(monkeypatch, _ToolClient())
    await _call(1)
    await _call(999999)

    catalog = effect_catalog.cached_catalog("default")
    assert 999999 in catalog.unknown_ids, "the miss was not recorded at all"

    catalog.fetched_at -= effect_catalog._REVALIDATE_AFTER_SECONDS + 1
    await _call(1)  # the window has expired: a not-modified revalidation

    assert (
        999999 in effect_catalog.cached_catalog("default").unknown_ids
    ), "a miss was discarded by the very answer that proved it still valid"

    calls_so_far = len(client.catalogue_calls)
    await _call(999999)
    assert (
        len(client.catalogue_calls) == calls_so_far
    ), "the surviving miss still cost a round trip, which is what it exists to save"


# --- the not-found message must describe the path that produced it ----------


@pytest.mark.asyncio
async def test_the_not_found_message_distinguishes_a_check_from_a_cached_miss(monkeypatch):
    """The cached-miss path contacts nobody, so it must not claim a check."""
    client = _use(monkeypatch, _ToolClient())
    await _call(1)  # warm the cache

    checked = await _call(999999)  # revalidates against Telegram
    cached = await _call(999999)  # answered locally

    assert "checked against Telegram for this lookup" in checked
    assert "checked against Telegram for this lookup" not in cached
    assert "no new check was made" in cached
    assert client.catalogue_calls == [0, 1], "the cached miss reached Telegram after all"
