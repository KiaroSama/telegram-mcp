"""Session files on disk, and the protection they must carry before use.

Split out of ``connection.py``, which was holding two jobs: reaching Telegram,
and looking after the credential that lets it. This is the second one.

A ``.session`` file IS a live login. So nothing here trusts a filename: the
sidecars (``-journal``, ``-wal``, ``-shm``) are hardened alongside the database
because SQLite writes the same secret into them, a client whose files could not
be made owner-only is CLOSED rather than used, and ``adopt_legacy_session``
moves an older install's file into place instead of leaving two copies of one
login on disk.

``_build_client`` lives here rather than upstairs because constructing a client
is the moment a session file is opened - the hardening and the construction are
one decision, and separating them is how a client ends up running over a
world-readable session.

Re-exported from ``connection`` so nothing's import moves.
"""

import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from telethon import TelegramClient

from telegram_mcp.aliases import restrict_to_owner
from telegram_mcp.owner_only import verify_owner_only
from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
)
from telegram_mcp.settings import state_dir

# Where log records go, and what they may contain, now lives next door: it is a
# different job from reaching Telegram, and this file was carrying both. The
# names are re-exported because `safe_log` and several tests import them from
# here, and moving code should not move anyone's import.
from telegram_mcp.log_setup import (  # noqa: F401  (re-exported)
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    RedactingFilter,
    _make_file_handler,
    _OwnerOnlyRotatingFileHandler,
    _secret_env_values,
    console_handler,
    log_file_path,
    redact,
)

# Proxy configuration moved next door: turning TELEGRAM_PROXY_* into Telethon
# kwargs never touches a socket or a session, and this file was carrying both
# jobs. Re-exported for the same reason log_setup's names are - `runtime` star
# imports this module and `runner` imports parse_port from it.
from telegram_mcp.proxy import (  # noqa: F401  (re-exported)
    _PROXY_TYPES_ALL,
    _PROXY_TYPES_SOCKS_HTTP,
    _build_proxy_for_label,
    _get_proxy_env,
    parse_port,
)

package_dir = os.path.dirname(os.path.abspath(__file__))


script_dir = os.path.dirname(package_dir)


_SESSION_SIDECARS = ("", "-journal", "-wal", "-shm")


class SessionNotProtected(RuntimeError):
    """A session file could not be made owner-only, so its account is not started."""


_UNPROTECTED_SESSION_MESSAGE = (
    "The Telegram session file for this account could not be made readable by its owner "
    "alone, so the account was not started -- whoever can read that file is signed in as "
    "this account, with no password and no second factor. Either set TELEGRAM_SESSION_NAME "
    "to a bare name so the session lives in the server's own private state directory, or "
    "make the directory you chose readable by your account alone."
)


def session_file_path(name: str) -> Path:
    """Where a file-based session lives.

    A bare name goes in the private state directory, beside the alias store and
    the log: not in the git checkout, not wherever the client happened to spawn
    the server from, and in a directory this module owns and can keep private.
    An explicit path is honoured where the operator put it.

    A session left beside the installation or in the working directory by an
    older version is no longer answered with its old location. Those directories
    cannot be made private without stripping the permissions off everything else
    in them -- measured, on this project's own checkout -- so the account is
    moved instead, once, by :func:`adopt_legacy_session`.
    """
    candidate = Path(name)
    stem = candidate.name if candidate.name.endswith(".session") else candidate.name + ".session"
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate.parent / stem
    return state_dir() / stem


def adopt_legacy_session(destination) -> None:
    """Move a session an older version left in an unprotectable directory.

    Beside the installation is a git checkout; the working directory is wherever
    the MCP client happened to spawn the server. Neither can be locked down, and
    an auth key sitting in one of them is readable by every account on the
    machine for as long as it stays there. So it moves -- with every sidecar it
    had, because a `-wal` holds pages of the same database and is the same
    credential.

    Nothing is overwritten: a database already in the managed directory is the
    one in use, and replacing it with an older copy would swap the account out
    from under a running client. A move that cannot be completed is a refusal
    rather than a fallback, because the fallback is running the account out of
    the directory this function has just decided is unsafe.
    """
    destination = Path(destination)
    if destination.exists():
        return
    resolved = destination.resolve(strict=False)
    for directory in (Path(script_dir), Path.cwd()):
        legacy = directory / destination.name
        if not legacy.exists() or legacy.resolve(strict=False) == resolved:
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for suffix in _SESSION_SIDECARS:
                source = Path(str(legacy) + suffix)
                if source.exists():
                    source.replace(Path(str(destination) + suffix))
        except OSError as error:
            raise SessionNotProtected(_UNPROTECTED_SESSION_MESSAGE) from error
        # No path in the message: where an operator keeps their account is not
        # something a log file needs to record.
        log_event(
            logging.INFO,
            "moved a Telegram session out of a directory that cannot be made private",
        )
        return


def _close_unprotected(client) -> None:
    """Release what the constructor opened before reporting it unprotected.

    The client is not connected yet - only its SQLite session is open - and the
    file that handle holds is the very one the error is about. Raising over a
    live client left it open for the life of the process, which is both a leaked
    handle and a lock on the database an operator is about to be told to fix.
    """
    try:
        client.session.close()
    except Exception:  # pragma: no cover - a session that never opened
        pass


def harden_session_files(
    path,
    restrict: Optional[Callable[[Any], bool]] = None,
    verify: Optional[Callable[[Any], bool]] = None,
) -> bool:
    """Whether the session database, its sidecars and its directory are private.

    Called BEFORE Telethon's constructor as well as after it, and the order is
    the point. The database does not exist yet on the first call, and neither do
    the `-journal`, `-wal` and `-shm` files SQLite creates whenever it decides
    to; restricting what happens to be on disk therefore protects almost nothing.
    What protects them is the directory, by a different mechanism on each
    platform. Windows gives it inheritable entries, so a file born inside is
    born carrying an owner-only DACL of its own. POSIX has no such inheritance:
    the directory is 0700 and protects by CONTAINMENT, so a sidecar SQLite makes
    at 0644 is still unreachable by anyone else -- the mode on the file is not
    the control there, and treating it as one would report a breach where there
    is none. Measured against a real ``SQLiteSession`` in both shapes.

    **The state directory is repaired; a directory the operator chose is only
    checked.** This server created its own and may do as it likes with it.
    Locking down someone else's would strip the permissions off whatever else
    they keep there -- measured: with a legacy session in the working directory,
    that took the inherited ACL off the whole project checkout. So an operator's
    directory that is already private is accepted, and one that is not is
    reported as unprotectable, which :func:`_build_client` turns into a refusal
    to start that account.

    Returning ``True`` means the whole set was verified, not that a call
    succeeded. It used to be able to return ``True`` having restricted nothing
    at all: with no database on disk yet and a custom parent it never touched,
    every branch was skipped and the initial ``True`` survived.
    """
    restrict = restrict or restrict_to_owner
    verify = verify or verify_owner_only
    path = Path(path)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log_event(
            logging.WARNING,
            "could not create the directory for the Telegram session file",
            error=error,
        )
        return False

    state = state_dir()
    if parent == state or state in parent.parents:
        applied = bool(restrict(parent))
    else:
        applied = bool(verify(parent))
    for suffix in _SESSION_SIDECARS:
        sibling = Path(str(path) + suffix)
        if sibling.exists():
            applied = bool(restrict(sibling)) and applied
    if not applied:
        # No path in the message: where an operator keeps their account is not
        # something a log file needs to record.
        log_event(
            logging.WARNING,
            "could not restrict the Telegram session file to its owner; "
            "anyone who can read it is signed in as this account",
        )
    return applied


def harden_env_file(path=None, restrict: Optional[Callable[[Any], bool]] = None) -> None:
    """Make the credential file readable by its owner alone.

    `.env` holds TELEGRAM_API_HASH and, in the single-account setup the README
    shows first, a full session string -- either of which is the account. The
    documented `cp .env.example .env` copies the example's mode, which a normal
    umask leaves at 0644.

    The file is never opened here: only its mode/ACL is touched. An install
    configured entirely through real environment variables has no `.env` at
    all, which is a supported setup rather than a failure.
    """
    restrict = restrict or restrict_to_owner
    if path is None:
        from dotenv import find_dotenv

        found = find_dotenv(usecwd=True)
        if not found:
            return
        path = found
    target = Path(path)
    if not target.is_file():
        return
    if not restrict(target):
        log_event(
            logging.WARNING,
            "could not restrict the .env file to its owner; it holds the API "
            "hash and may hold a session string",
        )


def _build_client(session: Any, label: str) -> TelegramClient:
    """Construct a ``TelegramClient`` honoring per-label proxy configuration.

    A string session is a name, not a path: it is resolved to the private state
    directory (unless the operator named one), and the directory is made private
    before Telethon's constructor creates the database in it -- not afterwards,
    which would publish the auth key for as long as the constructor took and
    would never cover the sidecars SQLite adds later.

    A session that cannot be protected does not get a client. It is the whole
    account in one file, so starting anyway means serving Telegram requests out
    of a credential this function has just established is readable by somebody
    else.
    """
    proxy, connection = _build_proxy_for_label(label)
    kwargs: dict[str, Any] = {}
    if proxy is not None:
        kwargs["proxy"] = proxy
    if connection is not None:
        kwargs["connection"] = connection
    kwargs.update(client_identity_kwargs())

    session_path = None
    if isinstance(session, str):
        session_path = session_file_path(session)
        # The destination is made private BEFORE anything is put in it. The
        # adoption below MOVES a legacy session file - which is the account
        # itself - and running it first left that file sitting in a directory
        # nothing had hardened yet.
        if not harden_session_files(session_path):
            raise SessionNotProtected(_UNPROTECTED_SESSION_MESSAGE)
        adopt_legacy_session(session_path)
        # Again, over whatever the adoption brought with it, and before SQLite
        # creates anything: a database born in an unproven directory is readable
        # for the length of the constructor.
        if not harden_session_files(session_path):
            raise SessionNotProtected(_UNPROTECTED_SESSION_MESSAGE)
        session = str(session_path)

    client = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH, **kwargs)
    # And again over what the constructor actually put on disk: the calls above
    # proved the directory, this one proves the database that was born in it.
    if session_path is not None and not harden_session_files(session_path):
        _close_unprotected(client)
        raise SessionNotProtected(_UNPROTECTED_SESSION_MESSAGE)
    return client
