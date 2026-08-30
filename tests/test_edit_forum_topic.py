"""Editing a forum topic, and the wrong turn taken on the way there.

`create_forum_topic` and `list_topics` existed with nothing that could change a
topic afterwards — no rename, no close, no reopen.

It was first reported unbuildable because `functions.channels` has no
`EditForumTopicRequest`. Then it was built as a hand-written wire encoder, its
constructor id derived by CRC32 from the `channels.editForumTopic` schema line —
a derivation that validated cleanly against `channels.getForumTopics`, which this
module was also hand-rolling.

Both were wrong the same way. Telethon 1.44 ships all of these under
**`functions.messages`**, and the `channels.*` forms are RETIRED:

    channels.getForumTopics  0x0DE560D1  channel:InputChannel   (retired)
    messages.getForumTopics  0x3BA47BFF  peer:InputPeer         (live)
    channels.editForumTopic  0xF4DFA185  channel:InputChannel   (retired)
    messages.editForumTopic  0xCECC1134  peer:InputPeer         (live)

Telegram still served the retired ids, so nothing looked broken. A CRC32
derivation proves the id matches the schema line it was handed — never that the
line is the one still in service. These tests pin the live requests so the module
cannot drift back onto a hand-rolled encoder.
"""

import inspect

import pytest
from telethon.tl import functions
from telethon.tl.types import Channel

from telegram_mcp.tools import topics as topics_mod


def _forum(forum=True):
    """A real Channel: the tool selects its branch with isinstance, so a stand-in
    class is refused before the forum check is ever reached."""
    return Channel(
        id=555,
        title="A Forum",
        photo=None,
        date=None,
        creator=True,
        left=False,
        broadcast=False,
        verified=False,
        megagroup=True,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        forum=forum,
    )


class Recorder:
    def __init__(self, answer=None):
        self.sent = []
        self.answer = answer

    async def __call__(self, request):
        self.sent.append(request)
        return self.answer


def _wire(monkeypatch, entity, answer=None):
    client = Recorder(answer=answer)

    async def _resolve(chat_id, cl=None, account=None):
        return entity

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(topics_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(topics_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(topics_mod, "ensure_connected", _connected)
    return client


# --- the live requests, not a hand-rolled copy ------------------------------


def test_the_module_no_longer_hand_rolls_any_request():
    """249 lines of wire encoding lived here, two of them addressing retired
    constructors. Telethon ships all three; a re-introduced encoder would be a
    silent return to that."""
    source = inspect.getsource(topics_mod)

    assert "TLRequest" not in source, "a hand-rolled request is back"
    assert "CONSTRUCTOR_ID" not in source
    assert "def _bytes" not in source


@pytest.mark.parametrize(
    "attr,expected_id",
    [
        ("GetForumTopicsRequest", 0x3BA47BFF),
        ("CreateForumTopicRequest", 0x2F98C3D5),
        ("EditForumTopicRequest", 0xCECC1134),
    ],
)
def test_the_live_requests_are_the_ones_telethon_ships(attr, expected_id):
    """Pinned by id: if a future Telethon moves these, it fails here rather than
    as an unexplained RPC error against a real forum."""
    assert getattr(functions.messages, attr).CONSTRUCTOR_ID == expected_id


@pytest.mark.parametrize(
    "attr", ["GetForumTopicsRequest", "CreateForumTopicRequest", "EditForumTopicRequest"]
)
def test_every_forum_request_addresses_a_peer_not_a_channel(attr):
    """The retired `channels.*` forms took an InputChannel. Handing one of those
    to the live request is the exact mistake this file records."""
    params = inspect.signature(getattr(functions.messages, attr).__init__).parameters

    assert "peer" in params
    assert "channel" not in params


# --- the tool ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_nothing_is_refused_before_anything_is_sent(monkeypatch):
    client = _wire(monkeypatch, _forum())

    result = await topics_mod.edit_forum_topic(-100555, 4)

    assert client.sent == []
    assert "Nothing to change" in result


@pytest.mark.asyncio
async def test_a_rename_reaches_the_live_request(monkeypatch):
    client = _wire(monkeypatch, _forum())

    await topics_mod.edit_forum_topic(-100555, 4, title="Planning")

    request = client.sent[-1]
    assert isinstance(request, functions.messages.EditForumTopicRequest)
    assert request.topic_id == 4
    assert request.title == "Planning"
    assert request.closed is None, "an untouched field must stay untouched"


@pytest.mark.asyncio
async def test_false_is_sent_and_is_not_the_same_as_omitted(monkeypatch):
    """`closed=False` REOPENS a topic. Treating it as "not given" would leave the
    tool unable to undo its own close."""
    client = _wire(monkeypatch, _forum())

    await topics_mod.edit_forum_topic(-100555, 4, closed=False)

    request = client.sent[-1]
    assert request.closed is False
    assert request.title is None


@pytest.mark.asyncio
async def test_a_non_forum_supergroup_is_refused(monkeypatch):
    client = _wire(monkeypatch, _forum(forum=False))

    result = await topics_mod.edit_forum_topic(-100555, 4, title="x")

    assert client.sent == []
    assert "forum topics enabled" in result
