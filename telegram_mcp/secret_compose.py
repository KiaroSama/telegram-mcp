"""Composing one outgoing secret message: its text, its reply, and what is lost.

Three steps every send through a secret chat shares, and which the timed sends in
:mod:`telegram_mcp.tools.secret_timed` need identically. They live here rather than in
the tool module because that module reached 700 lines -- the point at which this
project closes a file to new code -- and because a helper cannot live in two places at
once.

The reply step is the one worth reading, and it changed shape with the backend. The
encrypted layer has **no message ids**: a reply points at the `random_id` its sender
chose (`reply_to_random_id`), which is exactly the id this server publishes as
`message_id`. So the translation is an identity - but the LOOKUP still matters, because
a secret chat's history is local-only and permanently gappy, and a reply to a message
this login never received would arrive as an ordinary message with no error and no
warning. A reply that is not a reply is the silent loss this step exists to refuse.
"""

import asyncio
from typing import Any, Dict, Optional, Tuple

from telethon import utils as telethon_utils

from telegram_mcp import secret_history
from telegram_mcp.secret_limits import dropped_entities, secret_chat_layer

__all__ = ["dropped_note", "formatted_text", "reply_to", "timer_lock"]


def formatted_text(message: str, parse_mode: Optional[str]):
    """``(text, entities)`` for one outgoing message.

    Parsed with the same parser every other send in this server uses, so a caller's
    markdown behaves in a secret chat exactly as it does in an ordinary one. Which
    of the resulting entities actually cross is a separate question, answered by
    `dropped_note` - parsing never silently drops anything here.
    """
    if not parse_mode:
        return message, None

    try:
        parser = telethon_utils.sanitize_parse_mode(parse_mode)
    except (TypeError, ValueError):
        raise ValueError(
            f"parse_mode must be 'markdown' or 'html', not {parse_mode!r}. Leave it unset "
            "to send the text exactly as written. Nothing was sent."
        )
    return parser.parse(message)


def reply_to(account: str, chat_id: int, message_id: Optional[int]) -> Optional[int]:
    """The id to reply to, after proving the target is really there.

    ``None`` when no reply was asked for. Raises ``ValueError`` when the target is
    not in this device's copy, rather than letting the reply arrive as an ordinary
    message.
    """
    if message_id is None:
        return None
    wanted = int(message_id)
    known = {m["message_id"] for m in secret_history.read(account, chat_id, 10_000)}
    if wanted not in known:
        raise ValueError(
            f"Message {message_id} is not in this device's copy of that chat, so it cannot "
            "be replied to. A secret chat has no server-side history to fetch it from, and "
            "the reply would arrive as an ordinary message rather than a reply. "
            "read_secret_messages shows what this login actually received. Nothing was sent."
        )
    return wanted


async def dropped_note(manager, chat_id: int, entities) -> list:
    """Which of the caller's formatting this chat will not carry.

    Costs nothing on an unformatted message: with no entities there is nothing to
    drop, so the chat's layer is never read.
    """
    if not entities:
        return []
    layer = await secret_chat_layer(manager, int(chat_id))
    return dropped_entities(entities, layer)


_timer_locks: Dict[Tuple[int, int, int], asyncio.Lock] = {}


def timer_lock(manager: Any, chat_id: int) -> asyncio.Lock:
    """The one lock every change to a chat's self-destruct timer goes through.

    A timed send reads the timer, arms it, sends and puts the old value back. Two of
    those overlapping, or an owner's ``set_secret_chat_timer`` landing in the middle,
    each read a value the other was about to replace - and the chat was left armed
    (ADR 0002). Keyed by the account's manager and the chat, and by the running loop,
    because an asyncio lock belongs to the loop it was first used on.
    """
    key = (id(manager), int(chat_id), id(asyncio.get_running_loop()))
    lock = _timer_locks.get(key)
    if lock is None:
        lock = _timer_locks[key] = asyncio.Lock()
    return lock
