"""Posting under a channel's identity instead of your own.

Telegram calls it "send as", and it is a per-message choice: an account that
administers several channels picks which one a message appears to come from, and
the wrong choice is published under the wrong name. Nothing in the answer text
distinguishes a request that carried the right identity from one that carried
none, so every test here asserts on the REQUEST — the same discipline
`test_writing_tools.py` was written for.

The reader is not a convenience. `send_as` takes an InputPeer, the set of legal
ones is decided by Telegram per chat, and there is no way to guess it: without
`list_send_as` a caller cannot supply a valid value at all.
"""

import pytest
from telethon.tl import functions, types

from telegram_mcp.tools import media as media_mod
from telegram_mcp.tools import messages as messages_mod

RAW_CHAT_ID = "@somechannel"
RESOLVED = types.InputPeerChannel(channel_id=4242, access_hash=7)
AS_CHANNEL = types.InputPeerChannel(channel_id=999, access_hash=11)


def _send_as_peers(with_names=True):
    """What `channels.getSendAs` answers with.

    `chats` and `users` are POPULATED by default, and that is the point. The
    first version of this fixture left them empty, so the branch that turns a
    bare peer id into a title never ran: the test passed green while that code
    raised `NameError` on every real call, which is how it reached the live
    server broken. A fixture that skips a branch tests nothing about it.
    """
    names = (
        {
            "chats": [
                types.Channel(
                    id=999,
                    title="My Channel",
                    photo=None,
                    date=None,
                    username="mychannel",
                    creator=True,
                    left=False,
                    broadcast=True,
                    verified=False,
                    megagroup=False,
                    restricted=False,
                    signatures=False,
                    min=False,
                    scam=False,
                    has_link=False,
                    has_geo=False,
                    slowmode_enabled=False,
                    access_hash=11,
                ),
            ],
            "users": [types.User(id=1, first_name="Me", last_name=None, username="myself")],
        }
        if with_names
        else {"chats": [], "users": []}
    )
    return types.channels.SendAsPeers(
        peers=[
            types.SendAsPeer(peer=types.PeerUser(user_id=1)),
            types.SendAsPeer(peer=types.PeerChannel(channel_id=999)),
            types.SendAsPeer(peer=types.PeerChannel(channel_id=777), premium_required=True),
        ],
        **names,
    )


class Recorder:
    def __init__(self, send_as_peers=None, fail_send_as=False):
        self.sent = []
        self.calls = []
        self._peers = send_as_peers
        self._fail = fail_send_as

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.channels.GetSendAsRequest):
            if self._peers is None:
                raise ValueError("CHAT_ID_INVALID")
            return self._peers
        return True

    async def send_message(
        self,
        entity,
        text,
        reply_to=None,
        parse_mode=None,
        formatting_entities=None,
        message_effect_id=None,
        send_as=None,
    ):
        self.calls.append(("send_message", entity, text, send_as))

    async def send_file(self, entity, file, caption=None, reply_to=None, send_as=None, **kw):
        self.calls.append(("send_file", entity, send_as))

    async def get_me(self, input_peer=False):
        return types.InputPeerUser(user_id=1, access_hash=0)


def _last(client, request_type):
    matches = [r for r in client.sent if isinstance(r, request_type)]
    assert matches, f"no {request_type.__name__} was sent"
    return matches[-1]


# ------------------------------------------------------------------- reader


@pytest.mark.asyncio
async def test_list_send_as_reports_every_identity_and_flags_the_gated_ones(wire_client):
    """A premium-only identity looks exactly like a usable one until it is sent
    with, so the flag has to survive into the answer."""
    import json

    client = wire_client(messages_mod, Recorder(_send_as_peers()), entity=RESOLVED)

    payload = json.loads(await messages_mod.list_send_as(RAW_CHAT_ID))

    assert _last(client, functions.channels.GetSendAsRequest).peer is RESOLVED
    records = payload["results"]
    assert len(records) == 3
    assert [r["premium_required"] for r in records] == [False, False, True]
    # Ids leave as strings for the same reason every other id here does.
    assert all(isinstance(r["send_as"], str) for r in records)
    # A bare peer id is unusable to a human: the title and username have to be
    # looked up out of the answer's own `chats`/`users`.
    channel = next(r for r in records if r["send_as"] == "999")
    assert channel["title"] == "My Channel"
    assert channel["username"] == "mychannel"
    assert next(r for r in records if r["send_as"] == "1")["title"] == "Me"
    # An id the answer names nowhere still has to survive, without a title.
    assert next(r for r in records if r["send_as"] == "777")["title"] is None


@pytest.mark.asyncio
async def test_list_send_as_says_so_when_the_chat_offers_no_choice(wire_client):
    """Most chats do not: it is a channel/megagroup feature, and Telegram's own
    error says nothing about that."""
    wire_client(messages_mod, Recorder(send_as_peers=None), entity=RESOLVED)

    answer = await messages_mod.list_send_as(RAW_CHAT_ID)

    # Names the chat, the feature, and where it IS available - the three things
    # Telegram's own CHAT_ID_INVALID says none of.
    assert RAW_CHAT_ID in answer
    assert "send-as" in answer.lower()
    assert "channel or megagroup" in answer.lower()


# ------------------------------------------------------------------- writer


@pytest.mark.asyncio
async def test_send_message_carries_the_chosen_identity(wire_client):
    client = wire_client(messages_mod, Recorder(_send_as_peers()), entity=AS_CHANNEL)

    await messages_mod.send_message(RAW_CHAT_ID, "hello", send_as="@mychannel")

    call = next(c for c in client.calls if c[0] == "send_message")
    assert call[3] is AS_CHANNEL, "the identity never reached Telethon"


@pytest.mark.asyncio
async def test_sending_without_an_identity_stays_exactly_as_before(wire_client):
    """The default path must not start resolving anything extra."""
    client = wire_client(messages_mod, Recorder(_send_as_peers()), entity=RESOLVED)

    await messages_mod.send_message(RAW_CHAT_ID, "hello")

    call = next(c for c in client.calls if c[0] == "send_message")
    assert call[3] is None
    assert not [
        r for r in client.sent if isinstance(r, functions.channels.GetSendAsRequest)
    ], "a plain send paid for a getSendAs round trip"


@pytest.mark.asyncio
async def test_the_raw_reply_path_carries_it_too(wire_client):
    """Replying inside a topic goes as a raw SendMessageRequest, which is a
    SECOND code path - the one a friendly-method-only change would miss."""
    client = wire_client(messages_mod, Recorder(_send_as_peers()), entity=AS_CHANNEL)

    await messages_mod.reply_to_message(RAW_CHAT_ID, 5, "hi", topic_id=9, send_as="@mychannel")

    assert _last(client, functions.messages.SendMessageRequest).send_as is AS_CHANNEL


@pytest.mark.asyncio
async def test_send_file_carries_it(wire_client, tmp_path, monkeypatch):
    """A channel that posts banners posts images, so text-only would be half a
    feature.

    The file has to sit inside an allowed root or `send_file` refuses it before
    reaching Telegram at all - which it did on the first run of this test, and is
    the guard working, not a problem to route around.
    """
    from telegram_mcp import file_roots

    root = (tmp_path / "root").resolve()
    root.mkdir()
    sample = root / "a.txt"
    sample.write_text("x", encoding="utf-8")
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    client = wire_client(media_mod, Recorder(_send_as_peers()), entity=AS_CHANNEL)

    await media_mod.send_file(RAW_CHAT_ID, str(sample), send_as="@mychannel")

    call = next((c for c in client.calls if c[0] == "send_file"), None)
    assert call is not None, "send_file never reached Telegram"
    assert call[2] is AS_CHANNEL


# ------------------------------------------------------- the sticky default


@pytest.mark.asyncio
async def test_setting_the_default_addresses_both_peers(wire_client):
    """Two peers, and mixing them up is the whole risk: `peer` is the chat the
    default applies IN, `send_as` is the identity it applies TO. Telegram accepts
    either order and the wrong one silently changes a different chat."""
    client = wire_client(messages_mod, Recorder(_send_as_peers()), entity=AS_CHANNEL)

    await messages_mod.set_default_send_as(RAW_CHAT_ID, "@mychannel")

    request = _last(client, functions.messages.SaveDefaultSendAsRequest)
    assert request.peer is AS_CHANNEL
    assert request.send_as is AS_CHANNEL


@pytest.mark.asyncio
async def test_setting_the_default_refuses_an_empty_identity(wire_client):
    """There is no "unset" in the API - Telegram always has a current identity -
    so a blank argument is a caller mistake, not a way back to yourself."""
    wire_client(messages_mod, Recorder(_send_as_peers()), entity=AS_CHANNEL)

    refused = await messages_mod.set_default_send_as(RAW_CHAT_ID, "")

    assert "list_send_as" in refused
