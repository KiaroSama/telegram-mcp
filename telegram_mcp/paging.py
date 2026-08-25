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
    "list_photos": 100,  # ids and dates only; the images are fetched separately
    # A sheet is bounded by its own geometry, not by record cost: 24 tiles at 4
    # columns is already the largest grid worth sending as one image block.
    "get_photo_sheet": 24,
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


# Timing arguments: the same problem one unit over. A count is "how many"; these
# are "how long", and an unchecked one buys an unbounded WAIT rather than an
# unbounded fetch. ``timeout=inf`` produced a deadline no comparison ever reached
# and ``asyncio.wait_for(..., timeout=inf)`` under it, so only cancellation from
# outside ever ended the call; ``nan`` is worse, because the reply still claims a
# bound.
#
# Two differences from ``bounded`` above, both deliberate:
#
# * a FLOOR as well as a ceiling, because the invalid end is not always zero.
#   ``lock_grace_seconds=0`` means "do not wait", which is a real answer;
#   ``lock_poll_interval=0`` is a busy-spin dressed up as a wait.
# * out of range is REFUSED, not clamped. Silently turning a caller's 1e18 into
#   300 answers a question nobody asked, and the caller cannot tell which.
#
# name -> (minimum, maximum), in the unit the argument is named for.
TIMING_BOUNDS: dict[str, tuple[float, float]] = {
    # Seconds an MCP call may block. Past five minutes the client has given up on
    # the request long before the server has.
    "timeout": (0.1, 300.0),
    # Milliseconds of quiet that ends a burst. Below 50ms nothing debounces;
    # past five minutes a burst is not a burst.
    "settle_ms": (50.0, 300_000.0),
    # Milliseconds one debounced wait may block, same ceiling as `timeout`.
    "max_wait_ms": (100.0, 300_000.0),
    # Seconds to wait for another process to release a session lock. Zero is
    # valid and means "fail immediately if it is held".
    "lock_grace_seconds": (0.0, 300.0),
    # Seconds between lock attempts. Zero spins a core for the whole grace
    # period; a minute is longer than any grace period worth polling.
    "lock_poll_interval": (0.01, 60.0),
}


class Span(NamedTuple):
    """A validated duration, or the refusal explaining why there is not one."""

    value: float
    error: Optional[str]


def _as_number(value: Any) -> Optional[float]:
    """``value`` as a finite float, or ``None`` if it is not one.

    ``bool`` is refused for the reason ``_as_int`` refuses it: ``True`` is an
    ``int``, and a flag arriving where a duration belongs is a mistake worth
    naming rather than reading as one second.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def bounded_number(value: Any, name: str) -> Span:
    """Validate one duration against its entry in :data:`TIMING_BOUNDS`.

    ``name`` is the lookup key as well as the label in the refusal, so a tool
    cannot quietly validate against bounds belonging to a different argument.
    """
    try:
        minimum, maximum = TIMING_BOUNDS[name]
    except KeyError:
        raise ValueError(f"no timing bounds are declared for {name!r}") from None

    number = _as_number(value)
    if number is None:
        return Span(
            minimum,
            f"Error: {name} must be a finite number, not {value!r}. Infinity and NaN are "
            "refused because neither ends a wait - every comparison against NaN is false, "
            "so the call would claim a bound it does not have. Nothing was started.",
        )
    if number < minimum or number > maximum:
        return Span(
            minimum,
            f"Error: {name} must be between {minimum:g} and {maximum:g}; {number:g} was "
            "given. Nothing was started.",
        )
    return Span(number, None)
