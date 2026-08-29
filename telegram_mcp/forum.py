"""Forum topics: one rule for addressing them, on the way in and on the way out.

A Telegram forum is a supergroup with `forum=True` whose messages live in
topics. Topic 1 is "General". A message belongs to a topic through its
`reply_to`, which is the same field an ordinary reply uses - and that overlap is
where this gets subtle enough to deserve one place.

Telegram has no "topic" field on a send request. Posting into topic T means
replying to T's root message; replying to message M *inside* topic T means
naming both, `reply_to_msg_id=M` and `top_msg_id=T`. Get that pair wrong and the
message still sends, into the wrong place, with no error - which is exactly the
kind of failure that stays invisible until someone reads the chat.

Before this module the rule was spelled out six times: five media tools passed a
bare int as `reply_to`, one built `InputReplyToMessage` by hand, `send_message`
could not address a topic at all, and nothing could reply inside one.
"""

from typing import Any, Optional

from telethon.tl.types import InputReplyToMessage

# Telegram's own id for a forum's default topic. It is not stored on messages
# the way other topics are: a message in General usually carries no
# `reply_to_top_id` at all, which is why reading has to fall back to it rather
# than report "no topic".
GENERAL_TOPIC_ID = 1


def topic_reply_to(
    topic_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Any]:
    """The `reply_to` value that puts a message where the caller meant.

    Four cases, and only the last one needs the explicit type:

      - neither            -> None: an ordinary message in the main chat
      - topic only         -> the topic id: Telegram reads "reply to the topic
        root" as "post in this topic"
      - reply only         -> the message id: an ordinary reply
      - both               -> `InputReplyToMessage(reply_to_msg_id=<message>,
        top_msg_id=<topic>)`, the only shape that says "a reply to THAT message,
        which lives in THAT topic"

    Returning a bare int for the middle two is deliberate: Telethon's high-level
    senders accept it, and the raw-request callers wrap it themselves. The pair
    case cannot be expressed as an int at all, which is why it was previously
    impossible to reply inside a topic.
    """
    if topic_id is None and reply_to_message_id is None:
        return None
    if topic_id is None:
        return reply_to_message_id
    if reply_to_message_id is None:
        return topic_id
    return InputReplyToMessage(reply_to_msg_id=reply_to_message_id, top_msg_id=topic_id)


def topic_reply_to_request(
    topic_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[InputReplyToMessage]:
    """The same decision, as the TL type a raw request needs.

    `functions.messages.SendMessageRequest` and friends take `reply_to` as an
    `InputReplyTo`, never a bare int - passing one there raises inside Telethon's
    serializer rather than being cast.
    """
    target = topic_reply_to(topic_id, reply_to_message_id)
    if target is None:
        return None
    if isinstance(target, InputReplyToMessage):
        return target
    return InputReplyToMessage(reply_to_msg_id=target)


def reply_target_of(msg) -> tuple:
    """``(topic_id, reply_to_message_id)`` - which topic, and a REAL reply only.

    The two overlap on one field and that is the whole difficulty. A message
    posted straight into topic T carries ``reply_to_msg_id = T``, because posting
    in a topic *is* replying to its root. Reading that field alone reports every
    topic post as "a reply to message T" - a plausible sentence about a message
    nobody replied to.

    The discriminator is ``reply_to_top_id``: when it is present the message is a
    genuine reply inside a topic, and both ids mean something. When it is absent
    the id is the topic and there is no reply.
    """
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return None, None

    direct = getattr(reply, "reply_to_msg_id", None)
    if not getattr(reply, "forum_topic", False):
        return None, direct

    top = getattr(reply, "reply_to_top_id", None)
    if top:
        return top, direct
    # The id IS the topic root, so it is membership and not a reply.
    return (direct or GENERAL_TOPIC_ID), None
