"""Creating, changing and destroying the message itself.

This module owns the write path for a message's own content: sending it
(``send_message``, ``reply_to_message``), rewording it (``edit_message``),
relaying it elsewhere (``forward_message``, ``forward_messages``) and removing
it (``delete_message``, ``delete_messages_bulk``, ``delete_chat_history``).
Every one of them changes what text exists in a chat.

It also holds the shared reading vocabulary — ``get_media_label``,
``get_reply_quote``, ``message_to_dict``, ``format_message_line`` and
``LINK_DOMAIN`` — which ``telegram_mcp.message_view`` and
``telegram_mcp.tools.inspection`` import from this exact path, and which
``messages_read`` imports in turn. Keeping them here is what makes the
dependency between the message modules one-directional: the siblings import
from this module and this module imports from none of them.

Tools that only touch things hanging off a message live next door:
``messages_read`` (queries), ``messages_state`` (pins, reactions, buttons,
polls) and ``messages_queue`` (scheduled sends and drafts).
"""

from telegram_mcp.runtime import *
from telegram_mcp.text_fidelity import display_name

# A URL is a machine value: bounded so a hostile link cannot flood the context,
# but far above display_name's prose default, which cuts real Mini App links in
# half. The convention originates in telegram_mcp/button_view.py; it is repeated
# here rather than imported because that module reaches back into this one and
# this module deliberately imports from none of its siblings (see the docstring).
MAX_MACHINE_VALUE = 2048

# Domain used to build message permalinks. Overridable because the default is a
# single point of failure: on 2026-07-13 the .me registry put t.me on serverHold
# over an OFAC listing and every t.me link on earth broke for about a day, while
# telegram.me kept resolving. The domain has been ACTIVE again since 2026-07-14.
LINK_DOMAIN = os.getenv("TELEGRAM_LINK_DOMAIN", "t.me")

# Bounds on the delete_chat_history continuation loop. Telegram answers each
# deleteHistory with the number of events still to go and expects the call to be
# repeated; these keep "repeat until zero" from becoming "repeat forever" when a
# chat is enormous or the server stops making progress.
_DELETE_HISTORY_MAX_PASSES = 20
_DELETE_HISTORY_DEADLINE_SECONDS = 60.0


def get_media_label(msg) -> str:
    """Short label of attached media for a message, or "" if none.

    The media object is already present on the fetched message (msg.media /
    msg.photo / msg.document etc.) — no extra API call needed. Surfacing it in
    listings prevents the classic miss where a photo/file WITH a caption shows
    up looking like a plain text message (Telethon puts the caption in
    msg.message but the media stays in msg.media).
    """
    try:
        # Link web preview is NOT an attachment. Check it FIRST: for a message with a
        # link, Telethon returns the preview image via msg.photo; otherwise it would
        # be incorrectly classified as a "photo".
        if getattr(msg, "web_preview", None) is not None:
            return ""
        # Sticker/voice/video/audio/GIF are also represented as documents, so check
        # them BEFORE the generic document handler.
        sticker = getattr(msg, "sticker", None)
        if sticker is not None:
            alt = ""
            for attr in getattr(sticker, "attributes", []) or []:
                a = getattr(attr, "alt", None)
                if a:
                    # Whoever built the sticker pack chose this alt, and this label is
                    # what an agent reads to decide what the message contains. Clean it
                    # here, at the producer: an alt that is nothing but bidi overrides
                    # empties out and the label falls back to the bare "sticker".
                    alt = display_name(a)
                    break
            return f"sticker {alt}".strip()
        if getattr(msg, "photo", None) is not None:
            return "photo"
        if getattr(msg, "voice", None) is not None:
            return "voice"
        if getattr(msg, "video_note", None) is not None:
            return "video_note"
        if getattr(msg, "video", None) is not None:
            return "video"
        if getattr(msg, "audio", None) is not None:
            return "audio"
        if getattr(msg, "gif", None) is not None:
            return "gif"
        if getattr(msg, "document", None) is not None:
            name = None
            f = getattr(msg, "file", None)
            if f is not None:
                # The filename is chosen by whoever sent the document. Cleaned before
                # interpolation so a name that is only hidden characters degrades to
                # the bare "document" instead of a dangling "document: ".
                name = display_name(getattr(f, "name", None))
            return f"document: {name}" if name else "document"
        if getattr(msg, "contact", None) is not None:
            return "contact"
        if getattr(msg, "geo", None) is not None:
            return "geo"
        if getattr(msg, "poll", None) is not None:
            return "poll"
        if getattr(msg, "media", None) is not None:
            return "media"
        return ""
    except Exception:
        return ""


def _inline_button_texts(msg):
    """Inline button texts of the message (flat list), [] if none.

    The label is sender-controlled and is exactly what an agent uses to decide
    which button to press, so a bidi override in it can make it read as something
    else entirely. A label that cleans away to nothing is dropped rather than
    reported as an empty button.
    """
    out = []
    try:
        for row in getattr(msg, "buttons", None) or []:
            for b in row:
                t = display_name(getattr(b, "text", None))
                if t:
                    out.append(t)
    except Exception:
        pass
    return out


def _link_urls(msg):
    """Explicit URLs from entities (links hidden behind text), [] if none.

    These are the URLs a message hides behind its visible text, so they are read
    precisely when the visible text cannot be trusted. Bounded as a machine value
    rather than as prose; one that cleans away to nothing is dropped.
    """
    out = []
    try:
        for e in getattr(msg, "entities", None) or []:
            u = display_name(getattr(e, "url", None), max_length=MAX_MACHINE_VALUE)
            if u:
                out.append(u)
    except Exception:
        pass
    return out


def get_reply_quote(msg) -> Optional[dict]:
    """Quoted fragment when a reply targets only *part* of the replied-to message.

    Telegram lets you select a span of another message and reply to just that
    span. Telethon exposes it on msg.reply_to as quote_text (the selected text)
    and quote_offset (its UTF-16 character offset inside the original message).
    Returns {"text": ..., "offset": ...} for such a partial-quote reply, or None
    for a plain whole-message reply (or no reply at all). Independent of
    reply_to_msg_id so a cross-chat quote reply still surfaces its quote.
    """
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return None
    quote_text = getattr(reply, "quote_text", None)
    if not quote_text:
        return None
    quote = {"text": sanitize_user_content(quote_text)}
    offset = getattr(reply, "quote_offset", None)
    if offset is not None:
        quote["offset"] = offset
    return quote


def message_to_dict(msg) -> dict:
    """API-complete but compact Telethon message view (omit empty fields).

    The goal is for the MCP output to match the API object in completeness, rather
    than losing data such as media, albums, forwards, edits, buttons, reactions,
    and so on. All these fields are already present in the message object returned
    by the same get_messages request.
    """
    d = {"id": msg.id, "sender": get_sender_name(msg), "date": msg.date}

    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        d["sender_id"] = sender_id
    username = get_sender_username(msg)
    if username:
        d["username"] = username
    if getattr(msg, "out", False):
        d["out"] = True

    text = sanitize_user_content(msg.message) if getattr(msg, "message", None) else ""
    if text:
        d["text"] = text

    media_label = get_media_label(msg)
    if media_label:
        d["media"] = media_label

    grouped_id = getattr(msg, "grouped_id", None)
    if grouped_id:
        d["grouped_id"] = grouped_id  # album: messages sharing one grouped_id form a single group

    reply_to_id = (
        getattr(msg.reply_to, "reply_to_msg_id", None) if getattr(msg, "reply_to", None) else None
    )
    if reply_to_id:
        d["reply_to"] = reply_to_id
    reply_quote = get_reply_quote(msg)
    if reply_quote:
        d["reply_quote"] = reply_quote  # reply to a selected span of the original

    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        finfo = {}
        fdate = getattr(fwd, "date", None)
        if fdate:
            finfo["date"] = fdate
        fname = getattr(fwd, "from_name", None)
        if fname:
            finfo["from_name"] = sanitize_name(fname)

        # from_name is set only when the original author hides their profile.
        # For an ordinary channel forward the origin sits in fwd.from_id, and
        # reading just from_name loses the attribution the Telegram UI shows as
        # "Forwarded from …". Telethon's msg.forward wrapper resolves that peer
        # from entities already present in the response — no extra API call.
        fo = getattr(msg, "forward", None)
        if fo is not None:
            chat = getattr(fo, "chat", None)
            if chat is not None:
                title = getattr(chat, "title", None) or " ".join(
                    x
                    for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None))
                    if x
                )
                if title:
                    finfo["from_chat"] = sanitize_name(title)
                uname = getattr(chat, "username", None)
                if uname:
                    finfo["from_username"] = uname
            chat_id = getattr(fo, "chat_id", None)
            if chat_id is not None:
                finfo["from_chat_id"] = chat_id
            sender = getattr(fo, "sender", None)
            if sender is not None:
                sname = " ".join(
                    x
                    for x in (
                        getattr(sender, "first_name", None),
                        getattr(sender, "last_name", None),
                    )
                    if x
                )
                if sname:
                    finfo["from_user"] = sanitize_name(sname)

        post_id = getattr(fwd, "channel_post", None)
        if post_id is not None:
            finfo["channel_post"] = post_id
        author = getattr(fwd, "post_author", None)
        if author:
            finfo["post_author"] = sanitize_name(author)

        # Canonical permalink, when the pieces are there: a public channel gives
        # <domain>/<username>/<post>, a private one the <domain>/c/<id>/<post>
        # form that only resolves for members.
        if post_id is not None:
            if finfo.get("from_username"):
                finfo["post_link"] = f"https://{LINK_DOMAIN}/{finfo['from_username']}/{post_id}"
            elif finfo.get("from_chat_id") is not None:
                finfo["post_link"] = (
                    f"https://{LINK_DOMAIN}/c/{abs(finfo['from_chat_id']) % 10**10}/{post_id}"
                )

        d["forwarded"] = finfo or True

    via_bot_id = getattr(msg, "via_bot_id", None)
    if via_bot_id:
        d["via_bot_id"] = via_bot_id

    edit_date = getattr(msg, "edit_date", None)
    if edit_date:
        d["edited"] = edit_date

    if getattr(msg, "pinned", False):
        d["pinned"] = True

    engagement = get_engagement_dict(msg)
    if engagement:
        d["engagement"] = engagement

    replies = getattr(msg, "replies", None)
    if replies is not None:
        cnt = getattr(replies, "replies", None)
        if cnt is not None:
            d["comments"] = cnt

    buttons = _inline_button_texts(msg)
    if buttons:
        d["buttons"] = buttons

    urls = _link_urls(msg)
    if urls:
        d["link_urls"] = urls

    action = getattr(msg, "action", None)
    if action is not None:
        d["action"] = type(action).__name__  # service message (joined/pinned/…)

    ttl = getattr(msg, "ttl_period", None)
    if ttl:
        d["ttl_period"] = ttl

    return d


def format_message_line(msg) -> str:
    """Single-line human-readable message representation with ALL key flags."""
    parts = [f"ID: {msg.id}", get_sender_info(msg), f"Date: {msg.date}"]

    reply_to_id = (
        getattr(msg.reply_to, "reply_to_msg_id", None) if getattr(msg, "reply_to", None) else None
    )
    if reply_to_id:
        parts.append(f"reply to {reply_to_id}")
    reply_quote = get_reply_quote(msg)
    if reply_quote:
        preview = reply_quote["text"].replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        parts.append(f'quoting "{preview}"')

    flags = []
    media_label = get_media_label(msg)
    if media_label:
        flags.append(f"📎 {media_label}")
    grouped_id = getattr(msg, "grouped_id", None)
    if grouped_id:
        flags.append(f"album:{grouped_id}")
    if getattr(msg, "fwd_from", None) is not None:
        flags.append("forwarded")
    if getattr(msg, "edit_date", None):
        flags.append("edited")
    if getattr(msg, "via_bot_id", None):
        flags.append("via_bot")
    if getattr(msg, "pinned", False):
        flags.append("pinned")
    btn = _inline_button_texts(msg)
    if btn:
        flags.append(f"buttons:{len(btn)}")
    action = getattr(msg, "action", None)
    if action is not None:
        flags.append(f"service:{type(action).__name__}")
    if flags:
        parts.append(", ".join(flags))

    engagement_info = get_engagement_info(msg).lstrip(" |").strip()
    if engagement_info:
        parts.append(engagement_info)

    raw = sanitize_user_content(msg.message) if getattr(msg, "message", None) else ""
    safe_text = raw.replace("\n", "\\n") if raw else "[empty]"
    return " | ".join(parts) + f" | Message: {safe_text}"


async def _send_rich(cl, entity, text: str, parse_mode: str, reply_to: Optional[int] = None):
    """Send text as a server-parsed rich message. Returns a JSON result string."""
    import random

    if not await account_is_premium(cl):
        return premium_required_result("send_message")
    try:
        await cl(
            functions.messages.SendMessageRequest(
                peer=entity,
                message=text,
                random_id=random.randint(0, 2**62),
                reply_to=(
                    types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None
                ),
                rich_message=make_rich_input(parse_mode, text),
            )
        )
    except telethon.errors.RPCError as e:
        # Premium can lapse between the check above and the send — same refusal.
        if is_premium_rpc_error(e):
            return premium_required_result("send_message")
        raise
    return json.dumps({"sent": True, "rich": True}, ensure_ascii=False)


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
    account: str = None,
) -> str:
    """
    Send a message to a specific chat.
    Args:
        chat_id: The ID or username of the chat.
        message: The message content to send.
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
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _send_rich(cl, entity, message, parse_mode.lower())
        sent = await cl.send_message(entity, message, parse_mode=parse_mode)
        # The id, because everything a caller might do next needs it: edit, react,
        # pin, forward, delete. Returning only "sent" leaves an agent holding a
        # message it cannot address.
        message_id = getattr(sent, "id", None)
        if message_id is None:
            return "Message sent successfully."
        return format_tool_result(
            [{"message_id": message_id, "chat_id": str(chat_id)}], {"sent": True}
        )
    except Exception as e:
        return log_and_format_error("send_message", e, chat_id=chat_id)


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

        ids_to_forward = message_id
        expanded_from_album = False
        if expand_album and isinstance(message_id, int):
            anchor = await cl.get_messages(from_entity, ids=message_id)
            grouped_id = getattr(anchor, "grouped_id", None) if anchor else None
            if grouped_id is not None:
                # Album ids are allocated contiguously by Telegram; a small
                # window around the anchor reliably captures all siblings.
                window = list(range(message_id - 9, message_id + 10))
                neighbors = await cl.get_messages(from_entity, ids=window)
                sibling_ids = sorted(
                    {
                        m.id
                        for m in neighbors
                        if m is not None and getattr(m, "grouped_id", None) == grouped_id
                    }
                )
                if len(sibling_ids) > 1:
                    ids_to_forward = sibling_ids
                    expanded_from_album = True

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
    account: str = None,
) -> str:
    """
    Edit a message you sent.
    Args:
        chat_id: The ID or username of the chat.
        message_id: The ID of the message to edit.
        new_text: The replacement text.
        parse_mode: Optional formatting mode — same values as send_message: 'md'/'markdown',
            'html', or 'rich'/'rich_markdown'/'rich_html' for full server-side formatting
            (tables, headings, formulas; REQUIRES Telegram Premium — without it nothing is
            changed and a structured telegram_premium_required result is returned).
            Omitting it keeps the previous behavior of this tool: Telethon's client
            default (Markdown), so **bold** in existing edits still renders.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _edit_rich(cl, entity, message_id, new_text, parse_mode.lower())
        # Only pass parse_mode when the caller set it: Telethon treats an explicit
        # None as "disable parsing", while omitting the argument uses its default
        # parser. Passing None unconditionally would turn previously formatted
        # edits into literal text.
        extra = {"parse_mode": parse_mode} if parse_mode is not None else {}
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
    account: str = None,
) -> str:
    """
    Reply to a specific message in a chat.
    Args:
        chat_id: The chat ID or username.
        message_id: The message ID to reply to.
        text: The reply text.
        parse_mode: Optional formatting mode — same values as send_message: 'md'/'markdown',
            'html', or 'rich'/'rich_markdown'/'rich_html' for full server-side formatting
            (tables, headings, formulas; REQUIRES Telegram Premium — without it nothing is
            sent and a structured telegram_premium_required result is returned).
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        if parse_mode and parse_mode.lower() in RICH_PARSE_MODES:
            return await _send_rich(cl, entity, text, parse_mode.lower(), reply_to=message_id)
        await cl.send_message(entity, text, reply_to=message_id, parse_mode=parse_mode)
        return f"Replied to message {message_id} in chat {chat_id}."
    except Exception as e:
        return log_and_format_error(
            "reply_to_message", e, chat_id=chat_id, message_id=message_id, text=text
        )


__all__ = [
    "send_message",
    "forward_message",
    "edit_message",
    "delete_message",
    "delete_chat_history",
    "delete_messages_bulk",
    "reply_to_message",
]
