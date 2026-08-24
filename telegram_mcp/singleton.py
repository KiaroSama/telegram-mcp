"""Per-session file lock guarding against concurrent Telegram connections.

MCP clients (Claude Desktop in particular) sometimes spawn more than one
instance of this server for the same configured session -- most commonly
when a connector is restarted and the old process hasn't exited yet before
the new one starts. Two processes calling ``TelegramClient.connect()`` /
``start()`` with the same auth key at the same time trips Telegram's abuse
protection (``AuthKeyDuplicatedError``), which can knock out *both*
connections rather than just the newcomer.

This module provides an OS-level advisory file lock (``flock`` on POSIX,
``msvcrt.locking`` on Windows) keyed by the session's actual identity (its
string value or canonical file path) and by NOTHING else. The account label
is deliberately absent from the lock name: one StringSession configured under
two labels is still one Telegram auth key, and a label-qualified lock handed
each label its own file, let both connect, and produced exactly the
``AuthKeyDuplicatedError`` this module exists to prevent. The lock is held for
the lifetime of the process and is released automatically by the OS if the
process exits or is killed, so there is no stale-lock file to clean up.

A second instance racing for the same session waits briefly (covers the
"restart replaces the old process" case) and, if the lock is still held once
the grace period elapses, raises :class:`SessionLockError` instead of ever
calling ``connect()`` -- so the live session is never disturbed.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
from pathlib import Path
from typing import IO, Optional

if os.name == "nt":
    import msvcrt

    def _try_lock(fh: IO) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fh: IO) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh: IO) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# Public names for the primitives above. The session pool in ``runtime`` needs
# the same "try to take an exclusive lock, don't block" behaviour on both
# platforms, and duplicating the msvcrt/fcntl split there is how one of the two
# copies ends up POSIX-only.
try_lock_exclusive = _try_lock
release_lock = _unlock


DEFAULT_LOCK_DIR = Path(tempfile.gettempdir()) / "telegram-mcp-locks"
DEFAULT_GRACE_SECONDS = 20.0
DEFAULT_POLL_INTERVAL = 0.5


class SessionLockError(RuntimeError):
    """Raised when a session lock isn't acquired before its grace period elapses."""


class SessionLock:
    """Exclusive, OS-released lock for one Telegram session, held for process lifetime."""

    def __init__(self, session_identity: str, *, lock_dir: Path = DEFAULT_LOCK_DIR):
        digest = hashlib.sha256(session_identity.encode("utf-8")).hexdigest()[:16]
        lock_dir.mkdir(parents=True, exist_ok=True)
        # No label in the name: same auth key -> same file -> one holder.
        self.path = lock_dir / f"session-{digest}.lock"
        self._fh: Optional[IO] = None

    def acquire(
        self,
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """Block (up to ``grace_seconds``) until the lock is free, then take it.

        Raises :class:`SessionLockError` if another live process still holds
        the lock once the grace period elapses.
        """
        # Validated before anything is opened or slept on: with ``nan`` the
        # deadline comparison below is never true and the wait never ends.
        for name, value in (("grace_seconds", grace_seconds), ("poll_interval", poll_interval)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative number, got {value!r}")

        fh = open(self.path, "a+")
        deadline = time.monotonic() + grace_seconds
        while True:
            if _try_lock(fh):
                self._fh = fh
                return
            if time.monotonic() >= deadline:
                fh.close()
                raise SessionLockError(
                    "Another telegram-mcp process is already connected with this "
                    f"session (lock held: {self.path}). Refusing to connect a "
                    "second time to avoid Telegram's AuthKeyDuplicatedError. If "
                    "that other process already exited, this lock will clear on "
                    "its own -- retry."
                )
            time.sleep(poll_interval)

    def release(self) -> None:
        if self._fh is not None:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None


def session_identity(client: object) -> str:
    """Derive a stable identity string for a ``TelegramClient``'s session.

    File-based sessions key on the CANONICAL file path -- ``realpath`` so a
    symlink alias resolves to the file it points at, ``normcase`` so a
    case-differing Windows spelling of the same file is the same identity;
    ``abspath`` alone let both buy a second lock on one session. String
    sessions key on their serialized value (same auth key -> same identity,
    regardless of which process or working directory loaded it). Falls back to
    the client's object id for sessionless test doubles.
    """
    session = getattr(client, "session", None)
    if session is not None:
        filename = getattr(session, "filename", None)
        if filename:
            return f"file:{os.path.normcase(os.path.realpath(filename))}"
        save = getattr(session, "save", None)
        if callable(save):
            try:
                saved = save()
            except Exception:
                saved = None
            if saved:
                return f"string:{saved}"
    return f"anon:{id(client)}"
