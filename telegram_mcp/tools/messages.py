"""Creating, changing and destroying the message itself.

This module owns the write path for a message's own content: sending it
(``send_message``, ``reply_to_message``), rewording it (``edit_message``),
relaying it elsewhere (``forward_message``, ``forward_messages``) and removing
it (``delete_message``, ``delete_messages_bulk``, ``delete_chat_history``).
Every one of them changes what text exists in a chat.

The shared reading vocabulary these tools were once stored alongside —
``get_media_label``, ``get_reply_quote``, ``message_to_dict``,
``format_message_line`` and ``LINK_DOMAIN`` — now lives one file over in
``messages_view``, which renders a message rather than changing one. It is
re-exported at the foot of this module because ``telegram_mcp.message_view``,
``telegram_mcp.tools.inspection`` and ``messages_read`` all import those names
from this exact path, and moving code should not move anyone's import.

Tools that only touch things hanging off a message live next door:
``messages_read`` (queries), ``messages_state`` (pins, reactions, buttons,
polls) and ``messages_queue`` (scheduled sends and drafts).
"""

from telegram_mcp.runtime import *
from telegram_mcp.entities import build_send_entities
import random

from telethon import utils as telethon_utils
from telethon.tl.types import InputReplyToMessage

from telegram_mcp.forum import topic_reply_to, topic_reply_to_request
from telegram_mcp.sent import sent_message_ids

# Bounds on the delete_chat_history continuation loop. Telegram answers each
# deleteHistory with the number of events still to go and expects the call to be
# repeated; these keep "repeat until zero" from becoming "repeat forever" when a
# chat is enormous or the server stops making progress.
_DELETE_HISTORY_MAX_PASSES = 20
_DELETE_HISTORY_DEADLINE_SECONDS = 60.0


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


def _as_utc(value: Union[str, int]) -> datetime:
    """A schedule time from an ISO-8601 string or a Unix timestamp, as UTC.

    Lives here rather than beside the scheduling tools because two modules need
    it and this one is the base the siblings import from; the reverse direction
    is what the module docstring above forbids.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


async def _send_rich(
    cl,
    entity,
    text: str,
    parse_mode: str,
    topic_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
):
    """Send text as a server-parsed rich message. Returns a JSON result string."""
    if not await account_is_premium(cl):
        return premium_required_result("send_message")
    try:
        sent = await cl(
            functions.messages.SendMessageRequest(
                peer=entity,
                message=text,
                random_id=random.randint(0, 2**62),
                # A raw request takes the TL type, never a bare int: passing one
                # raises inside the serializer rather than being cast. The old
                # form here could also not express "reply inside a topic", which
                # needs both ids.
                reply_to=topic_reply_to_request(topic_id, reply_to_message_id),
                rich_message=make_rich_input(parse_mode, text),
            )
        )
    except telethon.errors.RPCError as e:
        # Premium can lapse between the check above and the send — same refusal.
        if is_premium_rpc_error(e):
            return premium_required_result("send_message")
        raise
    # A raw request answers with Updates, not a Message, which is why this path
    # alone still reported only "sent" while `send_message`'s plain path had
    # carried the id for months.
    ids = sent_message_ids(sent)
    payload = {"sent": True, "rich": True}
    if ids:
        payload["message_id"] = ids[0]
    return json.dumps(payload, ensure_ascii=False)


async def _send_text(cl, entity, text, parse_mode, built_entities, effect_id, reply_target):
    """Send plain/parsed text, routed by what the reply target needs.

    Telethon's friendly `send_message` puts `reply_to` through
    `utils.get_message_id`, which takes an int or a Message and raises
    `TypeError: Invalid message type` on anything else. So an `InputReplyToMessage`
    -- the only way to say "reply to message M *inside topic T*", and the only
    way to quote a span -- cannot go through it at all. Those cases go as a raw
    `SendMessageRequest`, which is what the field was designed for.

    A bare id still takes the friendly path: it returns a `Message`, handles
    parse modes server-side, and is the overwhelmingly common case.
    """
    if not isinstance(reply_target, InputReplyToMessage):
        return await cl.send_message(
            entity,
            text,
            parse_mode=parse_mode,
            formatting_entities=built_entities,
            message_effect_id=effect_id,
            reply_to=reply_target,
        )

    # The raw request has no parse_mode: it takes entities only. Parsing here is
    # what the friendly method would have done a moment later anyway.
    if parse_mode and not built_entities:
        parser = telethon_utils.sanitize_parse_mode(parse_mode)
        if parser:
            text, built_entities = parser.parse(text)

    return await cl(
        functions.messages.SendMessageRequest(
            peer=entity,
            message=text,
            random_id=random.randint(0, 2**62),
            reply_to=reply_target,
            entities=built_entities or None,
            effect=effect_id,
        )
    )


async def _edit_rich(cl, entity, message_id: int, text: str, parse_mode: str):
    """Edit a message with server-parsed rich content. Returns a JSON result string."""
    if not await account_is_premium(cl):
        return premium_required_result("edit_message")
    try:
        await cl(
            functions.messages.EditMessageRequest(
                peer=entity,
                id=message_id,
                message=text,
                rich_message=make_rich_input(parse_mode, text),
            )
        )
    except telethon.errors.RPCError as e:
        if is_premium_rpc_error(e):
            return premium_required_result("edit_message")
        raise
    return json.dumps(
        {"sent": True, "rich": True, "edited_message_id": message_id}, ensure_ascii=False
    )


@mcp.tool(
    annotations=ToolAnnotations(title="Send Message", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("chat_id")
async def send_message(
    chat_id: Union[int, str],
    message: str,
    parse_mode: Optional[str] = None,
    entities: List[dict] = None,
    effect_id: int = None,
    topic_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    account: str = None,
) -> str:
    """
    Send a message to a specific chat, or into one forum topic.
    Args:
        chat_id: The ID or username of the chat.
        message: The message content to send.
        entities: Formatting list in the shape `inspect_message` returns
            (type/offset/length plus each kind's own fields). This is the ONLY
            way to place a premium/custom emoji: `parse_mode` has no syntax for
            one. `schedule_message` has always accepted this, so a message with
            custom emoji could be queued for later and not sent now.

            **`message` must be the `text_fidelity` value the entities came
            with**: the offsets are UTF-16 units into exactly that string.
            Anything that cannot be rebuilt faithfully refuses the whole call
            rather than sending text with formatting silently dropped.
        effect_id: A premium message effect, from `get_message_effect`. Telegram
            requires Premium and refuses it otherwise.
        topic_id: Forum topic ID from `list_topics`. In a forum supergroup a
            message sent without this lands in General, not in the topic the
            conversation is in. Pass 1 for General explicitly.
        reply_to_message_id: Reply to this message. Combine with `topic_id` to
            reply to a message that lives inside a topic - naming only the
            message would put the reply in the wrong topic.
        parse_mode: Optional formatting mode. Use 'html' for HTML tags (<b>, <i>, <code>, <pre>,
            <a href="...">), 'md' or 'markdown' for Markdown (**bold**, __italic__, `code`,
            ```pre```), or omit for plain text. Use 'rich'/'rich_markdown' for full
            server-side Markdown (tables, #headings, $formulas$, footnotes, collapsible
            sections) or 'rich_html' for full HTML — rich modes REQUIRE Telegram Premium
            on the account: without it nothing is sent and a structured
            {"sent": false, "reason": "telegram_premium_required"} result tells you to
            reformat and retry with 'md'/'html'. Premium is re-checked on every call
            (it can expire or be bought at any time).
    """
    try:
        built_entities = await build_send_entities(entities, message, account)
        if isinstance(built_entities, str):
            return built_entities
        if built_entities and parse_mode:
            return (
                "Give `entities` or `parse_mode`, not both: they are two ways to "
                "describe the same formatting and Telegram applies only one. "
                "Nothing was sent."
            )

        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _send_rich(
                cl, entity, message, parse_mode.lower(), topic_id, reply_to_message_id
            )
        sent = await _send_text(
            cl,
            entity,
            message,
            parse_mode,
            built_entities,
            effect_id,
            topic_reply_to(topic_id, reply_to_message_id),
        )
        # The id, because everything a caller might do next needs it: edit, react,
        # pin, forward, delete. Returning only "sent" leaves an agent holding a
        # message it cannot address.
        ids = sent_message_ids(sent)
        if not ids:
            return "Message sent successfully."
        return format_tool_result(
            [{"message_id": ids[0], "chat_id": str(chat_id)}], {"sent": True}
        )
    except Exception as e:
        return log_and_format_error("send_message", e, chat_id=chat_id, topic_id=topic_id)


@mcp.tool(
    annotations=ToolAnnotations(title="Forward Message", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("from_chat_id", "to_chat_id")
async def forward_message(
    from_chat_id: Union[int, str],
    message_id: Union[int, List[int]],
    to_chat_id: Union[int, str],
    account: str = None,
    expand_album: bool = True,
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
    """
    try:
        cl = get_client(account)
        from_entity = await resolve_entity(from_chat_id, cl)
        to_entity = await resolve_entity(to_chat_id, cl)

        ids_to_forward, expanded_from_album = await _album_batch(
            cl, from_entity, message_id, expand_album
        )

        await cl.forward_messages(to_entity, ids_to_forward, from_entity)
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
        expand_album: When a single id belongs to an album, copy the whole album
            rather than one detached item. No effect on a list.
        drop_captions: Copy the media without its caption.
    """
    try:
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
        await cl.forward_messages(
            to_entity,
            ids,
            from_entity,
            drop_author=True,
            drop_media_captions=drop_captions,
            schedule=target,
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
        if drop_captions:
            record["captions"] = "dropped"
        return format_tool_result(record)
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Edit Message", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def edit_message(
    chat_id: Union[int, str],
    message_id: int,
    new_text: str,
    parse_mode: Optional[str] = None,
    entities: List[dict] = None,
    account: str = None,
) -> str:
    """
    Edit a message you sent.
    Args:
        chat_id: The ID or username of the chat.
        message_id: The ID of the message to edit.
        new_text: The replacement text.
        entities: Formatting list in the shape `inspect_message` returns - the
            only way to put a premium/custom emoji into an edit, since
            `parse_mode` has no syntax for one. `new_text` must be the
            `text_fidelity` value the entities came with; the offsets are UTF-16
            units into exactly that string.
        parse_mode: Optional formatting mode — same values as send_message: 'md'/'markdown',
            'html', or 'rich'/'rich_markdown'/'rich_html' for full server-side formatting
            (tables, headings, formulas; REQUIRES Telegram Premium — without it nothing is
            changed and a structured telegram_premium_required result is returned).
            Omitting it keeps the previous behavior of this tool: Telethon's client
            default (Markdown), so **bold** in existing edits still renders.
    """
    try:
        built_entities = await build_send_entities(entities, new_text, account)
        if isinstance(built_entities, str):
            return built_entities
        if built_entities and parse_mode:
            return (
                "Give `entities` or `parse_mode`, not both: they are two ways to "
                "describe the same formatting and Telegram applies only one. "
                "Nothing was changed."
            )

        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _edit_rich(cl, entity, message_id, new_text, parse_mode.lower())
        # Only pass parse_mode when the caller set it: Telethon treats an explicit
        # None as "disable parsing", while omitting the argument uses its default
        # parser. Passing None unconditionally would turn previously formatted
        # edits into literal text.
        extra = {"parse_mode": parse_mode} if parse_mode is not None else {}
        if built_entities:
            # An explicit entity list IS the formatting; leaving the default
            # parser on would have it re-read the text and fight them.
            extra = {"parse_mode": None, "formatting_entities": built_entities}
        await cl.edit_message(entity, message_id, new_text, **extra)
        return f"Message {message_id} edited."
    except Exception as e:
        return log_and_format_error(
            "edit_message", e, chat_id=chat_id, message_id=message_id, new_text=new_text
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Message", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def delete_message(
    chat_id: Union[int, str], message_id: int, revoke: bool = False, account: str = None
) -> str:
    """
    Delete a message by ID, from this account's view unless told otherwise.

    Args:
        chat_id: Chat ID or username.
        message_id: The message to delete.
        revoke: Pass True to delete the message for EVERYONE in the chat, wherever
            Telegram still permits it. The default removes it from this account's
            view only, because that is the one of the two that can be lived with
            if it was the wrong message. Ignored for channels, which always delete
            for everyone.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        # Telethon's friendly method defaults to revoke=True, and so did this,
        # which made the least alarming-sounding call the most destructive one on
        # offer: an agent tidying its own view took the message out of the
        # recipient's chat as well. Reaching the other party is now something the
        # caller asks for.
        await cl.delete_messages(entity, message_id, revoke=revoke)
        scope = "for both parties" if revoke else "for you only"
        return f"Message {message_id} deleted {scope}."
    except Exception as e:
        return log_and_format_error("delete_message", e, chat_id=chat_id, message_id=message_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Chat History",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def delete_chat_history(
    chat_id: Union[int, str], max_id: int = 0, revoke: bool = False, account: str = None
) -> str:
    """
    Clear the full message history of a chat.

    Args:
        chat_id: Chat ID or username.
        max_id: Delete messages up to this ID; 0 deletes all messages (default).
        revoke: If True, delete for both parties (default False = only for you).
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        entity = await resolve_entity(chat_id, cl)

        # messages.affectedHistory.offset is a continuation signal: a positive
        # value means the method has to be repeated with the same parameters
        # until it answers zero. One call and a "cleared" report was a claim the
        # server had not made -- it had said the opposite.
        deleted = 0
        offset = None
        passes = 0
        deadline = time.monotonic() + _DELETE_HISTORY_DEADLINE_SECONDS
        stalled = False
        timed_out = False
        while passes < _DELETE_HISTORY_MAX_PASSES:
            budget_left = deadline - time.monotonic()
            if budget_left <= 0:
                break
            try:
                # The budget bounds the CALL, not merely the gap between calls.
                # Checked only afterwards, a single request that never returned
                # sat past the deadline for as long as Telegram felt like, and
                # only cancellation from outside ever ended it. wait_for cancels
                # the request it is waiting on, so nothing is left running.
                result = await asyncio.wait_for(
                    cl(
                        functions.messages.DeleteHistoryRequest(
                            peer=entity, max_id=max_id, revoke=revoke
                        )
                    ),
                    timeout=budget_left,
                )
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                break
            passes += 1
            deleted += getattr(result, "pts_count", 0) or 0
            remaining = getattr(result, "offset", 0) or 0
            if remaining <= 0:
                offset = 0
                break
            # A remainder that does not shrink is not progress. Repeating the
            # identical request against it is an unbounded spin, so it stops here
            # and says what is left rather than looping on hope.
            if offset is not None and remaining >= offset:
                offset = remaining
                stalled = True
                break
            offset = remaining

        scope = "for both parties" if revoke else "for you"
        if offset == 0:
            return f"Chat {chat_id} history cleared {scope}: {deleted} messages deleted."
        if stalled:
            reason = "the server stopped reporting progress"
        elif timed_out:
            reason = (
                f"a delete call did not answer inside the "
                f"{_DELETE_HISTORY_DEADLINE_SECONDS:g}s budget and was abandoned"
            )
        else:
            reason = f"the {passes}-pass/{_DELETE_HISTORY_DEADLINE_SECONDS:g}s budget ran out"
        # An unanswered first call leaves no offset to quote; saying "unknown" is
        # the honest form of "Telegram never told us".
        left = "unknown" if offset is None else offset
        return (
            f"Chat {chat_id} history deletion is INCOMPLETE {scope}: {deleted} messages "
            f"deleted over {passes} pass(es), and Telegram still reports offset={left} "
            f"left because {reason}. Run delete_chat_history again to continue."
        )
    except telethon.errors.rpcerrorlist.ChatAdminRequiredError:
        return "Cannot delete chat history: admin privileges are required."
    except Exception as e:
        return log_and_format_error(
            "delete_chat_history",
            e,
            chat_id=chat_id,
            max_id=max_id,
            revoke=revoke,
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Messages Bulk",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=True,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def delete_messages_bulk(
    chat_id: Union[int, str],
    message_ids: List[int],
    revoke: bool = True,
    account: str = None,
) -> str:
    """
    Delete multiple messages in a single call.

    Args:
        chat_id: Chat ID or username.
        message_ids: List of message IDs to delete.
        revoke: If True, delete for both parties (default True). Ignored for channels.

    Outside channels Telegram treats a message id as account-global, not scoped
    to a chat: `messages.DeleteMessagesRequest` carries no peer field at all. So
    in a private chat or a basic group these ids are NOT restricted to `chat_id`
    - pass ids you read from this same chat.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        entity = await resolve_entity(chat_id, cl)
        if isinstance(entity, Channel):
            result = await cl(
                functions.channels.DeleteMessagesRequest(channel=entity, id=message_ids)
            )
        else:
            result = await cl(
                functions.messages.DeleteMessagesRequest(id=message_ids, revoke=revoke)
            )
        pts_count = getattr(result, "pts_count", 0)
        return f"Deleted {pts_count} of {len(message_ids)} messages from chat {chat_id}."
    except telethon.errors.rpcerrorlist.MessageIdInvalidError:
        return "Cannot delete messages: one or more message IDs are invalid."
    except telethon.errors.rpcerrorlist.ChatAdminRequiredError:
        return "Cannot delete messages: admin privileges are required."
    except Exception as e:
        return log_and_format_error(
            "delete_messages_bulk",
            e,
            chat_id=chat_id,
            message_ids=message_ids,
            revoke=revoke,
        )


@mcp.tool(
    annotations=ToolAnnotations(title="Reply To Message", openWorldHint=True, destructiveHint=True)
)
@with_account(readonly=False)
@validate_id("chat_id")
async def reply_to_message(
    chat_id: Union[int, str],
    message_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    entities: List[dict] = None,
    effect_id: int = None,
    topic_id: Optional[int] = None,
    quote_text: Optional[str] = None,
    quote_offset: Optional[int] = None,
    account: str = None,
) -> str:
    """
    Reply to a specific message in a chat, including one inside a forum topic.
    Args:
        chat_id: The chat ID or username.
        message_id: The message ID to reply to.
        text: The reply text.
        entities: Formatting list in the shape `inspect_message` returns - the
            only way to put a premium/custom emoji in a reply. `text` must be the
            `text_fidelity` value the entities came with.
        effect_id: A premium message effect, from `get_message_effect`.
        topic_id: The forum topic the message you are replying to lives in, from
            `list_topics`. Without it a reply to a message inside a topic is
            posted against the topic root instead, which puts it in the wrong
            place with no error. Omit outside forums.

            To post a NEW message in a topic rather than reply to one, use
            `send_message` with `topic_id` - passing a topic id here as the
            message id happens to work and is not what this argument means.
        quote_text: Reply to a SPAN of the message rather than the whole of it -
            the partial quote `inspect_message` reports as `reply_quote.text`.
            Must be an exact substring of the replied-to message; Telegram
            rejects a fragment it cannot find.
        quote_offset: Where that span starts, as `reply_quote.offset` reports it
            (a UTF-16 code-unit index into the replied-to message, not into
            `text`). Needed when the fragment appears more than once; omit and
            Telegram locates it itself.
        parse_mode: Optional formatting mode — same values as send_message: 'md'/'markdown',
            'html', or 'rich'/'rich_markdown'/'rich_html' for full server-side formatting
            (tables, headings, formulas; REQUIRES Telegram Premium — without it nothing is
            sent and a structured telegram_premium_required result is returned).
    """
    try:
        built_entities = await build_send_entities(entities, text, account)
        if isinstance(built_entities, str):
            return built_entities
        if built_entities and parse_mode:
            return (
                "Give `entities` or `parse_mode`, not both: they are two ways to "
                "describe the same formatting and Telegram applies only one. "
                "Nothing was sent."
            )

        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _send_rich(cl, entity, text, parse_mode.lower(), topic_id, message_id)
        # A quote needs the TL type even without a topic: it carries fields a bare
        # message id has nowhere to put.
        target = (
            topic_reply_to_request(topic_id, message_id, quote_text, quote_offset)
            if quote_text
            else topic_reply_to(topic_id, message_id)
        )
        sent = await _send_text(cl, entity, text, parse_mode, built_entities, effect_id, target)
        ids = sent_message_ids(sent)
        note = f"Replied to message {message_id} in chat {chat_id}."
        if not ids:
            return note
        return format_tool_result(
            [{"message_id": ids[0], "chat_id": str(chat_id)}],
            {"sent": True, "replied_to": message_id, "detail": note},
        )
    except Exception as e:
        return log_and_format_error(
            "reply_to_message", e, chat_id=chat_id, message_id=message_id, text=text
        )


__all__ = [
    "send_message",
    "copy_message",
    "forward_message",
    "forward_messages",
    "edit_message",
    "delete_message",
    "delete_chat_history",
    "delete_messages_bulk",
    "reply_to_message",
]

# The rendering helpers, re-exported for the callers that already import them
# from this path: `message_view` (deferred, to break the cycle), `tools.inspection`,
# `messages_read` and five test modules. Kept out of `__all__` above so the star
# import in `tools/__init__.py` still binds only this module's own tools -- two
# modules exporting one name is how a tool silently loses to its twin.
#
# Deliberately at the FOOT of the file, after the tools: an import at the top
# would read as a dependency this module has, and it has none. Nothing above
# this line uses these names.
#
# Read-only aliases. `LINK_DOMAIN` in particular is a copy of the binding, so
# overriding the domain has to happen in `messages_view`, where the builder that
# reads it lives; rebinding it here changes nothing.
from telegram_mcp.tools.messages_view import (  # noqa: E402,F401  (re-exported)
    LINK_DOMAIN,
    MAX_MACHINE_VALUE,
    _inline_button_texts,
    _link_urls,
    format_message_line,
    get_media_label,
    get_reply_quote,
    message_to_dict,
)
