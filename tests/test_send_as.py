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
# Telethon marks a channel peer as -(1000000000000 + id) - arithmetic, not the
# string "-100" glued on front, which is what the first version of these tests
# guessed. Written out so the rule is visible rather than a magic number.
MARKED_CHANNEL = str(-(1000000000000 + 999))
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
    def __init__(self, send_as_peers=None, default_send_as=None):
        self.sent = []
        self.calls = []
        self._peers = send_as_peers
        self._default_send_as = default_send_as

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.channels.GetSendAsRequest):
            if self._peers is None:
                raise ValueError("CHAT_ID_INVALID")
            return self._peers
        if isinstance(request, functions.channels.GetFullChannelRequest):
            # The default is deliberately NOT the first peer in `_send_as_peers`,
            # because "first means default" is exactly the guess this replaced.
            return types.messages.ChatFull(
                full_chat=types.ChannelFull(
                    id=4242,
                    about="",
                    read_inbox_max_id=0,
                    read_outbox_max_id=0,
                    unread_count=0,
                    chat_photo=None,
                    notify_settings=None,
                    bot_info=[],
                    pts=0,
                    default_send_as=self._default_send_as,
                ),
                chats=[],
                users=[],
            )
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
    channel = next(r for r in records if r["send_as"] == MARKED_CHANNEL)
    assert channel["title"] == "My Channel"
    assert channel["username"] == "mychannel"
    assert next(r for r in records if r["send_as"] == "1")["title"] == "Me"
    # An id the answer names nowhere still has to survive, without a title.
    unnamed = str(-(1000000000000 + 777))
    assert next(r for r in records if r["send_as"] == unnamed)["title"] is None


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


@pytest.mark.asyncio
async def test_the_id_the_reader_gives_is_one_the_writer_can_use(wire_client):
    """The reader/writer gap this feature nearly shipped with.

    `getSendAs` answers with `PeerChannel(channel_id=2234487991)` - the RAW id.
    `resolve_entity` needs the MARKED form, `-1002234487991`, and a raw channel id
    resolves to nothing: the live test failed with "this account cannot see that
    chat" while the value came from the tool's own output moments earlier. So the
    reported id has to be the one that can be handed straight back.

    A user id is unmarked and must stay that way, which is why this asserts both.
    """
    import json

    wire_client(messages_mod, Recorder(_send_as_peers()), entity=RESOLVED)

    records = json.loads(await messages_mod.list_send_as(RAW_CHAT_ID))["results"]

    assert (
        next(r for r in records if r["kind"] == "channel" and r["title"] == "My Channel")[
            "send_as"
        ]
        == MARKED_CHANNEL
    ), "a raw channel id cannot be resolved by anything that consumes it"
    assert (
        next(r for r in records if r["kind"] == "user")["send_as"] == "1"
    ), "a user id is not marked, and marking it would break it"


@pytest.mark.asyncio
async def test_a_numeric_send_as_is_normalised_the_way_chat_id_is(monkeypatch):
    """`send_as` arrives as a STRING and must be normalised before it is resolved.

    `list_send_as` publishes every id as a string - they exceed 2**53 - so the
    value handed back is `"-1001565431543"`, and this project's resolver treats a
    string differently from the int. Live proof: `get_chat` resolved all four
    forms while `set_default_send_as` refused the same values with "this account
    cannot see that chat", because only the first goes through `@validate_id`.

    This asserts on what the RESOLVER WAS ASKED, not on what it returned. The
    shared `wire_client` fixture cannot see this bug at all - it hands back one
    fixed entity whatever it is given, so a string and an int look identical
    through it. A double that answers the same for both inputs cannot test the
    difference between them.
    """
    asked = []

    async def _resolve(value, client=None, account=None):
        asked.append(value)
        return AS_CHANNEL

    async def _ensure(_client):
        return None

    monkeypatch.setattr(messages_mod, "get_client", lambda account=None: Recorder())
    monkeypatch.setattr(messages_mod, "ensure_connected", _ensure)
    monkeypatch.setattr(messages_mod, "resolve_entity", _resolve)

    # By KEYWORD, which is how an MCP call always arrives: the JSON-RPC
    # `arguments` object becomes kwargs. `@validate_id` reads kwargs ONLY and
    # silently skips a positionally-passed argument, so calling it positionally
    # here would test a path production never takes and report a failure that is
    # not real.
    await messages_mod.set_default_send_as(chat_id=RAW_CHAT_ID, send_as="-1001565431543")

    assert -1001565431543 in asked, (
        f"the resolver was asked {asked!r}; a numeric send_as must arrive as an int, "
        "the way @validate_id delivers chat_id"
    )


@pytest.mark.asyncio
async def test_the_default_comes_from_the_chat_not_from_the_list_order(wire_client):
    """`getSendAs` does not put the current default first, and saying it does was
    a guess this tool shipped with.

    Proven live: `set_default_send_as` returned OK, Telegram accepted the write,
    and the order coming back was unchanged. The authoritative field is
    `ChannelFull.default_send_as`, so that is what the flag reads.
    """
    import json

    wire_client(
        messages_mod,
        Recorder(_send_as_peers(), default_send_as=types.PeerChannel(channel_id=999)),
        entity=RESOLVED,
    )

    records = json.loads(await messages_mod.list_send_as(RAW_CHAT_ID))["results"]

    flagged = [r for r in records if r["default"]]
    assert len(flagged) == 1, f"exactly one default expected, got {flagged}"
    assert (
        flagged[0]["send_as"] == MARKED_CHANNEL
    ), "the flag followed list order instead of the chat's own default_send_as"
    assert records[0]["default"] is False, "the first entry is not automatically the default"
