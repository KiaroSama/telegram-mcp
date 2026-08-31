"""Turning a fetched Telethon message into something a caller can read.

Nothing here talks to Telegram. Every function takes a message object that some
other module already fetched and renders it: as a compact dict
(``message_to_dict``), as one human-readable line (``format_message_line``), or
as one of the fragments those two are built from (``get_media_label``,
``get_reply_quote``, ``_inline_button_texts``, ``_link_urls``). ``LINK_DOMAIN``
sits with them because the permalink it builds is part of that rendering.

This is the bottom of the message stack: it imports from none of its siblings,
and ``messages``, ``messages_read`` and ``inspection`` all read from it. Keeping
the direction one-way is the point — ``telegram_mcp.message_view`` reaches back
here for ``get_media_label``, and it can only afford to do that because the call
is deferred inside a function (see ``message_view.describe_media_label``).

The tools that send, edit and delete a message live next door in ``messages``,
which re-exports these names so the path every existing caller imports them from
keeps resolving.
"""

from telegram_mcp.runtime import *

from telegram_mcp.forum import reply_target_of
from telegram_mcp.text_fidelity import display_name

# The one place that knows the `/c/` permalink id shape, so the two builders
# cannot drift. The edge back from message_view into this module is deferred
# (see message_view.describe_media_label), so this direction stays safe.
from telegram_mcp.message_view import channel_link_id

# A URL is a machine value: bounded so a hostile link cannot flood the context,
# but far above display_name's prose default, which cuts real Mini App links in
# half. The convention originates in telegram_mcp/button_view.py; it is repeated
# here rather than imported because that module reaches back into this stack and
# this module deliberately imports from none of its siblings (see the docstring).
MAX_MACHINE_VALUE = 2048

# Domain used to build message permalinks. Overridable because the default is a
# single point of failure: on 2026-07-13 the .me registry put t.me on serverHold
# over an OFAC listing and every t.me link on earth broke for about a day, while
# telegram.me kept resolving. The domain has been ACTIVE again since 2026-07-14.
#
# `message_to_dict` reads this name out of THIS module, so an override has to be
# applied here. Setting the copy that `messages` re-exports rebinds a second name
# and changes nothing the builder below reads.
LINK_DOMAIN = os.getenv("TELEGRAM_LINK_DOMAIN", "t.me")


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

    # Which topic, and a real reply - not the same field twice. A message posted
    # into a forum topic carries the topic root in reply_to_msg_id, so reading
    # that alone reported every topic post as a reply to a message nobody had
    # replied to, and never said which topic it was in.
    topic_id, reply_to_id = reply_target_of(msg)
    if topic_id:
        d["topic_id"] = topic_id
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
                    f"https://{LINK_DOMAIN}/c/{channel_link_id(finfo['from_chat_id'])}/{post_id}"
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

    reply_to_id = reply_target_of(msg)[1]
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


# Empty on purpose, not an oversight. `tools/__init__.py` star-imports every module
# in this package, and here `__all__` is the list of MCP tools a module registers —
# which is what stops two modules from exporting one tool name and silently
# shadowing each other. This module registers none, so its share of that surface is
# nothing, and leaving it empty keeps the package namespace exactly as it was
# before the split. The helpers above are imported by name, or through the
# re-export in `messages`; never by star.
__all__ = []
