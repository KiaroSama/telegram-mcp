"""One rule for every "how many" argument a tool takes.

A limit is not a display preference. It is multiplied by a network round trip on
the way in and by a serialized record on the way out, so an unchecked one buys
unbounded RPC work and unbounded context in the same call — and the tools were
not consistent about it: some clamped, some passed whatever arrived straight into
``iter_messages`` and then into ``json.dumps``. Zero and negative values were the
worse half, because Telethon reads a non-positive limit as "no limit".

Nothing here talks to Telegram or to MCP, so the rules are testable on their own.

Three things this refuses rather than interprets:

* **bool** — ``True`` is an ``int`` in Python, and ``limit=True`` meaning 1 is a
  coincidence, not an intention.
* **NaN and infinity** — every comparison against NaN is False, so a bound tested
  with ``>`` silently does not exist. That is worse than having no bound, because
  the reply still claims one.
* **a non-integral float** — 2.5 records is not a number of records.

A value above the ceiling is *clamped*, not refused: the caller asked a
well-formed question and a smaller honest answer beats an error. What it must not
do is arrive silently, so the reply carries what was asked for beside what was
served.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple, Optional

# Ceilings per tool, by the name the tool is registered under. A tool that takes a
# count has to appear here — `tests/test_pagination.py` walks the live registry and
# fails on one that does not, so a new list tool cannot ship without a number.
#
# The numbers are not one global constant on purpose: 200 forum topics is a cheap
# single request, 200 user photos is 200 downloadable objects, and pretending
# those cost the same is how a "reasonable" default becomes unreasonable.
LIMITS: dict[str, int] = {
    # Messages: the heaviest records here, each with text, media and sender.
    "list_messages": 200,
    "get_history": 200,
    "get_messages": 200,
    "search_messages": 200,
    "search_global": 100,
    "get_saved_history": 100,  # Telegram's own cap on messages.getSavedHistory
    "inspect_messages": 50,  # every entity, reaction and media field per message
    "get_message_context": 25,  # taken twice, before and after
    # Chat and dialog listings: one record per chat, cheap but numerous.
    "get_chats": 200,
    "list_chats": 200,
    "list_saved_dialogs": 100,
    "search_public_chats": 100,
    "list_topics": 200,
    "get_common_chats": 100,  # Telegram caps messages.getCommonChats at 100
    "get_participants": 1000,  # a member list is the one place four figures is normal
    # Per-message detail: a request each, so the ceiling is what one screenful of
    # reasoning can actually use.
    "get_poll_voters": 200,
    "get_message_reactions": 200,
    "get_user_photos": 100,
    "list_disappearing_media": 200,
    "get_gif_search": 50,
    # Pending incoming bursts held in memory, not fetched from Telegram.
    "wait_for_new_message": 100,
}

# The furthest into a listing an offset may point. Page arithmetic is unbounded in
# Python — `(page - 1) * page_size` for page=10**18 does not overflow, it just
# produces a number Telegram is asked to skip past — so the bound has to be
# explicit. Beyond this a caller wants a search, not a page number.
MAX_OFFSET = 100_000


class Bound(NamedTuple):
    """The answer to "how many", plus what to tell the caller about it."""

    value: int
    requested: int
    ceiling: int
    error: Optional[str]

    @property
    def clamped(self) -> bool:
        return self.error is None and self.value != self.requested

    @property
    def metadata(self) -> dict[str, Any]:
        """What the reply says about the count it actually served.

        Always both numbers, even when they agree: a caller that has to infer
        whether it was trimmed by comparing lengths cannot tell a clamp from a
        chat that simply had fewer messages.
        """
        described: dict[str, Any] = {
            "requested_limit": self.requested,
            "effective_limit": self.value,
        }
        if self.clamped:
            described["limit_note"] = (
                f"Asked for {self.requested}; this tool serves at most {self.ceiling}, so "
                f"{self.value} were requested from Telegram. There may be more beyond them."
            )
        return described


def _as_int(value: Any) -> Optional[int]:
    """``value`` as a whole number, or ``None`` if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def bounded(value: Any, ceiling: int, name: str = "limit") -> Bound:
    """Validate one count against ``ceiling``.

    Returns a :class:`Bound` whose ``error`` is a refusal to hand straight back to
    the caller, or ``None`` when the call may proceed with ``value``.
    """
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
        raise ValueError(f"ceiling for {name} must be a positive integer, got {ceiling!r}")

    requested = _as_int(value)
    if requested is None:
        return Bound(
            ceiling,
            0,
            ceiling,
            f"Error: {name} must be a whole number, not {value!r}. Nothing was fetched.",
        )
    if requested < 1:
        return Bound(
            ceiling,
            requested,
            ceiling,
            f"Error: {name} must be at least 1; {requested} was given. Telegram reads a "
            f"non-positive limit as 'no limit', which is the opposite of what it looks "
            f"like. This tool serves at most {ceiling}. Nothing was fetched.",
        )
    return Bound(min(requested, ceiling), requested, ceiling, None)


def bounded_page(page: Any, page_size: Any, ceiling: int) -> tuple[Bound, Optional[int]]:
    """``(page_size bound, offset)`` for a page-numbered listing.

    The offset is the second half of the same problem: a validated page_size with
    an unvalidated page number still asks Telegram to skip past an arbitrary
    number of records. ``None`` as the offset means the bound carries the refusal.
    """
    size = bounded(page_size, ceiling, name="page_size")
    if size.error:
        return size, None

    number = _as_int(page)
    if number is None or number < 1:
        return (
            Bound(
                size.value,
                size.requested,
                ceiling,
                f"Error: page must be a whole number from 1 upwards, not {page!r}. "
                "Nothing was fetched.",
            ),
            None,
        )

    offset = (number - 1) * size.value
    if offset > MAX_OFFSET:
        return (
            Bound(
                size.value,
                size.requested,
                ceiling,
                f"Error: page {number} at page_size {size.value} starts {offset} records in, "
                f"past the {MAX_OFFSET}-record paging limit. Paging that deep costs a full "
                "scan for every page; narrow the request with a search or a date range "
                "instead. Nothing was fetched.",
            ),
            None,
        )
    return size, offset


def page_metadata(bound: Bound, page: int, offset: int, returned: int) -> dict[str, Any]:
    """Where this page sat, and whether asking for the next one is worth it."""
    described = dict(bound.metadata)
    described.update(
        {
            "page": page,
            "offset": offset,
            "returned": returned,
            # `returned == effective_limit` is the only honest "maybe more": a
            # short page means the listing ran out, and saying otherwise sends the
            # caller after a page that does not exist.
            "has_more": returned >= bound.value,
        }
    )
    return described


def bounded_slice(records: list, bound: Bound) -> tuple[list, dict[str, Any]]:
    """The first ``bound.value`` of ``records``, and what was left behind.

    For listings served from memory rather than fetched, where the limit cannot be
    pushed down into the request and truncation is the only place it can happen.
    """
    served = records[: bound.value]
    described = dict(bound.metadata)
    described.update(
        {
            "total": len(records),
            "returned": len(served),
            "has_more": len(served) < len(records),
        }
    )
    return served, described
