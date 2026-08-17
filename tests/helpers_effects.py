"""Shared fakes for the get_message_effect tool tests.

The catalogue-lifecycle tests and the asset-ladder tests drive the same tool
through the same fake client, so the fakes, the cache-isolating fixture and the
call/payload shorthands live here rather than in either of them.
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
