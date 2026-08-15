"""Telegram's message-effect catalog: fetched once, refreshed the way Telegram asks.

A message-level effect reaches us as a bare integer on ``Message.effect``. Turning
that into something an agent can look at needs ``messages.GetAvailableEffects``,
which does not answer per effect — it returns the whole catalog. On a live account
that is 697 effects and 894 documents, so fetching it per inspected message would
be indefensible. Telegram provides the remedy in the response: send its ``hash``
back and it answers ``AvailableEffectsNotModified`` when nothing has changed.

Everything here is deliberately free of MCP and of the frame extractor: the
resolution rules are worth testing without a client, and the download policy
belongs with the other downloads.
"""

import asyncio
import time
from typing import Any, Optional

from telethon import functions

# Telegram's hash makes a refresh cheap — it replies "not modified" and sends no
# payload — but it is still a round trip, and an agent inspecting a hundred
# messages would make a hundred of them. Inside this window the cached catalogue
# is served without contacting Telegram at all; past it, the hash decides.
# Effects are a slow-moving published set, not per-account state.
_REVALIDATE_AFTER_SECONDS = 30 * 60

# Telegram hands back one flat document list covering every effect. Verified
# against a live account: of 697 effects, not one referenced a document id that
# was missing from that list, so resolving through it needs no second call.
_MIME_FORMATS = {
    "application/x-tgsticker": "lottie_tgs",
    "video/webm": "video",
    "image/webp": "static_image",
    "image/png": "static_image",
}


class Catalog:
    """One fetched snapshot of the effect catalog."""

    __slots__ = ("hash", "effects", "documents", "fetched_at")

    def __init__(self, hash_: int, effects: dict, documents: dict, fetched_at: float = 0.0):
        self.hash = hash_
        self.effects = effects
        self.documents = documents
        self.fetched_at = fetched_at

    def is_fresh(self, now: float) -> bool:
        """Whether this snapshot may be served without asking Telegram again."""
        return (now - self.fetched_at) < _REVALIDATE_AFTER_SECONDS


_catalog: Optional[Catalog] = None
# Without this, two concurrent inspections both see an empty cache and both pull
# the whole catalog; the second one is pure waste.
_lock = asyncio.Lock()


def _reset_catalog() -> None:
    """Drop the cached catalog. For tests and for a file-reference refresh."""
    global _catalog
    _catalog = None


def cached_catalog() -> Optional[Catalog]:
    """The catalog currently held, without fetching one."""
    return _catalog


async def load_catalog(cl, force: bool = False) -> Catalog:
    """The effect catalog, from cache when Telegram says it has not changed.

    ``force`` discards the stored hash, which is what a stale file reference
    needs: the ids stay valid but the references that authorise a download do not.
    """
    global _catalog
    async with _lock:
        now = time.monotonic()
        if _catalog is not None and not force and _catalog.is_fresh(now):
            return _catalog

        known_hash = _catalog.hash if (_catalog is not None and not force) else 0

        result = await cl(functions.messages.GetAvailableEffectsRequest(hash=known_hash))

        # Not modified: Telegram sends no payload at all, so the cache is the only
        # copy in existence. Refusing to trust it here would defeat the mechanism.
        if not hasattr(result, "effects"):
            if _catalog is not None:
                # Unchanged: keep the payload, restart the window rather than
                # revalidating on every call from here on.
                _catalog.fetched_at = now
                return _catalog
            # Telegram answered "unchanged" against a hash we do not hold. Nothing
            # to serve, and asking again with the same hash would repeat it.
            result = await cl(functions.messages.GetAvailableEffectsRequest(hash=0))

        _catalog = Catalog(
            getattr(result, "hash", 0),
            {effect.id: effect for effect in getattr(result, "effects", [])},
            {doc.id: doc for doc in getattr(result, "documents", [])},
            now,
        )
        return _catalog


def describe_document(document, label: str) -> Optional[dict[str, Any]]:
    """What an effect asset is, before deciding whether to pay for it."""
    if document is None:
        return None
    mime = getattr(document, "mime_type", None)
    info: dict[str, Any] = {
        "asset": label,
        "document_id": getattr(document, "id", None),
        "mime_type": mime,
        "size_bytes": getattr(document, "size", None),
        "format": _MIME_FORMATS.get(mime, "unknown"),
    }
    for attribute in getattr(document, "attributes", None) or []:
        width = getattr(attribute, "w", None)
        height = getattr(attribute, "h", None)
        if width and height:
            info["width"], info["height"] = width, height
            break
    return info


def premium_effect_size(document):
    """A document's own premium-effect ``VideoSize``, or ``None``.

    This is the same ``type="f"`` asset a premium sticker carries. It matters here
    because it is Telegram's documented fallback when an effect has no animation
    document of its own — and that is the common case, not the exotic one: 574 of
    697 live effects have no ``effect_animation_id``.
    """
    for video_size in getattr(document, "video_thumbs", None) or []:
        if getattr(video_size, "type", None) == "f":
            return video_size
    return None


def resolve_effect(catalog: Catalog, effect_id: int) -> Optional[dict[str, Any]]:
    """Everything known about one effect, or ``None`` if the catalog lacks it."""
    effect = catalog.effects.get(effect_id)
    if effect is None:
        return None

    sticker = catalog.documents.get(getattr(effect, "effect_sticker_id", None))
    icon = catalog.documents.get(getattr(effect, "static_icon_id", None))
    animation = catalog.documents.get(getattr(effect, "effect_animation_id", None))

    info: dict[str, Any] = {
        "effect_id": effect.id,
        "emoticon": getattr(effect, "emoticon", None),
        "premium_required": bool(getattr(effect, "premium_required", False)),
        "static_icon": describe_document(icon, "static_icon"),
        "preview_sticker": describe_document(sticker, "preview_sticker"),
        "effect_animation": describe_document(animation, "effect_animation"),
    }

    if animation is not None:
        info["animation_source"] = "effect_animation"
    else:
        video_size = premium_effect_size(sticker)
        if video_size is not None:
            info["animation_source"] = "premium_effect_of_preview_sticker"
            fallback: dict[str, Any] = {
                "asset": "premium_effect_of_preview_sticker",
                "document_id": getattr(sticker, "id", None),
                "thumb_type": "f",
                "size_bytes": getattr(video_size, "size", None),
            }
            for field, key in (("w", "width"), ("h", "height")):
                value = getattr(video_size, field, None)
                if value is not None:
                    fallback[key] = value
            info["effect_animation"] = fallback
        else:
            info["animation_source"] = "none"

    return info
