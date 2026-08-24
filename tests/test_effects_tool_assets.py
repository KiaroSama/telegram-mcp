"""Tool-level tests for the asset ladder behind get_message_effect.

Everything here is about which asset a lookup resolves to and how it is
described: the icon/sticker/animation rungs, the emoticon rule, what a dangling
document reference reports, and which encoder a payload reaches. The catalogue
those assets come from is covered in test_effects_tool_catalogue.py.
"""

import pytest

from telegram_mcp import media_preview

import telegram_mcp.tools.effects as effects_tool
from helpers_effects import _Doc, _Effect, _ToolClient, _call, _payload, _use

# An autouse fixture is active in whichever module holds the name, so the import
# is the registration; nothing here calls it.
from helpers_effects import _isolated  # noqa: F401

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
    """Remember which encoder the tool chose for the payload it was handed.

    Two seams now, not one: the single-image path is still dispatched through
    `to_thread`, while frame extraction moved to run_in_executor so a cancelled
    decode can be waited on. Watching only the old dispatcher recorded an empty
    list for the frame path and the assertion read as 'treated as static' - a
    wrong diagnosis of a decoder choice that was actually correct.
    """
    used = []

    async def _record_still(fn, *args):
        used.append(fn.__name__)
        return [{"frame_index": 0}], ["image"]

    def _record_frames(raw, suffix, count, max_dimension, cancelled=None):
        used.append("_encode_frames")
        return [{"frame_index": 0}], ["image"]

    monkeypatch.setattr(effects_tool.asyncio, "to_thread", _record_still)
    monkeypatch.setattr(media_preview, "_encode_frames", _record_frames)
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
