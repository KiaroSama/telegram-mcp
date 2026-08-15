"""Deep structured view of a Telethon message.

The upstream ``message_to_dict`` is a compact listing view. This module layers the
remaining API detail on top of it — text entities and their formatting, custom and
premium emoji document IDs, per-reaction breakdowns, sticker metadata, media
dimensions/duration/mime/filename/size, thumbnail availability and topic
membership — so an agent can reason about a message the way the Telegram client
renders it instead of about a flattened string.

Layering rather than reimplementing keeps upstream improvements to
``message_to_dict`` flowing through, and keeps this file free of merge conflicts.
"""

from __future__ import annotations

from typing import Any, Optional

from sanitize import sanitize_name, sanitize_user_content

# Media kinds that carry a downloadable file.
_DOWNLOADABLE_KINDS = {
    "photo",
    "video",
    "video_note",
    "voice",
    "audio",
    "document",
    "sticker",
    "gif",
}


def _entity_kind(entity: Any) -> str:
    """``MessageEntityBoldItalic`` -> ``bold_italic``."""
    name = type(entity).__name__
    if name.startswith("MessageEntity"):
        name = name[len("MessageEntity") :]
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out) or "unknown"


def describe_entities(msg) -> list[dict[str, Any]]:
    """Text entities with their offsets, so formatting is not lost to plain text.

    Offsets and lengths are Telegram's UTF-16 code-unit values, exactly as the API
    reports them; they index into the raw ``message`` string, not into a sanitized
    copy.
    """
    entities = getattr(msg, "entities", None) or []
    text = getattr(msg, "message", "") or ""
    described: list[dict[str, Any]] = []

    for entity in entities:
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        item: dict[str, Any] = {"type": _entity_kind(entity)}
        if offset is not None:
            item["offset"] = offset
        if length is not None:
            item["length"] = length

        if offset is not None and length is not None:
            try:
                # UTF-16 offsets: slice in UTF-16 space, then decode back.
                encoded = text.encode("utf-16-le")
                fragment = encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")
                if fragment:
                    item["text"] = sanitize_user_content(fragment)
            except (UnicodeDecodeError, ValueError):
                pass

        url = getattr(entity, "url", None)
        if url:
            item["url"] = url
        document_id = getattr(entity, "document_id", None)
        if document_id is not None:
            item["custom_emoji_id"] = document_id
        user_id = getattr(entity, "user_id", None)
        if user_id is not None:
            item["user_id"] = user_id
        language = getattr(entity, "language", None)
        if language:
            item["language"] = language
        if getattr(entity, "collapsed", False):
            item["collapsed"] = True
        described.append(item)

    return described


def describe_custom_emoji(msg) -> list[dict[str, Any]]:
    """Custom/premium emoji used in the text, as ``{id, placeholder, offset}``.

    The placeholder is the fallback glyph a non-premium client shows; the ID is
    what identifies the actual animated document.
    """
    found: list[dict[str, Any]] = []
    for entity in describe_entities(msg):
        if entity.get("custom_emoji_id") is None:
            continue
        item = {"document_id": entity["custom_emoji_id"]}
        if entity.get("text"):
            item["placeholder"] = entity["text"]
        if entity.get("offset") is not None:
            item["offset"] = entity["offset"]
        found.append(item)
    return found


def describe_reactions(msg) -> Optional[dict[str, Any]]:
    """Per-reaction counts, including which ones this account chose."""
    reactions = getattr(msg, "reactions", None)
    if reactions is None:
        return None

    results = getattr(reactions, "results", None) or []
    items: list[dict[str, Any]] = []
    for result in results:
        reaction = getattr(result, "reaction", None)
        item: dict[str, Any] = {"count": getattr(result, "count", 0) or 0}
        emoticon = getattr(reaction, "emoticon", None)
        document_id = getattr(reaction, "document_id", None)
        if emoticon:
            item["emoji"] = emoticon
        if document_id is not None:
            item["custom_emoji_id"] = document_id
        if not emoticon and document_id is None:
            item["type"] = type(reaction).__name__
        if getattr(result, "chosen_order", None) is not None:
            item["chosen"] = True
        items.append(item)

    if not items:
        return None
    data: dict[str, Any] = {"total": sum(i["count"] for i in items), "items": items}
    if getattr(reactions, "can_see_list", False):
        data["can_see_list"] = True
    return data


def _sticker_set_name(document) -> Optional[str]:
    for attribute in getattr(document, "attributes", None) or []:
        sticker_set = getattr(attribute, "stickerset", None)
        name = getattr(sticker_set, "short_name", None)
        if name:
            return name
    return None


def _describe_thumbnails(media_owner) -> list[dict[str, Any]]:
    """Available server-side thumbnail sizes, smallest first.

    ``thumb_index`` is the value to pass to ``get_media_thumbnail``.
    """
    sizes = getattr(media_owner, "thumbs", None) or getattr(media_owner, "sizes", None) or []
    described = []
    for index, size in enumerate(sizes):
        item: dict[str, Any] = {"thumb_index": index, "type": getattr(size, "type", None)}
        for field in ("w", "h", "size"):
            value = getattr(size, field, None)
            if value is not None:
                item[{"w": "width", "h": "height", "size": "bytes"}[field]] = value
        item["kind"] = type(size).__name__
        described.append({k: v for k, v in item.items() if v is not None})
    return described


def describe_media(msg) -> Optional[dict[str, Any]]:
    """Full media metadata: kind, file identity, geometry, duration, thumbnails.

    Everything here comes from the message object already in hand — no extra API
    round trip — so an agent can decide whether a download is worth it before
    paying for one.
    """
    media = getattr(msg, "media", None)
    if media is None:
        return None

    info: dict[str, Any] = {"telegram_type": type(media).__name__}

    kind = None
    for candidate in (
        "sticker",
        "photo",
        "voice",
        "video_note",
        "gif",
        "video",
        "audio",
        "document",
    ):
        if getattr(msg, candidate, None) is not None:
            kind = candidate
            break
    if kind is None:
        for candidate in (
            "contact",
            "geo",
            "poll",
            "web_preview",
            "dice",
            "game",
            "invoice",
            "venue",
        ):
            if getattr(msg, candidate, None) is not None:
                kind = candidate
                break
    info["kind"] = kind or "other"

    file = getattr(msg, "file", None)
    if file is not None:
        for attribute, key in (
            ("name", "file_name"),
            ("mime_type", "mime_type"),
            ("size", "size_bytes"),
            ("width", "width"),
            ("height", "height"),
            ("duration", "duration_seconds"),
            ("title", "title"),
            ("performer", "performer"),
            ("emoji", "emoji"),
            ("ext", "extension"),
        ):
            value = getattr(file, attribute, None)
            if value is not None:
                info[key] = (
                    sanitize_name(value) if key in ("file_name", "title", "performer") else value
                )

    document = getattr(msg, "document", None) or getattr(msg, "sticker", None)
    if document is not None:
        document_id = getattr(document, "id", None)
        if document_id is not None:
            info["document_id"] = document_id
        set_name = _sticker_set_name(document)
        if set_name:
            info["sticker_set"] = set_name
        attribute_names = [type(a).__name__ for a in getattr(document, "attributes", None) or []]
        if attribute_names:
            info["attributes"] = attribute_names
        if "DocumentAttributeAnimated" in attribute_names:
            info["animated"] = True
        if info.get("mime_type") == "application/x-tgsticker":
            info["animation_format"] = "lottie_tgs"
        elif info.get("mime_type") == "video/webm" and info["kind"] == "sticker":
            info["animation_format"] = "video_webm"

    photo = getattr(msg, "photo", None)
    if photo is not None and getattr(photo, "id", None) is not None:
        info["photo_id"] = photo.id

    thumbnails = _describe_thumbnails(document if document is not None else photo)
    if thumbnails:
        info["thumbnails"] = thumbnails
        info["has_thumbnail"] = True

    info["downloadable"] = info["kind"] in _DOWNLOADABLE_KINDS

    poll = getattr(msg, "poll", None)
    if poll is not None:
        question = getattr(getattr(poll, "poll", None), "question", None)
        text = getattr(question, "text", None) or question
        if isinstance(text, str):
            info["poll_question"] = sanitize_user_content(text)

    geo = getattr(msg, "geo", None)
    if geo is not None:
        for field in ("lat", "long"):
            value = getattr(geo, field, None)
            if value is not None:
                info["latitude" if field == "lat" else "longitude"] = value

    return info


def describe_topic(msg) -> Optional[dict[str, Any]]:
    """Forum topic membership, when the message lives in one."""
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return None
    if not getattr(reply, "forum_topic", False):
        return None
    topic: dict[str, Any] = {"is_topic_message": True}
    top_id = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    if top_id is not None:
        topic["topic_id"] = top_id
    return topic


def message_permalink(msg, chat: Any = None, link_domain: str = "t.me") -> Optional[str]:
    """Canonical ``t.me`` link for the message, when one can be built."""
    chat = chat if chat is not None else getattr(msg, "chat", None)
    if chat is None:
        return None
    username = getattr(chat, "username", None)
    if username:
        return f"https://{link_domain}/{username}/{msg.id}"
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None
    if not (getattr(chat, "broadcast", False) or getattr(chat, "megagroup", False)):
        return None
    return f"https://{link_domain}/c/{abs(chat_id) % 10**10}/{msg.id}"


def deep_message_dict(
    msg, base: dict[str, Any], chat: Any = None, link_domain: str = "t.me"
) -> dict[str, Any]:
    """Enrich the compact ``message_to_dict`` output with full API detail.

    Args:
        msg: The Telethon message.
        base: Result of the upstream ``message_to_dict(msg)``.
        chat: Resolved chat entity, used to build the permalink.
        link_domain: Domain for permalinks.
    """
    data = dict(base)

    entities = describe_entities(msg)
    if entities:
        data["entities"] = entities
    custom_emoji = describe_custom_emoji(msg)
    if custom_emoji:
        data["custom_emoji"] = custom_emoji

    media = describe_media(msg)
    if media:
        # Upstream stores a short label under "media"; keep it and add the detail.
        data["media_details"] = media

    reactions = describe_reactions(msg)
    if reactions:
        data["reactions"] = reactions

    topic = describe_topic(msg)
    if topic:
        data["topic"] = topic

    permalink = message_permalink(msg, chat=chat, link_domain=link_domain)
    if permalink:
        data["permalink"] = permalink

    for attribute, key in (
        ("post_author", "post_author"),
        ("silent", "silent"),
        ("noforwards", "protected"),
        ("from_scheduled", "from_scheduled"),
    ):
        value = getattr(msg, attribute, None)
        if value:
            data[key] = sanitize_name(value) if isinstance(value, str) else True

    return data
