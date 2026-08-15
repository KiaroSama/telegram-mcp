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
        self.last_max_bytes_target = None

    async def __call__(self, request):
        self.catalogue_calls.append(request.hash)
        refreshed = len(self.catalogue_calls) > 1
        include_new = self._new_after_refresh and refreshed

        class Available:
            hash = 1 if not refreshed else 2
            effects = _effects(include_new)
            # A refresh hands back fresh references; that is the whole point.
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


def _use(monkeypatch, client):
    monkeypatch.setattr(effects_tool, "get_client", lambda account=None: client)
    return client


async def _call(effect_id, **kwargs):
    return await get_message_effect(effect_id, account="default", **kwargs)


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

    assert client.catalogue_calls == [0, 0], "the unknown ID must bypass the freshness window"
    assert payload["results"][0]["effect_id"] == 3
    assert payload["results"][0]["emoticon"] == "🎉"


@pytest.mark.asyncio
async def test_an_id_still_unknown_after_the_refresh_reports_not_found_once(monkeypatch):
    client = _use(monkeypatch, _ToolClient())
    result = await _call(999999)

    assert isinstance(result, str)
    assert "not in Telegram's current effect catalogue" in result
    assert "after a forced refresh" in result
    assert len(client.catalogue_calls) == 2, "exactly one forced refresh, never a loop"


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
