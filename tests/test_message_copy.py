"""Copying a message without taking it apart, and rebuilding one that was.

A premium emoji is a document id pinned to a UTF-16 offset. Anything that
re-derives the text moves those offsets underneath it, so the emoji lands on the
wrong character and nothing errors. Two answers live here and they are not
interchangeable:

* ``copy_message`` never decomposes anything -- the SERVER duplicates the
  message -- so it is what copying should use.
* ``_rebuild_entities`` is for composing something NEW, and it refuses anything
  it cannot place exactly rather than placing it approximately.

No network: the client is a fake that records what it was handed.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import telethon
from telethon import types

from telegram_mcp.message_view import describe_entities
from telegram_mcp.text_fidelity import fidelity_text
from telegram_mcp.tools import chats as chats_mod
from telegram_mcp.tools import messages as messages_mod
from telegram_mcp.tools.messages import copy_message

# The rebuilder moved out of `scheduled.py` into `telegram_mcp.entities`, the
# write-side inverse of message_view.describe_entities: send_message,
# reply_to_message and edit_message all needed it and could not reach it
# there. Import from the module that OWNS it.
from telegram_mcp.entities import entity_classes as _entity_classes
from telegram_mcp.entities import rebuild_entities as _rebuild_entities

PARTY = chr(0x1F389)
EMOJI_ID = 5312345678901234567
SOON = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()


def _described(text, entities):
    """What ``inspect_message`` would publish for this message."""
    return describe_entities(SimpleNamespace(message=text, entities=entities))


# --- rebuilding entities the viewer described -------------------------------


def test_a_premium_emoji_survives_the_round_trip():
    text = "Hi " + PARTY + " there"
    raw = [types.MessageEntityCustomEmoji(offset=3, length=2, document_id=EMOJI_ID)]

    clean, _ = fidelity_text(text)
    rebuilt = _rebuild_entities(_described(text, raw), clean)

    assert not isinstance(rebuilt, str), rebuilt
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], types.MessageEntityCustomEmoji)
    assert rebuilt[0].document_id == EMOJI_ID
    assert (rebuilt[0].offset, rebuilt[0].length) == (3, 2)


def test_formatting_beside_a_premium_emoji_no_longer_refuses_the_whole_message():
    """The regression. The first version rebuilt three entity kinds and returned
    an error for any fourth, so one bold word next to a premium emoji made the
    entire send fail -- which is the commonest shape a real message has."""
    text = "Hi " + PARTY + " bold here and a link"
    raw = [
        types.MessageEntityCustomEmoji(offset=3, length=2, document_id=EMOJI_ID),
        types.MessageEntityBold(offset=6, length=4),
        types.MessageEntityTextUrl(offset=22, length=4, url="https://example.com/x"),
    ]

    clean, _ = fidelity_text(text)
    rebuilt = _rebuild_entities(_described(text, raw), clean)

    assert not isinstance(rebuilt, str), rebuilt
    assert [type(e) for e in rebuilt] == [type(e) for e in raw]
    assert [(e.offset, e.length) for e in rebuilt] == [(e.offset, e.length) for e in raw]
    assert rebuilt[2].url == "https://example.com/x"


def test_the_rebuild_knows_every_entity_kind_this_telethon_ships():
    """Derived from Telethon's types rather than a table, so a kind added
    upstream is covered without anyone remembering to add it."""
    classes = _entity_classes()

    for kind in ("bold", "italic", "spoiler", "pre", "text_url", "custom_emoji"):
        assert kind in classes, kind
    telethon_kinds = [n for n in dir(types) if n.startswith("MessageEntity")]
    assert len(classes) == len(telethon_kinds)


def test_an_offset_past_the_end_of_the_text_is_refused():
    refusal = _rebuild_entities([{"type": "bold", "offset": 40, "length": 4}], "short")

    assert isinstance(refusal, str)
    assert "UTF-16 units" in refusal


def test_a_raw_telegram_offset_is_refused_rather_than_placed():
    """``offset_is_raw`` marks an offset the viewer could NOT rebase onto the
    text it returned, so it indexes a different string entirely."""
    described = [{"type": "bold", "offset": 2, "length": 2, "offset_is_raw": True}]

    refusal = _rebuild_entities(described, "a longer piece of text")

    assert isinstance(refusal, str)
    assert "raw Telegram offset" in refusal


def test_a_value_the_viewer_cleaned_is_refused_because_it_is_not_the_original():
    described = [
        {
            "type": "text_url",
            "offset": 0,
            "length": 2,
            "url": "https://example.com/cleaned",
            "url_altered": True,
        }
    ]

    refusal = _rebuild_entities(described, "hello")

    assert isinstance(refusal, str)
    assert "url_altered" in refusal


def test_an_unknown_kind_is_named_rather_than_dropped():
    refusal = _rebuild_entities([{"type": "invented", "offset": 0, "length": 1}], "hello")

    assert isinstance(refusal, str)
    assert "invented" in refusal


# --- copying, where nothing is decomposed at all ----------------------------


class _CopyClient:
    """Records the forward it was asked for; answers album lookups."""

    def __init__(self, album=None, refuse=False):
        self.forwarded = None
        self._album = album or {}
        self._refuse = refuse

    async def get_messages(self, entity, ids=None):
        if isinstance(ids, list):
            return [self._album.get(i) for i in ids]
        return self._album.get(ids)

    async def forward_messages(self, to_peer, ids, from_peer, **kwargs):
        if self._refuse:
            raise telethon.errors.rpcerrorlist.ChatForwardsRestrictedError(request=None)
        self.forwarded = SimpleNamespace(to_peer=to_peer, ids=ids, from_peer=from_peer, **kwargs)
        return []


@pytest.fixture
def wire_copy(monkeypatch):
    def wire(client):
        monkeypatch.setattr(messages_mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(messages_mod, "ensure_connected", _ensure)
        monkeypatch.setattr(messages_mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_a_copy_drops_the_author_so_it_arrives_without_a_forward_header(wire_copy):
    """The whole point. ``drop_author`` is what makes the server COPY rather than
    forward, and the copy is what carries the premium emoji through untouched."""
    client = wire_copy(_CopyClient())

    await copy_message("@src", 11, "@dest", expand_album=False, account="a")

    assert client.forwarded is not None, "nothing was sent"
    assert client.forwarded.drop_author is True
    assert client.forwarded.schedule is None
    assert client.forwarded.drop_media_captions is False


@pytest.mark.asyncio
async def test_a_copy_can_be_scheduled_which_is_the_case_that_lost_the_emoji(wire_copy):
    client = wire_copy(_CopyClient())

    await copy_message("@src", 11, "@dest", when=SOON, expand_album=False, account="a")

    assert client.forwarded.schedule is not None
    assert client.forwarded.schedule.tzinfo is not None
    assert client.forwarded.drop_author is True


@pytest.mark.asyncio
async def test_a_copy_scheduled_in_the_past_sends_nothing(wire_copy):
    client = wire_copy(_CopyClient())
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    answer = await copy_message("@src", 11, "@dest", when=past, account="a")

    assert "must be in the future" in answer
    assert client.forwarded is None, "a past schedule still reached the server"


@pytest.mark.asyncio
async def test_content_protection_is_reported_as_itself(wire_copy):
    """Worth naming: the obvious next move is to read the text and send it
    again, which loses the premium emoji AND does what the chat forbade."""
    wire_copy(_CopyClient(refuse=True))

    answer = await copy_message("@src", 11, "@dest", expand_album=False, account="a")

    assert "content protection" in answer


@pytest.mark.asyncio
async def test_a_copy_expands_an_album_the_way_a_forward_does(wire_copy):
    """Shared helper, so an album cannot forward whole and copy as one photo."""
    album = {i: SimpleNamespace(id=i, grouped_id=99) for i in (10, 11, 12)}
    client = wire_copy(_CopyClient(album=album))

    await copy_message("@src", 11, "@dest", account="a")

    assert client.forwarded.ids == [10, 11, 12]


# --- reading slow mode back -------------------------------------------------


@pytest.fixture
def wire_full_chat(monkeypatch):
    def wire(full_chat):
        chat = SimpleNamespace(id=777, title="Group", username="grp")

        class _Client:
            async def __call__(self, _request):
                return SimpleNamespace(chats=[chat], full_chat=full_chat)

        client = _Client()
        monkeypatch.setattr(chats_mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(chats_mod, "ensure_connected", _ensure)
        monkeypatch.setattr(chats_mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_slow_mode_interval_can_be_read_back(wire_full_chat):
    """``toggle_slow_mode`` could set it and nothing could read it, so a caller
    could not check the interval it was about to change."""
    wire_full_chat(
        SimpleNamespace(
            about="",
            participants_count=5,
            linked_chat_id=None,
            slowmode_seconds=30,
            slowmode_next_send_date=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        )
    )

    record = json.loads(await chats_mod.get_full_chat("@grp", account="a"))

    assert record["slowmode_seconds"] == 30
    assert record["slowmode_next_send_date"].startswith("2026-09-01T12:00")


@pytest.mark.asyncio
async def test_a_chat_that_cannot_have_slow_mode_reports_no_key_at_all(wire_full_chat):
    """Absent and 0 are different facts: 0 means a supergroup with slow mode
    switched off, which a broadcast channel is not."""
    wire_full_chat(SimpleNamespace(about="", participants_count=5, linked_chat_id=None))

    record = json.loads(await chats_mod.get_full_chat("@chan", account="a"))

    assert "slowmode_seconds" not in record
    assert "slowmode_next_send_date" not in record


@pytest.mark.asyncio
async def test_slow_mode_configured_but_off_still_reports_zero(wire_full_chat):
    wire_full_chat(
        SimpleNamespace(about="", participants_count=5, linked_chat_id=None, slowmode_seconds=0)
    )

    record = json.loads(await chats_mod.get_full_chat("@grp", account="a"))

    assert record["slowmode_seconds"] == 0
    assert "slowmode_next_send_date" not in record
