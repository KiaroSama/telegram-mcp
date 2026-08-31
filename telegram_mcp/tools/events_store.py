"""Where incoming events are kept, and the ceilings that stop them growing.

Two stores sit behind :mod:`telegram_mcp.tools.events`: the in-memory map of
bursts nobody has collected yet, and the JSONL feed file the callback consumer
appends to. Both are owned here, and the tools module reaches them through this
module (``events_store._pending_msgs``) rather than importing the names -- a
second binding of a rebindable global is two pending maps that agree right up
until something rebinds one of them, and then diverge in silence.

Nothing here imports the MCP runtime or Telethon. A ceiling is a decision about
size and age: it needs an environment variable and a clock, not a client.
"""

import logging
import math
import os
import stat
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from telegram_mcp.owner_only import restrict_to_owner_strict, verify_owner_only
from telegram_mcp.safe_log import log_event

# (account_label, chat_id) -> {first_ts, last_ts, count, first_id, last_id, name,
# username, account}
#
# Keyed by account as well as chat: a marked chat id is only unique WITHIN a
# login. The same integer names a different conversation on each account, so a
# chat-id-only key merged two people into one burst and let whichever account
# answered first consume the other's messages.
_pending_msgs: Dict[tuple[str, int], Dict[str, Any]] = {}

_FEED_FILE_ENV = "TELEGRAM_EVENT_FEED_FILE"

# --- what stops any of this from growing forever -----------------------------
#
# Three separate leaks share one cause: a server that runs for weeks was never
# told when to stop keeping something. The feed file was append-only with a
# comment saying to rotate it by hand; the pending map had no count and no
# expiry, so a conversation nobody collected stayed resident for the life of the
# process; and the wait serialized however many chats happened to be in it, which
# is the model's context rather than the machine's memory but leaks just the
# same. Every number below is a ceiling with a default, overridable by
# environment, and validated -- an unusable override falls back rather than
# silently removing the ceiling it was meant to set.
_FEED_MAX_BYTES_DEFAULT = 8 * 1024 * 1024
_FEED_MAX_AGE_SECONDS_DEFAULT = 7 * 24 * 60 * 60
_PENDING_MAX_DEFAULT = 500
_PENDING_TTL_SECONDS_DEFAULT = 60 * 60

# Enough dropped bursts to see a pattern, few enough that the ledger reporting
# the leak is not itself one.
_DROP_LEDGER_MAX = 20


def _positive_env(name: str, default: int) -> int:
    """A whole positive number from the environment, or ``default``.

    ``nan`` is the value worth naming: every comparison against it is False, so a
    size ceiling set to it does not fire and the status still reports a ceiling.
    That is worse than no bound at all. ``inf`` disables it honestly but
    pointlessly, and zero or a negative fires on everything.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value < 1:
        log_event(
            logging.WARNING,
            "event-limit-unusable",
            setting=name,
            supplied=raw,
            using=default,
        )
        return default
    return int(value)


def feed_retention() -> Tuple[int, int]:
    """``(max_bytes, max_age_seconds)`` for the feed file."""
    return (
        _positive_env("TELEGRAM_EVENT_FEED_MAX_BYTES", _FEED_MAX_BYTES_DEFAULT),
        _positive_env("TELEGRAM_EVENT_FEED_MAX_AGE_SECONDS", _FEED_MAX_AGE_SECONDS_DEFAULT),
    )


def pending_bounds() -> Tuple[int, int]:
    """``(max_pending_chats, ttl_seconds)`` for the un-collected burst map."""
    return (
        _positive_env("TELEGRAM_EVENT_PENDING_MAX", _PENDING_MAX_DEFAULT),
        _positive_env("TELEGRAM_EVENT_PENDING_TTL_SECONDS", _PENDING_TTL_SECONDS_DEFAULT),
    )


def _new_drop_ledger() -> Dict[str, Any]:
    return {"total": 0, "reasons": {}, "recent": deque(maxlen=_DROP_LEDGER_MAX)}


# A dropped burst is a message the agent will never answer. Once a ceiling is
# reached, losing one may be unavoidable; losing it quietly is not, so every drop
# is counted and the most recent few are named.
_dropped: Dict[str, Any] = _new_drop_ledger()


def _record_drop(key: tuple, reason: str) -> None:
    account, chat_id = key
    _dropped["total"] += 1
    _dropped["reasons"][reason] = _dropped["reasons"].get(reason, 0) + 1
    _dropped["recent"].append(
        {"account": account, "chat_id": chat_id, "reason": reason, "at": round(time.time(), 2)}
    )


def overflow_state() -> Dict[str, Any]:
    """What was dropped, and why. Reported by the waits and by the status tool."""
    return {
        "dropped_total": _dropped["total"],
        "dropped_reason_counts": dict(_dropped["reasons"]),
        "recent_dropped": list(_dropped["recent"]),
    }


def _expire_pending() -> None:
    """Forget bursts nobody came for. Called from the waits and the feed loop, so
    the map shrinks on a quiet server too, not only when a message arrives."""
    _max_count, ttl = pending_bounds()
    cutoff = time.monotonic() - ttl
    for key in [key for key, rec in _pending_msgs.items() if rec["last_ts"] < cutoff]:
        _pending_msgs.pop(key, None)
        _record_drop(key, "expired")


def _enforce_pending_ceiling() -> None:
    """Drop least-recently-active chats until the map fits.

    Oldest first: the newest message is the one an agent still has a chance of
    answering usefully.
    """
    max_count, _ttl = pending_bounds()
    while len(_pending_msgs) > max_count:
        oldest = min(_pending_msgs, key=lambda key: _pending_msgs[key]["last_ts"])
        _pending_msgs.pop(oldest, None)
        _record_drop(oldest, "overflow")


def _default_feed_file() -> Path:
    """Runtime data location, never the install directory.

    The package may live in a read-only site-packages or container layer, so the
    default feed path follows the XDG state convention instead of `__file__`.
    """
    base = os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "telegram-mcp" / "incoming_feed.jsonl"


def feed_file_path() -> Path:
    override = os.getenv(_FEED_FILE_ENV)
    return Path(override) if override else _default_feed_file()


def _rotated_feed_path(path: Path) -> Path:
    """The one retained generation. One, not a numbered series: a series is the
    same unbounded growth with more filenames."""
    return path.with_name(path.name + ".1")


def _same_object(path, fd: int) -> bool:
    """Whether ``path`` still names the object behind ``fd``.

    ``st_dev``/``st_ino`` are the file's identity on both platforms - on Windows
    CPython fills them from the volume serial number and the NTFS file index, and
    an unlink-and-recreate produces a different index. Comparing them is how a
    permission change on a NAME is tied to the object actually held open.
    """
    try:
        by_name, by_handle = os.stat(path), os.fstat(fd)
    except OSError:
        return False
    return (by_name.st_dev, by_name.st_ino) == (by_handle.st_dev, by_handle.st_ino)


def _restrict_to_owner(path, fd: Optional[int] = None) -> None:
    """Make ``path`` readable by its owner alone, and prove it about the object.

    The feed holds contact names, usernames and chat ids, so this is a privacy
    control and it fails closed: a caller who cannot be given a private file is
    told, rather than handed a world-readable one.

    Two things changed here, both about identity rather than about permissions.

    ``icacls`` is gone. `icacls /inheritance:r /grant:r <user>:(F)` drops the
    INHERITED entries and replaces the entry for the principal it names, and
    touches no other explicit entry - so a file already carrying `Everyone:(R)`
    kept it while the command exited 0. :mod:`telegram_mcp.owner_only` writes the
    whole DACL and then reads it back off the object, which is the difference
    between evidence about a call and evidence about a file. It costs no
    subprocess either, so it can run on every open instead of once per pathname.

    The pathname cache is gone with it, and that was the actual leak: it recorded
    that a NAME had been hardened. Replace the file at that name - an external
    rotation, an editor writing new-then-rename, anything hostile - and the new
    object was treated as already private and never touched. The check is now the
    file's own state plus :func:`_same_object`, so the answer cannot be inherited
    by a different file that happens to share a name.
    """
    if os.name == "posix":
        if fd is not None:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            return
        os.chmod(path, 0o600)
        return

    key = str(path)
    if fd is not None and not _same_object(path, fd):
        raise OSError(
            f"cannot restrict {key} to its owner: the name no longer refers to the file "
            "that was opened, so hardening it would report success about a different object."
        )
    if not verify_owner_only(path) and not restrict_to_owner_strict(path):
        raise OSError(f"cannot restrict {key} to its owner: its DACL still names other accounts.")
    # Re-checked after the write, not before: on Windows the replacement can land
    # between the two calls, and a DACL applied to the wrong object is worth an
    # error rather than a reassurance.
    if fd is not None and not _same_object(path, fd):
        raise OSError(
            f"cannot restrict {key} to its owner: the file was replaced while it was "
            "being hardened."
        )


def _rotate_feed_if_needed(path: Path) -> None:
    """Keep the feed inside its size and age budget, before anything is appended.

    Checked at open time rather than mid-write, so a file can carry the one
    record that took it over its ceiling. Two generations of that is the bound.
    """
    max_bytes, max_age = feed_retention()
    rotated = _rotated_feed_path(path)

    try:
        if time.time() - rotated.stat().st_mtime > max_age:
            rotated.unlink()
    except OSError:
        pass  # no rotated generation, or it went away underneath us

    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return  # nothing to rotate yet

    try:
        # os.replace, not a copy: atomic, and it drops the previous generation in
        # the same step rather than leaving a window with three of them.
        os.replace(path, rotated)
    except OSError as error:
        log_event(logging.ERROR, "event-feed-rotate-failed", error=error)
        return
    # Both names now refer to different files than they did; the retained
    # generation carries the same private records, so it is hardened as itself.
    _restrict_to_owner(rotated)


def _open_feed_append():
    """Append-open the feed file, owner-only — it holds private contact metadata.

    `Path.touch(mode=...)` only applies its mode when creating, so an existing or
    externally rotated file keeps whatever permissions it had; the restriction is
    therefore applied on every open rather than only on creation.
    """
    path = feed_file_path()
    if not os.getenv(_FEED_FILE_ENV):
        # Only auto-create the directory we own; an explicit path must exist so a
        # typo fails loudly instead of scattering directories.
        path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_feed_if_needed(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _restrict_to_owner(path, fd)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")


def _touch_feed_file() -> None:
    """Create (or re-restrict, and rotate) the feed file before the consumer starts."""
    _open_feed_append().close()


# Only the four names the rest of the package reads. `__all__` here PARTITIONS
# with events.py's rather than overlapping it: `tools/__init__.py` star-imports
# both, and a name exported twice is one module silently shadowing the other.
__all__ = [
    "feed_file_path",
    "feed_retention",
    "pending_bounds",
    "overflow_state",
]
