"""Getting a connected Telethon client for an account, and keeping it connected.

One question, one module: given an account label - or none, in single-account mode -
hand back a client that is logged in, routed through whatever proxy the operator
configured, and actually reachable right now.

The pieces are here because they only make sense together. A session string names an
account; a session POOL exists because Telethon's `StringSession` holds no persistent
entity cache, so several concurrent clients need distinct slots rather than one shared
one. `@with_account` is the router that turns a tool's `account=` argument into a
client. `ensure_connected` is the part that distinguishes "the socket is open" from
"the server answers", which are not the same thing and only the second one matters.

**Patch this module, not `runtime`.** `runtime` re-exports these names for the star
imports every tool module uses, so rebinding `runtime._build_client` in a test sets a
second name and the code here keeps calling its own.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, List, Optional

from telethon import TelegramClient, functions
from telethon.errors import AuthKeyDuplicatedError, RPCError
from telethon.sessions import StringSession

from telegram_mcp.aliases import normalise_account_label, restrict_to_owner
from telegram_mcp.owner_only import verify_owner_only
from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.safe_log import log_event, logger, safe_exception
from telegram_mcp.settings import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    StartupMessage,
    ValidationError,
)
from telegram_mcp.settings import _parse_bool_env, state_dir

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
from telegram_mcp.singleton import try_lock_exclusive

# The installation, for the two things that still resolve against it: a session
# file an older install left beside main.py, and the historic `script_dir` name
# the tools re-export.
package_dir = os.path.dirname(os.path.abspath(__file__))
script_dir = os.path.dirname(package_dir)

# ---------------------------------------------------------------------------
# Multi-account configuration
# ---------------------------------------------------------------------------


_PROXY_TYPES_SOCKS_HTTP = {"socks5", "socks4", "http"}
_PROXY_TYPES_ALL = _PROXY_TYPES_SOCKS_HTTP | {"mtproxy"}

# TCP ports a socket can actually be reached on. 0 means "any free port" to
# bind() and nothing at all to connect(), and the rest are simply out of range.
_MIN_PORT = 1
_MAX_PORT = 65535


def parse_port(raw: str, variable: str) -> int:
    """Parse a TCP port from configuration, refusing anything unreachable.

    Shared by the proxy settings and the HTTP transport's ``MCP_PORT``: both
    used to take the value on trust, so ``0``/``-1``/``70000`` were carried all
    the way to the first connection attempt and surfaced there as an unrelated
    socket error.
    """
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{variable} must be an integer between {_MIN_PORT} and {_MAX_PORT}, got {raw!r}."
        ) from exc
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ValidationError(
            f"{variable} must be between {_MIN_PORT} and {_MAX_PORT}, got {port}."
        )
    return port


def _get_proxy_env(name: str, label: str) -> Optional[str]:
    """Resolve a TELEGRAM_PROXY_* env var with optional ``_<LABEL>`` suffix.

    Per-account values override the unsuffixed defaults so a global proxy can
    coexist with per-label overrides.
    """
    suffixed = os.getenv(f"TELEGRAM_PROXY_{name}_{label.upper()}")
    if suffixed:
        return suffixed
    return os.getenv(f"TELEGRAM_PROXY_{name}") or None


def _build_proxy_for_label(label: str) -> tuple[Optional[Any], Optional[Any]]:
    """Return ``(proxy, connection)`` kwargs for ``TelegramClient`` for a label.

    Reads ``TELEGRAM_PROXY_*`` env vars (with optional ``_<LABEL>`` suffix).
    Returns ``(None, None)`` when no proxy is configured. Raises
    :class:`ValidationError` for malformed configuration so the server fails
    fast instead of silently bypassing the proxy.
    """
    proxy_type = _get_proxy_env("TYPE", label)
    if not proxy_type:
        return None, None

    proxy_type = proxy_type.strip().lower()
    if proxy_type not in _PROXY_TYPES_ALL:
        raise ValidationError(
            f"Invalid TELEGRAM_PROXY_TYPE '{proxy_type}'. "
            f"Expected one of: {', '.join(sorted(_PROXY_TYPES_ALL))}."
        )

    host = _get_proxy_env("HOST", label)
    port_raw = _get_proxy_env("PORT", label)
    if not host or not port_raw:
        raise ValidationError(
            "TELEGRAM_PROXY_HOST and TELEGRAM_PROXY_PORT are required when "
            "TELEGRAM_PROXY_TYPE is set."
        )
    port = parse_port(port_raw, "TELEGRAM_PROXY_PORT")

    if proxy_type == "mtproxy":
        secret = _get_proxy_env("SECRET", label)
        if not secret:
            raise ValidationError("TELEGRAM_PROXY_SECRET is required for mtproxy.")
        try:
            from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        except ImportError as exc:  # pragma: no cover - defensive guard
            raise ValidationError(
                "Telethon MTProxy connection class is unavailable; upgrade telethon."
            ) from exc
        return (host, port, secret), ConnectionTcpMTProxyRandomizedIntermediate

    # SOCKS4/SOCKS5/HTTP via python-socks (Telethon's optional dependency).
    try:
        import python_socks  # noqa: F401
    except ImportError as exc:
        raise ValidationError(
            f"Proxy type '{proxy_type}' requires the 'python-socks' package. "
            "Install it with `pip install python-socks` or `uv sync --extra proxy`."
        ) from exc

    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": host,
        "port": port,
        "rdns": _parse_bool_env(_get_proxy_env("RDNS", label), default=True),
    }
    username = _get_proxy_env("USERNAME", label)
    password = _get_proxy_env("PASSWORD", label)
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy, None


# --- File-based sessions -----------------------------------------------------
#
# A `.session` file IS the account. It is a SQLite database holding the auth
# key, and whoever can read it is logged in as that account with no password
# and no second factor -- Telethon's own docstring says as much. It was being
# created wherever the process happened to start, with whatever the umask gave
# it (0644 on a normal host) and, on Windows, readable by every account on the
# machine.

# SQLite writes alongside the database it opens. A `-journal` holds pages of
# the same file mid-write, and `-wal`/`-shm` hold them for as long as the
# connection lives, so restricting only the `.session` restricts nothing while
# a write is in flight.
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


# --- Session pool ------------------------------------------------------------
# A POOL of interchangeable authorized sessions for the SAME account lets
# several concurrent MCP clients (e.g. the desktop app AND a terminal CLI) run
# against one Telegram account without tripping AuthKeyDuplicatedError.
#
# Telegram forbids one auth key (one StringSession) being used from two IPs at
# once; on a dual-stack / VPN host two local clients can egress via different
# source IPs and collide. The fix is one authorized session PER concurrent
# client (Telegram allows one account on many "devices"). Generate extra
# sessions with `uv run session_string_generator.py` and list them in
# TELEGRAM_SESSION_STRINGS (whitespace/comma/semicolon separated). Each process
# claims the first session not already locked by a live process via an advisory
# flock, so clients deterministically pick distinct slots; the OS releases the
# lock if a process dies.

# Acquired lock handles are held for the process lifetime so the advisory locks
# stay held until exit (or crash, when the OS releases them).
_SESSION_LOCKS: list = []


def _parse_session_pool() -> List[str]:
    """Parse TELEGRAM_SESSION_STRINGS into a de-duplicated list of sessions."""
    raw = os.getenv("TELEGRAM_SESSION_STRINGS")
    if not raw:
        return []
    pool: List[str] = []
    for tok in re.split(r"[\s,;]+", raw.strip()):
        if tok and tok not in pool:
            pool.append(tok)
    return pool


def _acquire_session(pool: List[str]) -> str:
    """Claim the first free session in the pool via an advisory file lock."""
    lock_dir = os.path.join(tempfile.gettempdir(), "telegram-mcp-session-locks")
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        lock_dir = tempfile.gettempdir()
    for idx, session in enumerate(pool):
        digest = hashlib.sha1(session.encode("utf-8")).hexdigest()[:16]
        lock_path = os.path.join(lock_dir, f"session-{digest}.lock")
        try:
            # "a+", not "w": on Windows the lock covers the first byte, and
            # truncating a file another live client holds is refused.
            fh = open(lock_path, "a+")
        except OSError:
            continue
        if not try_lock_exclusive(fh):
            # Locked by another live client — try the next session.
            try:
                fh.close()
            except Exception:
                pass
            continue
        _SESSION_LOCKS.append(fh)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        print(f"Using Telegram session slot {idx + 1}/{len(pool)}.", file=sys.stderr)
        return session
    # Handing out an already-claimed session here would make Telegram burn it
    # with AuthKeyDuplicatedError — losing the slot for the client that owns it
    # too. Refusing to start is recoverable; a burned session is not.
    raise StartupMessage(
        f"All {len(pool)} pooled Telegram session(s) are already claimed by other "
        "live clients, so this one has no session to use. Add another session to "
        "TELEGRAM_SESSION_STRINGS (generate it with "
        "`uv run session_string_generator.py`) — one slot per concurrent client — "
        "or stop one of the other clients."
    )


def _discover_accounts(env: Optional[dict] = None) -> dict[str, TelegramClient]:
    """Scan env vars to build account label -> TelegramClient mapping.

    Detection rules:
    - TELEGRAM_SESSION_STRING_<LABEL> / TELEGRAM_SESSION_NAME_<LABEL> -> multi-mode
    - TELEGRAM_SESSION_STRINGS (whitespace/comma/semicolon separated) -> a pool
      of interchangeable sessions for the default account; each process claims a
      free slot to avoid AuthKeyDuplicatedError (takes precedence for "default")
    - Unsuffixed TELEGRAM_SESSION_STRING / TELEGRAM_SESSION_NAME -> label "default"
    - If both suffixed and unsuffixed exist -> unsuffixed becomes "default"

    Two variables that name the SAME label are a configuration error, not a
    precedence question: the old loop simply let the later one win, so which
    account the server ran as depended on the order ``os.environ`` iterated in.
    A label that normalises to nothing is refused for the same reason.

    Each client is constructed via :func:`_build_client`, which applies any
    matching ``TELEGRAM_PROXY_*`` configuration (optionally per-label).
    """
    environment = os.environ if env is None else env
    accounts: dict[str, TelegramClient] = {}

    prefix_str = "TELEGRAM_SESSION_STRING_"
    prefix_name = "TELEGRAM_SESSION_NAME_"

    # Collect first, decide second: every conflict is then visible at once and
    # the answer cannot depend on iteration order.
    declared: dict[str, list[tuple[str, str, str]]] = {}
    for key, value in environment.items():
        if key.startswith(prefix_str) and value:
            suffix, kind = key[len(prefix_str) :], "string"
        elif key.startswith(prefix_name) and value:
            suffix, kind = key[len(prefix_name) :], "name"
        else:
            continue
        # The SAME rule the account manager and session generator apply when
        # they WRITE a label. Reading with a weaker one (a bare strip+lower) let
        # `TELEGRAM_SESSION_STRING_WORK-2` register as `work-2`, a name no tool
        # could ever produce - so the account existed and nothing could address
        # it. Canonicalising here also makes `WORK-2` and `WORK_2` the same
        # account, which is what the collision check below then refuses.
        try:
            label = normalise_account_label(suffix).lower()
        except ValueError as error:
            raise ValidationError(
                f"'{key}' does not name a usable account: {error} Use "
                f"'{prefix_str}<LABEL>' with a label, or the unsuffixed variable "
                "for the default account."
            ) from error
        declared.setdefault(label, []).append((key, kind, value))

    for label, sources in sorted(declared.items()):
        if len(sources) > 1:
            names = ", ".join(sorted(key for key, _, _ in sources))
            raise ValidationError(
                f"Account '{label}' is defined more than once ({names}). These "
                "resolve to one account after normalisation - spaces and hyphens "
                "both become underscores and case is folded - and which one wins "
                "would depend on environment order. Keep exactly one."
            )
        _key, kind, value = sources[0]
        session = StringSession(value) if kind == "string" else value
        accounts[label] = _build_client(session, label)

    # Backward-compatible unsuffixed variables. A pool (TELEGRAM_SESSION_STRINGS)
    # takes precedence for the default account and claims a free session slot.
    session_pool = _parse_session_pool()
    session_string = environment.get("TELEGRAM_SESSION_STRING")
    session_name = environment.get("TELEGRAM_SESSION_NAME")

    if "default" not in accounts:
        if session_pool:
            accounts["default"] = _build_client(
                StringSession(_acquire_session(session_pool)), "default"
            )
        elif session_string:
            accounts["default"] = _build_client(StringSession(session_string), "default")
        elif session_name:
            accounts["default"] = _build_client(session_name, "default")

    if not accounts:
        print(
            "Error: No Telegram session configured. "
            "Set TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    return accounts


clients: dict[str, TelegramClient] = _discover_accounts()


def _env_file() -> Optional[str]:
    """The `.env` this process reads, or None when it runs on real env vars."""
    try:
        from dotenv import find_dotenv

        return find_dotenv(usecwd=True) or None
    except Exception:
        return None


def _env_fingerprint(path: Optional[str]) -> tuple:
    """A digest of the file's CONTENT, not its metadata.

    This was `(st_mtime_ns, st_size)` and that was wrong. Replacing one session
    string with another of the same length changes neither: the account manager
    rewrites `.env` wholesale, so a re-login is exactly a same-size rewrite, and
    on a filesystem whose timestamp resolution is coarser than the gap between
    the two writes the mtime does not move either. CI caught it on a Windows
    runner where the local machine never had.

    Hashing a file of a few kilobytes costs microseconds and is checked once per
    `get_client`. The stat was the cheaper answer to the wrong question.
    """
    if not path:
        return ()
    try:
        with open(path, "rb") as handle:
            return (_account_digest_bytes(handle.read()),)
    except OSError:
        return ()


def _account_digest_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _account_digest(value: str) -> str:
    """A session string is a full login; only ever its digest is kept."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_ACCOUNT_PREFIXES = ("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")


def _accounts_from_disk() -> dict:
    """The environment as it would be if this process had started right now.

    Account variables come from the FILE alone, everything else from the live
    process. That asymmetry is the point: `load_dotenv` can add a new variable
    to `os.environ` but never removes one, so an account deleted from `.env`
    would otherwise still be in the environment and stay configured forever.
    """
    from dotenv import dotenv_values

    path = _env_file()
    on_disk = dotenv_values(path) if path else {}
    env = {k: v for k, v in os.environ.items() if not k.startswith(_ACCOUNT_PREFIXES)}
    env.update({k: v for k, v in on_disk.items() if v})
    return env


def _current_digests(env: dict) -> dict:
    return {
        key: _account_digest(value)
        for key, value in env.items()
        if key.startswith(_ACCOUNT_PREFIXES) and value
    }


_env_stamp: tuple = _env_fingerprint(_env_file())
_env_digests: dict = {}


def refresh_accounts() -> list:
    """Pick up accounts added, removed or re-logged-in since startup.

    Adding an account used to need a server restart, and the failure when you
    forgot was not "unknown account" - it was `AuthKeyUnregisteredError` from
    the session this process was still holding, which reads like Telegram
    revoking the login rather than like stale state here. Re-logging in an
    existing account produced the same thing.

    Cost on the common path is one `os.stat`. The file is only parsed, and
    clients only rebuilt, when that stamp actually moves.

    Returns the labels that changed, so a caller can say what happened.
    """
    global _env_stamp, _env_digests

    path = _env_file()
    stamp = _env_fingerprint(path)
    if stamp == _env_stamp and _env_digests:
        return []

    try:
        env = _accounts_from_disk()
        digests = _current_digests(env)
    except Exception:
        # A half-written `.env` - the account manager backs up and rewrites, so
        # there IS a window - must not take the running server down. The next
        # call sees a new stamp and tries again.
        return []

    if not _env_digests:  # first call: adopt the startup state, change nothing
        _env_stamp, _env_digests = stamp, digests
        return []
    if digests == _env_digests:
        _env_stamp = stamp
        return []

    try:
        rebuilt = _discover_accounts(env)
    except Exception:
        # A `.env` that no longer describes a valid account set (a duplicate
        # label, an unusable one) leaves the WORKING clients in place. Refusing
        # to serve because a file on disk went wrong would be worse than serving
        # what already works.
        _env_stamp = stamp
        return []

    changed = sorted(set(rebuilt) ^ set(clients)) + sorted(
        label for label in set(rebuilt) & set(clients) if _replaced(label, digests)
    )
    for label in set(clients) - set(rebuilt):
        _retire(clients.pop(label))
    for label, client in rebuilt.items():
        if label in clients and not _replaced(label, digests):
            continue
        if label in clients:
            _retire(clients[label])
        clients[label] = client

    _env_stamp, _env_digests = stamp, digests
    return sorted(set(changed))


def _replaced(label: str, digests: dict) -> bool:
    """Whether this label's session value differs from the one in use."""
    keys = [k for k in digests if k.upper().endswith(label.upper())] or []
    before = {k: v for k, v in _env_digests.items() if k in keys}
    after = {k: v for k, v in digests.items() if k in keys}
    return before != after


def _retire(client) -> None:
    """Close a client this process no longer serves.

    `get_client` is synchronous and is called both from inside the server's loop
    and from plain code, so there are two cases and the first version handled
    only one: it scheduled the disconnect on the running loop and did nothing at
    all when there was none - which is the ordinary case, so the socket simply
    stayed open. Its own test caught that.

    Never raises. A lookup must not fail because tidying up did.
    """
    import asyncio

    try:
        closing = client.disconnect()
    except Exception:
        return
    if closing is None:  # Telethon already closed it synchronously
        return
    try:
        asyncio.get_running_loop().create_task(closing)
        return
    except RuntimeError:
        pass  # no loop here: close it now rather than leaving it open
    try:
        asyncio.run(closing)
    except Exception:
        try:
            closing.close()  # at least do not leave a pending coroutine
        except Exception:
            pass


def get_client(account: str = None) -> TelegramClient:
    """Resolve account label to TelegramClient."""
    refresh_accounts()
    if account is None:
        if len(clients) == 1:
            return next(iter(clients.values()))
        raise ValueError(f"Account is required. Available accounts: {', '.join(clients.keys())}")
    label = account.lower()
    if label not in clients:
        raise ValueError(
            f"Unknown account '{account}'. Available accounts: {', '.join(clients.keys())}"
        )
    return clients[label]


def is_multi_mode() -> bool:
    """Return True when more than one account is configured."""
    return len(clients) > 1


def with_account(readonly=False):
    """Decorator that adds multi-account support to MCP tools.

    - In single-mode: always uses the sole client, no output tagging.
    - In multi-mode with explicit account: uses that account's client.
    - In multi-mode without account + readonly: fans out to all accounts
      concurrently and returns one JSON object, ``{"accounts": {label: result}}``.
      A failing account appears as ``{"error": "<Type>: <message>"}`` beside the
      others rather than discarding them.
    - In multi-mode without account + NOT readonly: returns an error.

    The wrapped function must accept ``account: str = None`` and use
    ``get_client(account)`` internally to obtain the TelegramClient.
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            account = kwargs.get("account")

            # Explicit account OR single-mode -> call once
            if account is not None or not is_multi_mode():
                return await fn(*args, **kwargs)

            # account is None AND multi-mode
            if not readonly:
                labels = ", ".join(clients.keys())
                return f"Error: 'account' is required. Available accounts: {labels}"

            # Read-only fan-out to all accounts concurrently
            async def _call_for(label):
                kw = dict(kwargs)
                kw["account"] = label
                return await fn(*args, **kw)

            # return_exceptions: without it the first failing account propagates out of
            # gather and out of this wrapper, discarding every other account's already
            # completed result — one expired session turned a five-account query into a
            # single error string.
            #
            # labels is materialised once and reused for the zip, so results cannot be
            # mis-paired if `clients` is rebound mid-await. The old code carried the
            # label inside the returned tuple, which return_exceptions makes impossible
            # for the failing branch.
            labels = list(clients)
            outcomes = await asyncio.gather(
                *(_call_for(label) for label in labels), return_exceptions=True
            )

            # One envelope instead of "\n\n".join(f"[{label}]\n{result}"). Every tool
            # returns JSON from format_tool_result, and welding those strings together
            # produced something no caller could parse. Values are decoded where they
            # are JSON and kept verbatim where a tool answers in prose ("No messages
            # found."), so both kinds survive.
            #
            # BaseException, not Exception: gather(return_exceptions=True) returns
            # whatever was raised, and CancelledError is a BaseException.
            accounts: dict[str, Any] = {}
            for label, outcome in zip(labels, outcomes):
                if isinstance(outcome, BaseException):
                    accounts[label] = {"error": f"{type(outcome).__name__}: {outcome}"}
                    continue
                try:
                    accounts[label] = json.loads(outcome)
                except (TypeError, ValueError):
                    accounts[label] = outcome
            # ensure_ascii=False matches format_tool_result, so non-ASCII chat titles are
            # not escaped twice; default=str is a net for a non-string, non-JSON value —
            # this wrapper must never raise.
            return json.dumps({"accounts": accounts}, ensure_ascii=False, default=str)

        # The routing contract, readable without unwrapping: a registry test can
        # check it against the tool annotation, which is how save_disappearing_media
        # was found declaring readOnlyHint=False while routing as read-only.
        wrapper.__telegram_readonly__ = readonly
        return wrapper

    return decorator


_last_conn_verified: dict[int, float] = {}
_RECONNECT_LOCKS: dict[int, asyncio.Lock] = {}
_CONN_VERIFY_INTERVAL: float = 30.0  # seconds between live pings
_RECONNECT_TIMEOUT: float = 30.0  # seconds before a reconnect attempt is abandoned

# Raised from two places — the reconnect's connect(), and the liveness probe, which
# meets it first when Telegram invalidates the key on first use after the clash.
_BURNED_SESSION_MESSAGE = (
    "Telegram session is no longer usable: the same session string was "
    "used by another client at the same time (AuthKeyDuplicatedError). "
    "Give each concurrent client its own session via "
    "TELEGRAM_SESSION_STRINGS or TELEGRAM_SESSION_STRING_<LABEL>, then "
    "regenerate the burned session with `uv run session_string_generator.py`."
)


async def _force_reconnect(cl: TelegramClient):
    """Disconnect + reconnect this client, one caller at a time.

    The client object is SHARED: every tool call for an account gets the same one
    from `clients`. Two concurrent callers that both found the socket dead used to
    interleave here — A disconnects, B disconnects, A connects, B tears down the
    connection A just brought up. The lock serialises them; the re-check after
    acquiring it means the second caller returns instead of reconnecting a client
    the first one already fixed.
    """
    key = id(cl)
    async with _RECONNECT_LOCKS.setdefault(key, asyncio.Lock()):
        if cl.is_connected() and time.time() - _last_conn_verified.get(key, 0.0) < (
            _CONN_VERIFY_INTERVAL
        ):
            return
        log_event(logging.WARNING, "forcing a reconnect")
        try:
            await cl.disconnect()
        except Exception:
            pass
        try:
            await asyncio.wait_for(cl.connect(), timeout=_RECONNECT_TIMEOUT)
        except AuthKeyDuplicatedError as exc:
            # Telegram permanently invalidates an auth key used from two IPs at
            # once, so retrying here can never succeed — surface it instead of
            # letting the caller sit in a reconnect loop.
            raise StartupMessage(_BURNED_SESSION_MESSAGE) from exc
        except asyncio.TimeoutError as exc:
            raise StartupMessage(
                f"Reconnecting to Telegram timed out after {_RECONNECT_TIMEOUT:.0f}s."
            ) from exc
        if not await cl.is_user_authorized():
            log_event(
                logging.ERROR,
                "not authorized after reconnect; refusing interactive login",
            )
            # A raise, not a call to Telethon's start(). That method defaults to
            # `phone=lambda: input(...)` — a synchronous read inside a coroutine, which
            # blocks the whole event loop, and on stdio it reads the same stdin the MCP
            # protocol speaks over. Wrapping it in asyncio.wait_for cannot save us: the
            # timeout is scheduled on the very loop input() has stopped, so it never
            # fires. The server would hang silently and permanently. runner.py refuses
            # the same thing at startup for the same reason.
            raise StartupMessage(
                "Telegram session is no longer authorized. Interactive phone login is "
                "disabled for the MCP server because it runs over stdio. Regenerate the "
                "session with `uv run session_string_generator.py` and update "
                "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env; "
                "`Manage-Accounts.ps1` does both for a labelled account."
            )
        _last_conn_verified[key] = time.time()
        log_event(logging.WARNING, "forced reconnect succeeded")


async def ensure_connected(cl: TelegramClient = None):
    """Verify Telegram connection is alive, reconnect if needed.

    is_connected() can return True when the underlying TCP socket is dead.
    We periodically send a lightweight request to verify the connection
    actually works, and force-reconnect on any failure.

    Accepts an explicit client; falls back to the default single-account
    client when called without one.
    """
    if cl is None:
        cl = get_client()

    key = id(cl)

    if not cl.is_connected():
        await _force_reconnect(cl)
        return

    # Skip verification if recently confirmed alive
    now = time.time()
    if now - _last_conn_verified.get(key, 0.0) < _CONN_VERIFY_INTERVAL:
        return

    # Verify with a lightweight Telegram API call
    try:
        await asyncio.wait_for(
            cl(functions.help.GetNearestDcRequest()),
            timeout=5.0,
        )
    except AuthKeyDuplicatedError as exc:
        # Also an RPCError, so "the server answered" is literally true — but what it
        # answered is that this session is permanently dead. Falling through to the
        # branch below would record it as verified and send the caller on to a tool
        # call that fails generically, discarding the one message that says how to
        # recover. Must precede the RPCError branch.
        raise StartupMessage(_BURNED_SESSION_MESSAGE) from exc
    except RPCError:
        # The server ANSWERED — it just refused. That is proof the socket is alive,
        # which is the only question this function asks. FloodWaitError is the case
        # that made this matter: reconnecting while the account is rate-limited is
        # exactly the wrong move, and the old `except (..., Exception)` caught every
        # RPC refusal as if the transport had died.
        _last_conn_verified[key] = now
    except Exception:
        # Transport-level: ConnectionError / OSError / asyncio.TimeoutError, and
        # anything else that is NOT the server talking back — including
        # TypeNotFoundError, where Telethon could not parse the reply and the read
        # buffer is desynchronised (see runtime.py:333-341).
        #
        # asyncio.CancelledError is a BaseException and is deliberately NOT caught:
        # a cancelled tool call must not drag the shared client through a reconnect.
        await _force_reconnect(cl)
    else:
        _last_conn_verified[key] = now


__all__ = [
    "_BURNED_SESSION_MESSAGE",
    "_CONN_VERIFY_INTERVAL",
    "_PROXY_TYPES_ALL",
    "_PROXY_TYPES_SOCKS_HTTP",
    "_RECONNECT_LOCKS",
    "_RECONNECT_TIMEOUT",
    "_SESSION_LOCKS",
    "_UNPROTECTED_SESSION_MESSAGE",
    "_acquire_session",
    "_build_client",
    "_build_proxy_for_label",
    "_discover_accounts",
    "_force_reconnect",
    "_get_proxy_env",
    "_last_conn_verified",
    "_parse_session_pool",
    "SessionNotProtected",
    "adopt_legacy_session",
    "clients",
    "console_handler",
    "ensure_connected",
    "get_client",
    "harden_env_file",
    "harden_session_files",
    "is_multi_mode",
    "log_event",
    "log_file_path",
    "logger",
    "package_dir",
    "restrict_to_owner",
    "session_file_path",
    "safe_exception",
    "script_dir",
    "with_account",
]
