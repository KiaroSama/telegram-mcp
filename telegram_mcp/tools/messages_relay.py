"""Putting a message that already exists into another chat.

``forward_message`` and ``forward_messages`` keep Telegram's forward header;
``copy_message`` deliberately drops it, re-sending the content as this account's
own message. They are together because that is one decision with two answers,
and because both widen a single id to its whole album through ``_album_batch`` —
an album that forwards whole and copies as one detached photo is the same
surprise twice.

One-way by design, like every sibling in this family: it imports the shared
schedule vocabulary from ``messages`` and ``messages`` imports nothing back. The
re-export at the foot of that module is only there so the paths callers already
use keep resolving; it is not a dependency.
"""

import random

from telegram_mcp.runtime import *

# The base module of the message family. `copy_message` schedules a repeat with
# the same parser `schedule_message` uses, and two tools reading "daily"
# differently is the drift a shared definition exists to prevent.
from telegram_mcp.tools.messages import _PREMIUM_NOTE, _as_utc, _repeat_seconds


async def _album_batch(cl, entity, message_id, expand: bool):
    """``(ids, expanded)`` for one message id, widened to its album when asked.

    Telegram allocates an album's ids contiguously, so a small window around the
    anchor captures the siblings. Shared by forward and copy: an album that
    forwards whole and copies as one detached photo is the same surprise twice.
    """
    if not expand or not isinstance(message_id, int):
        return message_id, False
    anchor = await cl.get_messages(entity, ids=message_id)
    grouped_id = getattr(anchor, "grouped_id", None) if anchor else None
    if grouped_id is None:
        return message_id, False
    window = list(range(message_id - 9, message_id + 10))
    neighbors = await cl.get_messages(entity, ids=window)
    sibling_ids = sorted(
        {m.id for m in neighbors if m is not None and getattr(m, "grouped_id", None) == grouped_id}
    )
    if len(sibling_ids) > 1:
        return sibling_ids, True
    return message_id, False


@mcp.tool(
    annotations=ToolAnnotations(title="Forward Message", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("from_chat_id", "to_chat_id", "send_as")
async def forward_message(
    from_chat_id: Union[int, str],
    message_id: Union[int, List[int]],
    to_chat_id: Union[int, str],
    account: str = None,
    expand_album: bool = True,
    topic_id: Optional[int] = None,
    send_as: Union[int, str] = None,
    drop_author: bool = False,
    silent: bool = False,
) -> str:
    """
    Forward a message (or several) from a source chat to a destination chat.

    When forwarding a single int message_id, the server automatically detects
    Telegram albums (multi-photo/video posts sharing a `grouped_id`) and
    forwards the ENTIRE album as one grouped batch — so the destination
    receives the album intact with "Forwarded from <source>", not a single
    detached photo. This is the desired behavior in almost all cases.

    Set expand_album=False to forward only the exact message you specified
    (useful if you really want one photo out of an album).

    To forward a specific set of unrelated messages, pass a list of ints.
    Album expansion is not applied to list inputs — the list is treated as
    the explicit batch.

    Args:
        from_chat_id: Source chat (id or @username).
        message_id: A single message id (int) OR a list of ids. Single ints
            are auto-expanded to the full album when applicable.
        to_chat_id: Destination chat (id or @username).
        account: Optional account label for multi-account mode.
        expand_album: If True (default) and message_id is a single int, the
            server expands albums automatically. No effect on list inputs.
        topic_id: Forward INTO this forum topic. Without it a forward lands in
            the destination's General topic, which is a different place.
        send_as: Post the forward under this identity instead of your own -
            same values `list_send_as` reports for the DESTINATION.
        drop_author: Forward without the "Forwarded from" header.
        silent: Deliver without a notification.
    """
    try:
        cl = get_client(account)
        from_entity = await resolve_entity(from_chat_id, cl)
        to_entity = await resolve_entity(to_chat_id, cl)

        ids_to_forward, expanded_from_album = await _album_batch(
            cl, from_entity, message_id, expand_album
        )

        # Telethon's helper has no `top_msg_id` or `send_as`, so routing means
        # the raw request. The helper still handles everything else, and it is
        # kept for the ordinary case rather than reimplemented alongside it.
        if topic_id is not None or send_as is not None:
            posting_as = await resolve_input_entity(send_as, cl) if send_as else None
            batch = ids_to_forward if isinstance(ids_to_forward, list) else [ids_to_forward]
            await cl(
                functions.messages.ForwardMessagesRequest(
                    from_peer=await resolve_input_entity(from_chat_id, cl),
                    id=batch,
                    to_peer=await resolve_input_entity(to_chat_id, cl),
                    # Telegram deduplicates on random_id, so a per-message one
                    # is required: reusing a value silently drops the copy.
                    random_id=[random.randrange(-(2**63), 2**63) for _ in batch],
                    drop_author=drop_author or None,
                    silent=silent or None,
                    **({"top_msg_id": topic_id} if topic_id is not None else {}),
                    **({"send_as": posting_as} if posting_as is not None else {}),
                )
            )
        else:
            # Only what was actually asked for. Passing `drop_author=None`
            # unconditionally changes this call's signature for every existing
            # caller and test - the exact break `send_as` caused last time.
            await cl.forward_messages(
                to_entity,
                ids_to_forward,
                from_entity,
                **({"drop_author": True} if drop_author else {}),
                **({"silent": True} if silent else {}),
            )
        count = len(ids_to_forward) if isinstance(ids_to_forward, list) else 1
        if count == 1:
            return f"Message {message_id} forwarded from {from_chat_id} to {to_chat_id}."
        if expanded_from_album:
            return (
                f"Album of {count} messages forwarded from {from_chat_id} "
                f"to {to_chat_id} (auto-expanded from message {message_id})."
            )
        return f"{count} messages forwarded from {from_chat_id} to {to_chat_id}."
    except Exception as e:
        return log_and_format_error(
            "forward_message",
            e,
            from_chat_id=from_chat_id,
            message_id=message_id,
            to_chat_id=to_chat_id,
            topic_id=topic_id,
            send_as=send_as,
        )


@mcp.tool(
    annotations=ToolAnnotations(title="Copy Message", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("from_chat_id", "to_chat_id")
async def copy_message(
    from_chat_id: Union[int, str],
    message_id: Union[int, List[int]],
    to_chat_id: Union[int, str],
    when: Union[str, int] = None,
    repeat: str = None,
    topic_id: Optional[int] = None,
    expand_album: bool = True,
    drop_captions: bool = False,
    account: str = None,
) -> str:
    """
    Send a copy of a message, with no "Forwarded from" header.

    This is Telegram's own copy and the SERVER makes it, so custom (premium)
    emoji, every other entity, and any attached media arrive exactly as they
    were. Rebuilding the text on this side cannot match that: a premium emoji is
    a document id pinned to a UTF-16 offset, so anything that re-derives the text
    moves the offsets out from under it and the emoji lands on the wrong
    character. Copy with this; use `schedule_message(entities=...)` only to
    compose something new.

    Args:
        from_chat_id: Source chat (id or @username).
        message_id: A single message id, or a list of ids.
        to_chat_id: Destination chat (id or @username).
        when: Omit to send now. An ISO-8601 string ("2026-09-01T14:30:00Z") or a
            Unix timestamp schedules the copy instead; a naive datetime is UTC.
        repeat: "daily", "weekly", or omitted for a single send, exactly as
            `schedule_message` takes it. Needs `when`. This is the only way to
            put a RICH message on a recurring schedule: `schedule_message`
            composes from text and entities, which cannot express a table, a
            photo block or anything else `read_rich_message` reports - the copy
            is made by Telegram itself and carries all of it. Telegram requires
            Premium for the period.
        topic_id: Forum topic id from `list_topics`. Without it a copy into a
            forum supergroup lands in General, which is a different place and
            reports success either way.
        expand_album: When a single id belongs to an album, copy the whole album
            rather than one detached item. No effect on a list.
        drop_captions: Copy the media without its caption.
    """
    try:
        period = _repeat_seconds(repeat)
        if isinstance(period, str):
            return period
        if period is not None and when is None:
            return "repeat needs `when`: a recurring copy has to start somewhere."
        target = None
        if when is not None:
            target = _as_utc(when)
            if target <= datetime.now(timezone.utc):
                return (
                    f"when must be in the future - got {target.isoformat()}, now "
                    f"{datetime.now(timezone.utc).isoformat()}."
                )

        cl = get_client(account)
        await ensure_connected(cl)
        from_entity = await resolve_entity(from_chat_id, cl)
        to_entity = await resolve_entity(to_chat_id, cl)

        ids, expanded = await _album_batch(cl, from_entity, message_id, expand_album)
        # Telethon's helper carries neither a repeat period nor a topic, so
        # either one drops this to the raw request - the same reason
        # `forward_message` has two paths.
        if period is None and topic_id is None:
            await cl.forward_messages(
                to_entity,
                ids,
                from_entity,
                drop_author=True,
                drop_media_captions=drop_captions,
                schedule=target,
            )
        else:
            ids_to_forward = ids if isinstance(ids, list) else [ids]
            await cl(
                functions.messages.ForwardMessagesRequest(
                    from_peer=from_entity,
                    id=ids_to_forward,
                    to_peer=to_entity,
                    random_id=[
                        int.from_bytes(os.urandom(8), "big", signed=True) for _ in ids_to_forward
                    ],
                    drop_author=True,
                    schedule_date=target,
                    schedule_repeat_period=period,
                    top_msg_id=topic_id,
                    **({"drop_media_captions": True} if drop_captions else {}),
                )
            )

        count = len(ids) if isinstance(ids, list) else 1
        record = {
            "copied": count,
            "from_chat": from_chat_id,
            "to_chat": to_chat_id,
            "attribution": "dropped",
        }
        if expanded:
            record["expanded_from_album"] = message_id
        if target is not None:
            record["scheduled_for"] = target.isoformat()
        if period is not None:
            record["repeat"] = repeat
            record["repeat_seconds"] = period
        if topic_id is not None:
            record["topic_id"] = topic_id
        if drop_captions:
            record["captions"] = "dropped"
        return format_tool_result(record)
    except telethon.errors.rpcerrorlist.PremiumAccountRequiredError:
        return _PREMIUM_NOTE
    except telethon.errors.rpcerrorlist.ChatForwardsRestrictedError:
        # Content protection. Worth naming, because the obvious next move -
        # reading the text and sending it again - is exactly what loses the
        # premium emoji, and it is also what the source chat forbade.
        return (
            f"Chat {from_chat_id} has content protection on, so Telegram refuses to "
            f"copy or forward from it."
        )
    except Exception as e:
        return log_and_format_error(
            "copy_message",
            e,
            from_chat_id=from_chat_id,
            message_id=message_id,
            to_chat_id=to_chat_id,
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Forward Messages (batch)", openWorldHint=True, destructiveHint=True
    )
)
@with_account(readonly=False)
@validate_id("from_chat_id", "to_chat_id")
async def forward_messages(
    from_chat_id: Union[int, str],
    message_ids: List[int],
    to_chat_id: Union[int, str],
    account: str = None,
) -> str:
    """
    Forward a BATCH of messages from a source chat to a destination chat in
    a single atomic call.

    Use this whenever you need to forward more than one message. Pass all
    message ids as a list (e.g. message_ids=[12345, 12346, 12347]). Calling
    this once with a list is strictly better than calling forward_message
    multiple times: it preserves Telegram album grouping (siblings sharing
    `grouped_id` arrive as one grouped album), is atomic, and counts as a
    single forward op for Telegram rate limits.

    For exactly one message, you may use either this tool with a one-item
    list or `forward_message` with an int.

    Args:
        from_chat_id: Source chat (id or @username).
        message_ids: List of message ids to forward, in any order
            (e.g. [12345, 12346]). Must contain at least one id.
        to_chat_id: Destination chat (id or @username).
        account: Optional account label for multi-account mode.
    """
    try:
        if not message_ids:
            return "Error: message_ids must contain at least one id."
        cl = get_client(account)
        from_entity = await resolve_entity(from_chat_id, cl)
        to_entity = await resolve_entity(to_chat_id, cl)
        await cl.forward_messages(to_entity, list(message_ids), from_entity)
        return f"{len(message_ids)} messages forwarded from " f"{from_chat_id} to {to_chat_id}."
    except Exception as e:
        return log_and_format_error(
            "forward_messages",
            e,
            from_chat_id=from_chat_id,
            message_ids=message_ids,
            to_chat_id=to_chat_id,
        )
