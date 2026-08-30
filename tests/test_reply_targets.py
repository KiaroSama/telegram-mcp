"""Replying inside a topic, and quoting a span of what you reply to.

Two defects, one cause. `forum.topic_reply_to` returns an `InputReplyToMessage`
for the case it exists to serve -- "reply to message M *inside topic T*" -- and
both senders handed that straight to Telethon's friendly `send_message`. That
method runs `reply_to` through `utils.get_message_id`, which takes an int or a
`Message` and raises `TypeError: Invalid message type` on anything else. So the
one combination the forum work was built for could not be sent at all.

The same method is why a quote could be READ and not written:
`describe_reply_quote` has always reported `reply_quote.text`/`.offset`, and the
fields that carry them (`quote_text`, `quote_offset`) live on
`InputReplyToMessage` -- which the friendly path cannot accept either.

Both now take the raw `SendMessageRequest`, which is what the field was designed
for. A bare id still takes the friendly path; that is the common case and it
returns a `Message` rather than `Updates`.
"""

from types import SimpleNamespace

import pytest
from telethon import utils as telethon_utils
from telethon.tl import functions
from telethon.tl.types import InputReplyToMessage, MessageEntityBold

from telegram_mcp.forum import topic_reply_to, topic_reply_to_request
from telegram_mcp.tools import messages as messages_mod


class Recorder:
    def __init__(self):
        self.friendly = []
        self.raw = []

    async def send_message(self, entity, message, **kwargs):
        self.friendly.append((message, kwargs))
        return SimpleNamespace(id=101)

    async def __call__(self, request):
        self.raw.append(request)
        # A raw request answers with Updates, never a Message.
        return SimpleNamespace(updates=[SimpleNamespace(id=202, message=None)])


@pytest.fixture
def wired(monkeypatch):
    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(messages_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(messages_mod, "resolve_entity", _resolve)
    return client


# --- the defect itself, at its root -----------------------------------------


def test_telethons_friendly_path_cannot_carry_an_input_reply_to():
    """The fact the whole change rests on. If a future Telethon widens
    `get_message_id`, this fails and the routing can be simplified."""
    with pytest.raises(TypeError):
        telethon_utils.get_message_id(InputReplyToMessage(reply_to_msg_id=5, top_msg_id=9))


def test_a_topic_reply_really_does_produce_that_type():
    """Guard the premise: if this stopped returning the TL type the bug would be
    gone, and so would the reason for the raw path."""
    assert isinstance(topic_reply_to(9, 5), InputReplyToMessage)
    assert topic_reply_to(topic_id=9) == 9, "posting IN a topic is still a bare id"
    assert topic_reply_to(reply_to_message_id=5) == 5


# --- routing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_inside_a_topic_goes_as_a_raw_request(wired):
    await messages_mod.reply_to_message(-100, 55, "on it", topic_id=9)

    assert wired.friendly == [], "the friendly path would have raised TypeError here"
    request = wired.raw[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.reply_to.reply_to_msg_id == 55
    assert request.reply_to.top_msg_id == 9


@pytest.mark.asyncio
async def test_send_message_into_a_topic_with_a_reply_takes_the_same_route(wired):
    await messages_mod.send_message(-100, "hi", topic_id=9, reply_to_message_id=55)

    assert wired.friendly == []
    assert wired.raw[-1].reply_to.top_msg_id == 9


@pytest.mark.asyncio
async def test_a_plain_reply_still_takes_the_friendly_path(wired):
    """Guard the guard: the overwhelmingly common case must not have moved."""
    await messages_mod.reply_to_message(-100, 55, "on it")

    assert wired.raw == []
    _text, kwargs = wired.friendly[-1]
    assert kwargs["reply_to"] == 55


@pytest.mark.asyncio
async def test_posting_into_a_topic_without_replying_stays_friendly(wired):
    await messages_mod.send_message(-100, "hi", topic_id=9)

    assert wired.raw == []
    assert wired.friendly[-1][1]["reply_to"] == 9


# --- quotes ------------------------------------------------------------------


def test_the_target_builder_carries_a_quote():
    target = topic_reply_to_request(reply_to_message_id=5, quote_text="a span", quote_offset=12)

    assert target.quote_text == "a span"
    assert target.quote_offset == 12


def test_a_zero_offset_is_kept():
    """0 means "the quote starts at the beginning" and is falsy. Dropping it
    would leave Telegram to guess, which differs when the fragment repeats."""
    target = topic_reply_to_request(reply_to_message_id=5, quote_text="x", quote_offset=0)

    assert target.quote_offset == 0


@pytest.mark.asyncio
async def test_replying_to_a_span_sends_the_quote(wired):
    await messages_mod.reply_to_message(-100, 55, "about that", quote_text="the bit", quote_offset=4)

    request = wired.raw[-1]
    assert request.reply_to.reply_to_msg_id == 55
    assert request.reply_to.quote_text == "the bit"
    assert request.reply_to.quote_offset == 4


@pytest.mark.asyncio
async def test_a_quote_inside_a_topic_keeps_both_facts(wired):
    await messages_mod.reply_to_message(-100, 55, "yes", topic_id=9, quote_text="frag")

    reply_to = wired.raw[-1].reply_to
    assert (reply_to.top_msg_id, reply_to.reply_to_msg_id) == (9, 55)
    assert reply_to.quote_text == "frag"


# --- what the raw path must not lose ----------------------------------------


@pytest.mark.asyncio
async def test_parse_mode_survives_the_raw_route(wired):
    """The raw request has no parse_mode field -- it takes entities only. Parsing
    has to happen here or bold text would arrive with its asterisks showing."""
    await messages_mod.reply_to_message(-100, 55, "**bold** x", parse_mode="md", topic_id=9)

    request = wired.raw[-1]
    assert request.message == "bold x"
    assert any(isinstance(e, MessageEntityBold) for e in request.entities or [])


@pytest.mark.asyncio
async def test_explicit_entities_survive_the_raw_route(wired):
    await messages_mod.reply_to_message(
        -100,
        55,
        "hello world",
        entities=[{"type": "custom_emoji", "offset": 0, "length": 5, "custom_emoji_id": 999}],
        topic_id=9,
    )

    assert wired.raw[-1].entities[0].document_id == 999


@pytest.mark.asyncio
async def test_an_effect_survives_the_raw_route(wired):
    await messages_mod.reply_to_message(-100, 55, "boom", effect_id=777, topic_id=9)

    assert wired.raw[-1].effect == 777


# --- and the id, which neither path used to return --------------------------


@pytest.mark.asyncio
async def test_a_reply_reports_the_id_it_created(wired):
    result = await messages_mod.reply_to_message(-100, 55, "on it")

    assert '"message_id": 101' in result


@pytest.mark.asyncio
async def test_a_raw_reply_reports_the_id_out_of_updates(wired):
    """Updates carries the id in an UpdateMessageID, not on the result itself."""
    result = await messages_mod.reply_to_message(-100, 55, "on it", topic_id=9)

    assert '"message_id": 202' in result
