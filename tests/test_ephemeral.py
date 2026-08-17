"""Self-destructing media: finding it, sending it, and keeping what arrived.

The interesting rules here are all about honesty — a timer belongs to the sender,
a viewed message is gone from the server, and audio cannot come back as a picture
— so most of these assert what the caller is TOLD, not just what is returned.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import ephemeral as mod
from telegram_mcp.tools.ephemeral import (
    VIEW_ONCE,
    _describe_ttl,
    _ttl_of,
    list_disappearing_media,
    save_disappearing_media,
    send_disappearing_media,
)


def _msg(message_id=5, ttl=30, kind="photo", caption="", out=False):
    media = SimpleNamespace(ttl_seconds=ttl) if ttl is not None else None
    return SimpleNamespace(
        id=message_id,
        out=out,
        media=media,
        message=caption,
        date=None,
        sender=SimpleNamespace(first_name="Ada", title=None),
    )


class _Client:
    def __init__(self, messages=(), payload=b"\xff\xd8\xff\xe0jpegbytes"):
        self.messages = list(messages)
        self.payload = payload
        self.downloads = 0

    async def get_messages(self, entity, ids=None):
        return next((m for m in self.messages if m.id == ids), None)

    def iter_messages(self, entity, limit=None):
        messages = self.messages

        async def gen():
            for m in messages[:limit]:
                yield m

        return gen()

    def iter_download(self, target):
        self.downloads += 1
        payload = self.payload

        class _Iter:
            def __aiter__(self):
                async def inner():
                    yield payload

                return inner()

            async def close(self):
                return None

        return _Iter()


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, kind="photo", encoded=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        monkeypatch.setattr(mod, "describe_media", lambda m: {"kind": kind, "extension": ".jpg"})

        async def _to_thread(fn, *args):
            return encoded if encoded is not None else ([{"width": 10}], ["<image>"])

        monkeypatch.setattr(mod.asyncio, "to_thread", _to_thread)
        return client

    return wire


# --- reading the timer ------------------------------------------------------


def test_ordinary_media_has_no_timer():
    assert _ttl_of(SimpleNamespace(media=SimpleNamespace())) is None
    assert _ttl_of(SimpleNamespace(media=None)) is None


def test_the_view_once_sentinel_is_reported_as_such():
    """Telegram encodes "destroyed after one view" as the maximum int, not as a duration."""
    described = _describe_ttl(_msg(ttl=VIEW_ONCE))
    assert described["view_once"] is True
    assert described["ttl_seconds"] == VIEW_ONCE

    timed = _describe_ttl(_msg(ttl=30))
    assert timed["view_once"] is False


def test_a_sender_name_is_cleaned_like_every_other_display_name():
    hostile = _msg()
    hostile.sender = SimpleNamespace(first_name="Ada‮gpj.exe", title=None)

    assert "‮" not in _describe_ttl(hostile)["sender"]


# --- finding them -----------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_returns_only_the_messages_that_expire(_wire):
    _wire(_Client(messages=[_msg(1, ttl=None), _msg(2, ttl=30), _msg(3, ttl=None)]))

    payload = json.loads(await list_disappearing_media(1, limit=10, account="a"))

    assert [r["message_id"] for r in payload["results"]] == [2]
    assert payload["found"] == 1


@pytest.mark.asyncio
async def test_listing_states_that_saving_defeats_the_senders_choice(_wire):
    """The note is the point of the tool, not decoration around it."""
    _wire(_Client(messages=[_msg(2, ttl=30)]))

    payload = json.loads(await list_disappearing_media(1, account="a"))

    assert "did not agree" in payload["note"]


@pytest.mark.asyncio
async def test_nothing_found_explains_that_viewed_media_is_already_gone(_wire):
    _wire(_Client(messages=[_msg(1, ttl=None)]))

    result = await list_disappearing_media(1, account="a")

    assert "No self-destructing media" in result
    assert "cannot be listed" in result


# --- keeping them -----------------------------------------------------------


@pytest.mark.asyncio
async def test_saving_returns_the_picture_and_the_byte_count(_wire):
    client = _wire(_Client(messages=[_msg(5, ttl=30, caption="hi")]))

    result = await save_disappearing_media(1, 5, account="a")

    assert isinstance(result, list), f"the media was not returned: {result!r}"
    payload = json.loads(result[0])
    record = payload["results"][0]
    assert record["saved_bytes"] > 0
    assert record["ttl_seconds"] == 30
    assert "did not agree" in payload["note"]
    assert client.downloads == 1


@pytest.mark.asyncio
async def test_a_message_without_a_timer_is_refused_with_the_right_alternative(_wire):
    _wire(_Client(messages=[_msg(5, ttl=None)]))

    result = await save_disappearing_media(1, 5, account="a")

    assert "no self-destruct timer" in result
    assert "get_media_frames" in result


@pytest.mark.asyncio
async def test_voice_says_audio_cannot_come_back_as_an_image(_wire):
    """A wrong promise here would look like a bug in the renderer instead of a limit."""
    _wire(_Client(messages=[_msg(5, ttl=30)]), kind="voice")

    payload = json.loads(await save_disappearing_media(1, 5, account="a"))

    error = payload["results"][0]["save_error"]
    assert "Audio cannot be returned as an image" in error
    assert "download_media" in error


@pytest.mark.asyncio
async def test_an_already_viewed_message_says_the_server_dropped_it(_wire):
    _wire(_Client(messages=[_msg(5, ttl=30)], payload=b""))

    payload = json.loads(await save_disappearing_media(1, 5, account="a"))

    assert "already been viewed" in payload["results"][0]["save_error"]


@pytest.mark.asyncio
async def test_over_cap_warns_that_the_countdown_did_not_pause(_wire):
    _wire(_Client(messages=[_msg(5, ttl=30)], payload=b"x" * 5000))

    payload = json.loads(await save_disappearing_media(1, 5, max_bytes=10, account="a"))

    error = payload["results"][0]["save_error"]
    assert "larger than the 10-byte limit" in error
    assert "countdown is not paused" in error


# --- sending them -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sending_reuses_the_shared_path_gate_rather_than_widening_it(monkeypatch):
    """The file-path surface is upstream's, gated by allowed roots; this must not bypass it."""
    seen = {}

    async def _resolve_path(*, raw_path, ctx, tool_name):
        seen["tool_name"] = tool_name
        return None, "disabled until allowed roots are configured."

    monkeypatch.setattr(mod, "_resolve_readable_file_path", _resolve_path)

    result = await send_disappearing_media(1, "x.jpg", 30, account="a")

    assert "allowed roots" in result
    assert seen["tool_name"] == "send_disappearing_media"


@pytest.mark.asyncio
async def test_zero_seconds_means_view_once_not_no_timer(monkeypatch):
    """0 must not become ttl_seconds=0, which would send ordinary media."""
    sent = {}

    async def _resolve_path(*, raw_path, ctx, tool_name):
        return "/tmp/x.jpg", None

    class _SendClient:
        async def send_file(self, entity, path, ttl=None, caption=None, **kw):
            sent["ttl"] = ttl
            return SimpleNamespace(id=9, media=SimpleNamespace(ttl_seconds=ttl))

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(mod, "_resolve_readable_file_path", _resolve_path)
    monkeypatch.setattr(mod, "get_client", lambda account=None: _SendClient())
    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _resolve)

    payload = json.loads(await send_disappearing_media(1, "x.jpg", 0, account="a"))

    assert sent["ttl"] == VIEW_ONCE
    assert payload["results"][0]["view_once"] is True
