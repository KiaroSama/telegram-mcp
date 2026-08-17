"""Self-destructing media: finding it, sending it, and keeping what arrived.

The interesting rules here are all about honesty — a timer belongs to the sender,
a viewed message is gone from the server, and audio cannot come back as a picture
— so most of these assert what the caller is TOLD, not just what is returned.
"""

import json
from pathlib import Path
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

        # Saving writes a real file, so the path gate is part of the unit under
        # test. Default here: no roots configured.
        async def _no_roots(*, raw_path, default_filename, ctx, tool_name):
            return None, f"{tool_name} is disabled until allowed roots are configured."

        monkeypatch.setattr(mod, "_resolve_writable_file_path", _no_roots)
        return client

    return wire


@pytest.fixture
def _with_roots(monkeypatch, tmp_path):
    """Point the writable-path gate at a real temp directory."""

    def wire():
        async def _resolve(*, raw_path, default_filename, ctx, tool_name):
            return (tmp_path / (raw_path or default_filename)), None

        async def _roots(ctx, tool_name):
            return [tmp_path], None

        monkeypatch.setattr(mod, "_resolve_writable_file_path", _resolve)
        monkeypatch.setattr(mod, "_ensure_allowed_roots", _roots)
        monkeypatch.setattr(mod, "_path_is_within_any_root", lambda path, roots: True)
        return tmp_path

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
async def test_saving_writes_a_real_file_and_fetches_only_once(_wire, _with_roots):
    """A disappearing message cannot be downloaded twice, so one fetch has to
    serve both the file on disk and the preview."""
    # _wire patches the path gate too, so roots must be wired AFTER it.
    client = _wire(_Client(messages=[_msg(5, ttl=30, caption="hi")]))
    root = _with_roots()

    result = await save_disappearing_media(1, 5, account="a")

    payload = json.loads(result[0])
    record = payload["results"][0]
    saved = Path(record["saved_path"])
    assert saved.is_file(), "nothing was written to disk"
    assert saved.read_bytes() == client.payload, "the saved file is not the media"
    assert record["saved_bytes"] == len(client.payload)
    assert root in saved.parents
    assert client.downloads == 1, "the media was fetched more than once"
    assert "did not agree" in payload["note"]


@pytest.mark.asyncio
async def test_voice_is_saved_even_though_it_cannot_be_previewed(_wire, _with_roots):
    """The first version refused audio outright; the file is the whole point."""
    _wire(_Client(messages=[_msg(5, ttl=30)]), kind="voice")
    _with_roots()

    payload = json.loads((await save_disappearing_media(1, 5, account="a"))[0])
    record = payload["results"][0]

    assert Path(record["saved_path"]).is_file()
    assert "cannot be returned as an image" in record["preview_error"]


@pytest.mark.asyncio
async def test_without_roots_nothing_is_written_but_the_preview_still_arrives(_wire):
    """The gate is upstream's and stays shut; losing the content too would be worse."""
    _wire(_Client(messages=[_msg(5, ttl=30)]))

    result = await save_disappearing_media(1, 5, account="a")

    payload = json.loads(result[0])
    record = payload["results"][0]
    assert "allowed roots" in record["save_error"]
    assert "Nothing was written to disk" in record["save_error"]
    assert "saved_path" not in record
    assert record["fetched_bytes"] > 0
    assert len(result) > 1, "the preview was dropped as well"


@pytest.mark.asyncio
async def test_a_message_without_a_timer_is_refused_with_the_right_alternative(_wire):
    _wire(_Client(messages=[_msg(5, ttl=None)]))

    result = await save_disappearing_media(1, 5, account="a")

    assert "no self-destruct timer" in result
    assert "get_media_frames" in result


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


def test_the_ttl_ceiling_is_the_measured_one():
    """1-60 come back set; 61, 90, 300 and 3600 come back with no timer at all."""
    from telegram_mcp.tools.ephemeral import MAX_TTL_SECONDS

    assert MAX_TTL_SECONDS == 60


def _sender(monkeypatch, media_ttl):
    """A client whose send_file reports whatever timer Telegram applied."""
    sent = {}

    async def _resolve_path(*, raw_path, ctx, tool_name):
        return "/tmp/x.jpg", None

    class _SendClient:
        async def send_file(self, entity, path, ttl=None, caption=None, **kw):
            sent["ttl"] = ttl
            applied = ttl if media_ttl == "echo" else media_ttl
            return SimpleNamespace(id=9, media=SimpleNamespace(ttl_seconds=applied))

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(mod, "_resolve_readable_file_path", _resolve_path)
    monkeypatch.setattr(mod, "get_client", lambda account=None: _SendClient())
    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _resolve)
    return sent


@pytest.mark.asyncio
async def test_a_timer_over_the_ceiling_is_refused_rather_than_sent_wrong(monkeypatch):
    """Telegram drops an out-of-range timer silently, so sending it would produce
    permanent media while the caller believed it was disappearing."""
    sent = _sender(monkeypatch, "echo")

    result = await send_disappearing_media(1, "x.jpg", 90, account="a")

    assert "must be 1-60" in result
    assert "silently drops it" in result
    assert "ttl" not in sent, "a message with a doomed timer was sent anyway"


@pytest.mark.asyncio
async def test_a_dropped_timer_is_reported_loudly(monkeypatch):
    """Defence in depth: the accepted range is Telegram's to change."""
    _sender(monkeypatch, None)

    payload = json.loads(await send_disappearing_media(1, "x.jpg", 30, account="a"))
    record = payload["results"][0]

    assert record["timer_dropped"] is True
    assert "NOT disappearing" in record["warning"]
    assert payload["timer_applied"] is False


@pytest.mark.asyncio
async def test_an_applied_timer_is_not_flagged(monkeypatch):
    _sender(monkeypatch, "echo")

    payload = json.loads(await send_disappearing_media(1, "x.jpg", 30, account="a"))

    assert "timer_dropped" not in payload["results"][0]
    assert payload["timer_applied"] is True
