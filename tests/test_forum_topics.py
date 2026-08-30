"""Forums: one field means two things, and the server used to read only one.

A message's `reply_to` carries both "which topic am I in" and "which message am
I replying to". Posting into topic T *is* replying to T's root, so a topic post
arrives with `reply_to_msg_id = T` and nothing else. Read that field alone and
every topic post looks like a reply to a message nobody replied to — while the
topic itself, the thing the caller actually needs, is never reported.

The send path had the mirror-image gap. `topic_id` existed on five media tools
and nowhere else: `send_message` could not address a topic at all, and nothing
in the server could reply to a message *inside* one, because that needs both ids
and every call site passed a bare int.
"""

from types import SimpleNamespace

import pytest
from telethon.tl.types import InputReplyToMessage

from telegram_mcp.forum import (
    GENERAL_TOPIC_ID,
    reply_target_of,
    topic_reply_to,
    topic_reply_to_request,
)
from telegram_mcp.message_view import describe_topic


def _msg(*, forum=False, msg_id=None, top_id=None, has_reply=True):
    if not has_reply:
        return SimpleNamespace(reply_to=None)
    return SimpleNamespace(
        reply_to=SimpleNamespace(forum_topic=forum, reply_to_msg_id=msg_id, reply_to_top_id=top_id)
    )


# --- reading ----------------------------------------------------------------


def test_a_post_in_a_topic_is_not_a_reply_to_anything():
    """The defect this file exists for. `reply_to_msg_id` holds the topic root,
    so the old reading reported "reply to 42" about a message nobody replied to,
    and never said the post was in topic 42."""
    topic_id, reply_to = reply_target_of(_msg(forum=True, msg_id=42))

    assert topic_id == 42
    assert reply_to is None


def test_a_reply_inside_a_topic_carries_both_ids():
    topic_id, reply_to = reply_target_of(_msg(forum=True, msg_id=99, top_id=42))

    assert topic_id == 42
    assert reply_to == 99


def test_an_ordinary_reply_outside_a_forum_is_unchanged():
    """Guard the guard: the common case must not acquire a phantom topic."""
    topic_id, reply_to = reply_target_of(_msg(forum=False, msg_id=7))

    assert topic_id is None
    assert reply_to == 7


def test_a_message_with_no_reply_has_neither():
    assert reply_target_of(_msg(has_reply=False)) == (None, None)


def test_general_is_a_topic_even_though_it_carries_no_id():
    """A message in General frequently arrives with the forum flag and no ids at
    all. Reporting "no topic" there would be wrong in the one topic every forum
    has."""
    topic_id, reply_to = reply_target_of(_msg(forum=True))

    assert topic_id == GENERAL_TOPIC_ID
    assert reply_to is None


def test_describe_topic_agrees_with_the_shared_rule():
    """`message_view.describe_topic` is a view over the same decision, not a
    second copy of it — two copies drift the first time Telegram adds a field."""
    assert describe_topic(_msg(forum=True, msg_id=42)) == {
        "is_topic_message": True,
        "topic_id": 42,
    }
    assert describe_topic(_msg(forum=True, msg_id=99, top_id=42))["topic_id"] == 42
    assert describe_topic(_msg(forum=False, msg_id=7)) is None


# --- writing ----------------------------------------------------------------


def test_no_target_is_no_reply_to():
    assert topic_reply_to() is None
    assert topic_reply_to_request() is None


def test_a_topic_alone_is_sent_as_a_bare_id():
    """Telethon's high-level senders accept an int, and Telegram reads "reply to
    the topic root" as "post in this topic"."""
    assert topic_reply_to(topic_id=42) == 42


def test_a_reply_alone_is_sent_as_a_bare_id():
    assert topic_reply_to(reply_to_message_id=99) == 99


def test_replying_inside_a_topic_needs_both_ids():
    """The case that was impossible before: a bare int cannot say which topic the
    message being replied to lives in, so the reply landed against the topic root
    instead — in the wrong place, with no error."""
    target = topic_reply_to(topic_id=42, reply_to_message_id=99)

    assert isinstance(target, InputReplyToMessage)
    assert target.reply_to_msg_id == 99
    assert target.top_msg_id == 42


def test_a_raw_request_always_gets_the_tl_type():
    """`SendMessageRequest.reply_to` takes an InputReplyTo, never an int: passing
    one raises inside the serializer rather than being cast."""
    for kwargs in ({"topic_id": 42}, {"reply_to_message_id": 99}):
        target = topic_reply_to_request(**kwargs)
        assert isinstance(target, InputReplyToMessage), kwargs

    pair = topic_reply_to_request(topic_id=42, reply_to_message_id=99)
    assert (pair.reply_to_msg_id, pair.top_msg_id) == (99, 42)


def test_the_round_trip_holds():
    """What is sent for a topic is what is read back for it."""
    sent_topic = 42
    posted = _msg(forum=True, msg_id=topic_reply_to(topic_id=sent_topic))

    assert reply_target_of(posted) == (sent_topic, None)


# --- the tools that had none of this ----------------------------------------


@pytest.mark.parametrize(
    "module_name,tool_name",
    [
        ("messages", "send_message"),
        ("messages", "reply_to_message"),
        ("messages_queue", "save_draft"),
        ("scheduled", "schedule_message"),
        ("media", "send_file"),
        ("media", "send_album"),
        ("media", "send_voice"),
    ],
)
def test_every_sending_tool_can_address_a_topic(module_name, tool_name):
    """`topic_id` used to exist on five media tools and nowhere else, so text —
    the thing people actually send — could not reach a topic at all."""
    import importlib
    import inspect

    module = importlib.import_module(f"telegram_mcp.tools.{module_name}")
    params = inspect.signature(getattr(module, tool_name)).parameters

    assert "topic_id" in params, f"{tool_name} cannot send into a forum topic"


def test_no_tool_builds_its_own_reply_to():
    """Six copies of "which id means the topic" is six chances to disagree."""
    from pathlib import Path

    offenders = [
        path.name
        for path in Path("telegram_mcp/tools").glob("*.py")
        if "InputReplyToMessage(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"{offenders} bypass telegram_mcp.forum"


# --- the readers the first pass missed --------------------------------------


def test_every_message_reader_separates_the_topic_from_the_reply():
    """The first fix reached `message_to_dict` and `deep_message_dict` and left
    six siblings reading `reply_to.reply_to_msg_id` raw - so `list_messages`,
    `search_messages`, `get_pinned_messages`, `get_message_context`,
    `format_message_line` and `get_drafts` all still called a topic post a reply
    to a message nobody replied to.

    `get_message_context` was the worst of them: it FETCHED that id, so the
    "message replied to" it showed was the topic root.
    """
    import inspect

    from telegram_mcp.tools import messages as messages_mod
    from telegram_mcp.tools import messages_queue as queue_mod
    from telegram_mcp.tools import messages_read as read_mod

    for module in (read_mod, messages_mod, queue_mod):
        source = inspect.getsource(module)
        raw = [
            line.strip()
            for line in source.splitlines()
            if ".reply_to.reply_to_msg_id" in line or 'reply_to, "reply_to_msg_id"' in line
        ]
        assert raw == [], f"{module.__name__} still reads reply_to_msg_id raw: {raw}"
        assert "reply_target_of" in source, f"{module.__name__} does not use the shared rule"


@pytest.mark.parametrize("tool_name", ["save_draft", "clear_draft"])
def test_a_draft_can_be_written_and_cleared_in_the_same_topic(tool_name):
    """Telegram keeps ONE draft per topic. `save_draft` could write into a topic
    and `clear_draft` could only ever clear General, so a topic draft was
    unreachable once written."""
    import inspect

    from telegram_mcp.tools import messages_queue as queue_mod

    params = inspect.signature(getattr(queue_mod, tool_name)).parameters

    assert "topic_id" in params, f"{tool_name} cannot address a forum topic"
