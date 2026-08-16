"""Telegram's message-effect catalog: fetched once, refreshed the way Telegram asks.

A message-level effect reaches us as a bare integer on ``Message.effect``. Turning
that into something an agent can look at needs ``messages.GetAvailableEffects``,
which does not answer per effect — it returns the whole catalog. On a live account
that is 697 effects and 894 documents, so fetching it per inspected message would
be indefensible.

Three distinct refreshes, which is the whole design:

* **cached** — inside the window, nothing is sent;
* **revalidate** — send the stored hash back, and Telegram answers
  ``AvailableEffectsNotModified`` with no payload;
* **hard refresh** — send ``hash=0`` and pay for the entire catalog again.

Only an expired file reference justifies the third, because only a fresh payload
carries fresh references. Using it to answer "is this ID real?" would download 894
documents to learn that one integer is absent.

State is keyed by account. A ``Document`` carries an ``access_hash`` and a
``file_reference`` that authorise a download for the session that fetched them;
nothing documents them as portable between accounts, so nothing here is shared.

Everything is free of MCP and of the frame extractor: the resolution rules are
worth testing without a client, and the download policy belongs with the other
downloads.
"""

import asyncio
import time
from typing import Any, Optional

from telethon import functions

# Telegram's guidance is to re-check the catalogue at most hourly. The one
# documented exception — an unknown effect ID — is served by a hash revalidation,
# which costs a round trip and no payload.
_REVALIDATE_AFTER_SECONDS = 60 * 60

# Ceiling on the per-snapshot negative cache. The effect ID comes from tool input,
# so it needs one; it is enforced where an ID is added, not where the snapshot is
# confirmed unchanged.
MAX_UNKNOWN_IDS = 1024

# Telegram hands back one flat document list covering every effect. Verified
# against a live account: of 697 effects, not one referenced a document id that
# was missing from that list, so resolving through it needs no second call.
_MIME_FORMATS = {
    "application/x-tgsticker": "lottie_tgs",
    "video/webm": "video",
    "image/webp": "static_image",
    "image/png": "static_image",
}


# gzip. Telegram's .tgs is a gzipped Lottie, and the premium-effect asset turned
# out to be one where the code had assumed WebM — which is exactly why the format
# is read from the bytes rather than from where the asset came from.
_GZIP_MAGIC = bytes((0x1F, 0x8B))


def sniff_asset_format(raw: bytes, mime_format: str = "unknown") -> tuple[str, str]:
    """``(suffix, asset_format)`` for downloaded asset bytes.

    Lived in two modules with two slightly different rules, and the two suites
    that guard it each covered only one — so one copy could regress green. The
    magic bytes win over the advertised format: an advertised type is what
    Telegram says, and the bytes are what arrived.
    """
    if raw[:2] == _GZIP_MAGIC:
        return ".tgs", "lottie_tgs"
    if mime_format == "video":
        return ".webm", "video"
    if mime_format == "static_image":
        return ".webp", "static_image"
    return ".webm", "video"


class Catalog:
    """One fetched snapshot of the effect catalog, for one account."""

    __slots__ = (
        "hash",
        "effects",
        "documents",
        "fetched_at",
        "generation",
        "checked_epoch",
        "unknown_ids",
    )

    def __init__(self, hash_: int, effects: dict, documents: dict, fetched_at=0.0, generation=0):
        self.hash = hash_
        self.effects = effects
        self.documents = documents
        self.fetched_at = fetched_at
        self.generation = generation
        # Which check this snapshot was last confirmed by. A caller records the
        # value it saw and hands it back, so a check that finished while it waited
        # is reused instead of repeated.
        self.checked_epoch = 0
        # IDs this exact snapshot does not contain. Asking Telegram again for the
        # same ID against the same catalogue gets the same answer, so the negative
        # result rides along with the snapshot and dies with it — a new payload
        # builds a new Catalog, and its set starts empty.
        self.unknown_ids = set()

    def is_fresh(self, now: float) -> bool:
        """Whether this snapshot may be served without asking Telegram again."""
        return (now - self.fetched_at) < _REVALIDATE_AFTER_SECONDS

    def remember_unknown(self, effect_id: int) -> None:
        """Record that this snapshot does not contain ``effect_id``.

        The bound lives here, on the addition, rather than on the revalidation
        that confirms the snapshot: the ID comes from tool input, so the set does
        need a ceiling, but a "not modified" answer is the one event that PROVES
        every recorded miss is still a miss. Clearing there made the cache hold
        at most one ID — the freshness window restarted on the same object each
        time — so 50 lookups of 50 dead IDs cost 50 round trips.
        """
        if len(self.unknown_ids) >= MAX_UNKNOWN_IDS:
            # ponytail: crude reset rather than an LRU. The set only exists to
            # save a round trip, so the worst case of a reset is the cost we
            # already pay on a cold cache. Swap in an LRU if that ever shows up.
            self.unknown_ids.clear()
        self.unknown_ids.add(effect_id)


class _AccountState:
    __slots__ = ("catalog", "lock", "generation", "check_epoch")

    def __init__(self):
        self.catalog = None
        # Serialises fetches for this account: without it two concurrent
        # inspections both see an empty cache and both pull the whole catalogue.
        self.lock = asyncio.Lock()
        # Advances only when a new payload arrives — it means "these documents and
        # file references are current", which is what a stale-reference refresh
        # needs to reason about.
        self.generation = 0
        # Advances after any completed check, including "not modified". A
        # revalidation that changes nothing still answers the question, and
        # without a counter for that the second of two waiting callers sees an
        # unchanged generation and asks Telegram the very same thing again.
        self.check_epoch = 0


_states = {}


def account_key(account: Optional[str]) -> str:
    """The identity ``get_client`` resolves to, so cache and client cannot drift.

    ``runtime.get_client`` lowercases an explicit label and, with a single account
    configured, ignores the argument entirely — so ``"SECOND"``, ``"second"`` and
    (in single-account mode) ``None`` all reach one client. Keying on the raw
    string gave that one client two caches, and therefore two sets of file
    references for the same session.

    It lives here rather than in ``runtime.py`` because that file is upstream's
    and stays untouched; the rule is mirrored from it deliberately, and the tests
    pin the two together.
    """
    from telegram_mcp import runtime

    clients = getattr(runtime, "clients", None) or {}
    if account is None:
        # Single-account mode has exactly one identity and it is not "default":
        # an explicit call using that label must land on the same cache.
        return next(iter(clients)) if len(clients) == 1 else "default"
    return account.lower()


def _state(account: Optional[str]) -> _AccountState:
    key = account_key(account)
    if key not in _states:
        _states[key] = _AccountState()
    return _states[key]


def _reset_catalog() -> None:
    """Drop every account's cached catalog. For tests."""
    _states.clear()


def cached_catalog(account: Optional[str] = None) -> Optional[Catalog]:
    """The catalog currently held for an account, without fetching one."""
    return _state(account).catalog


async def _fetch(state: _AccountState, cl, known_hash: int, now: float) -> Catalog:
    """One request and the bookkeeping for what comes back. Caller holds the lock."""
    result = await cl(functions.messages.GetAvailableEffectsRequest(hash=known_hash))
    state.check_epoch += 1

    # Not modified: Telegram sends no payload at all, so the cache is the only
    # copy in existence. Its generation does not change — the content did not —
    # but the check did happen, which is what the epoch records.
    if not hasattr(result, "effects"):
        if state.catalog is not None:
            state.catalog.fetched_at = now
            state.catalog.checked_epoch = state.check_epoch
            # The recorded misses deliberately SURVIVE: "not modified" is proof
            # that the content behind them has not changed, so re-asking about the
            # same ID would buy the same answer. Their ceiling is enforced in
            # remember_unknown instead.
            return state.catalog
        # "Unchanged" against a hash we do not hold. Nothing to serve, and
        # repeating the same hash would repeat the answer.
        result = await cl(functions.messages.GetAvailableEffectsRequest(hash=0))
        state.check_epoch += 1
        if not hasattr(result, "effects"):
            # Defensive: "not modified" against hash=0 is not documented behaviour,
            # but the fall-through below would store an empty catalogue with a fresh
            # timestamp, and is_fresh would then serve "0 effects" as authoritative
            # for an hour — every lookup confidently reporting a retired effect.
            raise RuntimeError(
                "Telegram answered 'not modified' to a hash=0 effect catalogue request, "
                "so there is no catalogue to cache."
            )

    state.generation += 1
    state.catalog = Catalog(
        getattr(result, "hash", 0),
        {effect.id: effect for effect in getattr(result, "effects", [])},
        {doc.id: doc for doc in getattr(result, "documents", [])},
        now,
        state.generation,
    )
    state.catalog.checked_epoch = state.check_epoch
    return state.catalog


async def load_catalog(cl, account: Optional[str] = None):
    """``(catalog, contacted_telegram)`` for an account, honouring the window.

    The flag matters to the caller: a catalogue this very call just fetched is
    already the freshest Telegram has, so there is nothing to gain by asking again
    when a lookup misses.
    """
    state = _state(account)
    async with state.lock:
        now = time.monotonic()
        if state.catalog is not None and state.catalog.is_fresh(now):
            return state.catalog, False
        known_hash = state.catalog.hash if state.catalog is not None else 0
        return await _fetch(state, cl, known_hash, now), True


async def revalidate_catalog(
    cl, account: Optional[str], seen: Catalog, seen_epoch: int
) -> Catalog:
    """Bypass the window but keep the hash: has anything changed since ``seen``?

    Costs a round trip and, when nothing changed, no payload. Deduplication turns
    on ``seen_epoch``, not on the generation: a revalidation that answers "not
    modified" deliberately leaves the generation alone, so a second waiting caller
    would find it unchanged and ask Telegram exactly the same question again. The
    epoch records that the question was asked and answered, which is what a burst
    of unknown-ID lookups needs to collapse into one request.
    """
    state = _state(account)
    async with state.lock:
        current = state.catalog
        if current is not None and (
            current.generation > seen.generation or current.checked_epoch > seen_epoch
        ):
            return current
        known_hash = current.hash if current is not None else 0
        return await _fetch(state, cl, known_hash, time.monotonic())


async def refresh_catalog(cl, account: Optional[str], seen: Catalog) -> Catalog:
    """Pay for the whole catalogue again — the only cure for a stale file reference.

    ``hash=0`` because a reference is refreshed only by refetching the object that
    carries it, and Telegram sends no documents when it answers "not modified".
    Skipped entirely when another task already refreshed past ``seen``: several
    downloads failing on the same stale snapshot must not each buy a new one.
    """
    state = _state(account)
    async with state.lock:
        if state.catalog is not None and state.catalog.generation > seen.generation:
            return state.catalog
        return await _fetch(state, cl, 0, time.monotonic())


def describe_document(document, label: str) -> Optional[dict[str, Any]]:
    """What an effect asset is, before deciding whether to pay for it."""
    if document is None:
        return None
    mime = getattr(document, "mime_type", None)
    info = {
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


def _referenced(catalog: Catalog, document_id, label: str) -> Optional[dict[str, Any]]:
    """A referenced document, or an explicit marker that the catalogue lacked it.

    Returning ``None`` for both "no id" and "an id we could not resolve" throws
    away the one fact worth keeping about the second case: which document was
    named.
    """
    if not document_id:
        return None
    document = catalog.documents.get(document_id)
    if document is None:
        return {
            "asset": label,
            "document_id": document_id,
            "unresolved": True,
            "note": (
                "The effect names this document but the catalogue Telegram returned did not "
                "include it. That is an inconsistency in the catalogue, not a missing asset."
            ),
        }
    return describe_document(document, label)


def is_unresolved(reference) -> bool:
    return bool(reference) and bool(reference.get("unresolved"))


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

    icon_id = getattr(effect, "static_icon_id", None)
    sticker_id = getattr(effect, "effect_sticker_id", None)
    animation_id = getattr(effect, "effect_animation_id", None)
    icon = _referenced(catalog, icon_id, "static_icon")
    sticker = _referenced(catalog, sticker_id, "preview_sticker")
    animation = _referenced(catalog, animation_id, "effect_animation")

    # Telegram's rule when there is no static icon: the emoticon *is* the preview
    # icon. That applies to an absent ID only. An ID the catalogue failed to
    # resolve is a different thing, and calling it "the emoticon fallback" would
    # report a Telegram decision Telegram never made.
    if not icon_id:
        icon_source = "emoticon"
    elif is_unresolved(icon):
        icon_source = "unresolved_reference"
    else:
        icon_source = "static_icon"

    info = {
        "effect_id": effect.id,
        "emoticon": getattr(effect, "emoticon", None),
        "premium_required": bool(getattr(effect, "premium_required", False)),
        "icon_source": icon_source,
        "static_icon": icon,
        "preview_sticker": sticker,
        "effect_animation": animation,
    }

    if is_unresolved(animation):
        # Do not quietly fall back to the sticker: the effect *has* an animation,
        # and substituting a different asset would hide the fault.
        info["animation_source"] = "unresolved_reference"
    elif animation is not None:
        info["animation_source"] = "effect_animation"
    else:
        sticker_document = catalog.documents.get(sticker_id)
        video_size = premium_effect_size(sticker_document)
        if video_size is not None:
            info["animation_source"] = "premium_effect_of_preview_sticker"
            fallback = {
                "asset": "premium_effect_of_preview_sticker",
                "document_id": getattr(sticker_document, "id", None),
                "thumb_type": "f",
                "size_bytes": getattr(video_size, "size", None),
            }
            for field, key in (("w", "width"), ("h", "height")):
                value = getattr(video_size, field, None)
                if value is not None:
                    fallback[key] = value
            info["effect_animation"] = fallback
        elif is_unresolved(sticker):
            info["animation_source"] = "unresolved_reference"
        else:
            info["animation_source"] = "none"

    return info
