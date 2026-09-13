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
from typing import Any, List, Optional

from telethon import TelegramClient, functions
from telethon.errors import AuthKeyDuplicatedError, RPCError
from telethon.sessions import StringSession

from telegram_mcp.aliases import normalise_account_label, restrict_to_owner
from telegram_mcp.safe_log import log_event, logger, safe_exception
from telegram_mcp.settings import (
    StartupMessage,
    ValidationError,
)

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

# What the configuration says, as opposed to what is connected. Pure file and
# environment reading, so it moved to its own module; re-exported because
# `runtime` star-imports this one and the tests patch these names here.
from telegram_mcp.account_config import (  # noqa: F401  (re-exported)
    _ACCOUNT_PREFIXES,
    _EXTERNAL_ACCOUNT_VARS,
    _account_digest,
    _account_digest_bytes,
    _accounts_from_disk,
    _current_digests,
    _env_file,
    _env_fingerprint,
    _external_account_vars,
)

# Retiring a client outlives the synchronous call that starts it, so it owns a
# module of its own. Re-exported: `__all__` publishes these and `runtime`
# star-imports this file.
from telegram_mcp.retirement import (  # noqa: F401  (re-exported)
    _RETIRE_DRAIN_SECONDS,
    _retiring,
    drain_retirements,
    retire as _retire,
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

# Session files and client construction moved next door. Re-exported
# because `__all__` still publishes several of these and `runtime`
# star-imports this module.
from telegram_mcp.session_files import (  # noqa: F401  (re-exported)
    SessionNotProtected,
    _SESSION_SIDECARS,
    _UNPROTECTED_SESSION_MESSAGE,
    _build_client,
    _close_unprotected,
    adopt_legacy_session,
    harden_env_file,
    harden_session_files,
    package_dir,
    script_dir,
    session_file_path,
)

# The installation, for the two things that still resolve against it: a session
# file an older install left beside main.py, and the historic `script_dir` name
# the tools re-export.

# ---------------------------------------------------------------------------
# Multi-account configuration
# ---------------------------------------------------------------------------


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

# The pooled session this process claimed, if any. Held so a rebuild hands back
# the slot already locked instead of taking another client's.
_CLAIMED_SESSION: Optional[str] = None


def _parse_session_pool(env: Optional[dict] = None) -> List[str]:
    """Parse TELEGRAM_SESSION_STRINGS into a de-duplicated list of sessions.

    Takes the SNAPSHOT its caller is working from. Reading `os.environ` here
    while `_discover_accounts` had been handed a freshly parsed environment
    meant the two disagreed inside one call: the accounts came from the new
    file and the pool from whatever the process happened to still hold.
    """
    raw = (os.environ if env is None else env).get("TELEGRAM_SESSION_STRINGS")
    if not raw:
        return []
    pool: List[str] = []
    for tok in re.split(r"[\s,;]+", raw.strip()):
        if tok and tok not in pool:
            pool.append(tok)
    return pool


def _acquire_session(pool: List[str]) -> str:
    """Claim the first free session in the pool via an advisory file lock.

    A slot this process already holds is returned again rather than re-claimed.
    Rebuilding an unchanged pool - which a hot reload does whenever anything
    else in `.env` moves - otherwise walked past its own locked slot, found it
    taken, and claimed the NEXT one, quietly consuming a slot that belonged to
    another live client.
    """
    global _CLAIMED_SESSION
    if _CLAIMED_SESSION is not None and _CLAIMED_SESSION in pool:
        return _CLAIMED_SESSION
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
        _CLAIMED_SESSION = session
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


class NoAccountsConfigured(StartupMessage):
    """Nothing at all is configured - as distinct from something configured wrongly.

    The two need different answers. At startup there is no account to serve and
    the process says so and stops. During a reload it means the file on disk is
    momentarily unusable, and the right move is to keep serving the generation
    already running rather than to exit; a half-written `.env` is a window the
    account manager genuinely opens every time it rewrites one.

    A `StartupMessage`, so the runner's readable-error path still prints it
    word for word instead of a traceback.
    """


def _discover_accounts(
    env: Optional[dict] = None, reuse: Optional[dict] = None
) -> dict[str, TelegramClient]:
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

    ``reuse`` maps a label to a client the caller already owns and intends to
    keep. Validation still covers the WHOLE set - a duplicate label is an error
    whether or not that account changed - but nothing is constructed for a label
    being kept. A reload used to build every client and then drop the unchanged
    ones on the floor unclosed, so a `.env` touched ten times leaked ten clients
    per untouched account, each with whatever file handles and locks its session
    had taken.
    """
    reuse = reuse or {}
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
        if label in reuse:
            accounts[label] = reuse[label]
            continue
        _key, kind, value = sources[0]
        session = StringSession(value) if kind == "string" else value
        accounts[label] = _build_client(session, label)

    # Backward-compatible unsuffixed variables. A pool (TELEGRAM_SESSION_STRINGS)
    # takes precedence for the default account and claims a free session slot.
    session_pool = _parse_session_pool(environment)
    session_string = environment.get("TELEGRAM_SESSION_STRING")
    session_name = environment.get("TELEGRAM_SESSION_NAME")

    if "default" not in accounts:
        if "default" in reuse:
            # Before the pool branch on purpose: claiming a slot for an account
            # that is not being rebuilt is how a rebuild took a second one.
            accounts["default"] = reuse["default"]
        elif session_pool:
            accounts["default"] = _build_client(
                StringSession(_acquire_session(session_pool)), "default"
            )
        elif session_string:
            accounts["default"] = _build_client(StringSession(session_string), "default")
        elif session_name:
            accounts["default"] = _build_client(session_name, "default")

    if not accounts:
        # RAISED, not `sys.exit`. `SystemExit` derives from `BaseException`, so
        # `refresh_accounts`'s `except Exception` never saw it: a `.env` caught
        # mid-rewrite - and the account manager backs up and rewrites, so that
        # window is real - took the whole running server down from inside a
        # routine hot-reload check. Startup still exits, just below; a reload
        # keeps the generation it already has.
        raise NoAccountsConfigured(
            "No Telegram session configured. "
            "Set TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env"
        )

    return accounts


try:
    clients: dict[str, TelegramClient] = _discover_accounts()
except NoAccountsConfigured as _no_accounts:
    # Startup with nothing configured cannot proceed, and says so in one line
    # rather than a traceback - the behaviour this replaced, kept verbatim.
    print(f"Error: {_no_accounts}", file=sys.stderr)
    sys.exit(1)


_env_stamp: tuple = _env_fingerprint(_env_file())
# The baseline IS the startup configuration, established from the same view
# `refresh_accounts` will compare against. Starting empty meant the first
# refresh had nothing to diff, so it adopted whatever was on disk and applied
# none of it: an edit landing between startup discovery and that first call was
# recorded as active while the old client kept serving. The window is small and
# entirely real - the account manager writes `.env` and the next tool call is
# the first refresh.
_env_digests: dict = _current_digests(_accounts_from_disk())


# Told after every change to `clients`. A registry of callbacks rather than a
# direct call, because the modules that care - `tools.events` above all - reach
# this one through `runtime`'s star import, so the dependency runs one way only.
# Registering a callback is how the other direction gets expressed without an
# import cycle.
_registry_listeners: list = []


def on_clients_changed(callback) -> None:
    """Run ``callback(added, removed)`` after every change to ``clients``."""
    if callback not in _registry_listeners:
        _registry_listeners.append(callback)


def _notify_clients_changed(added: set, removed: set) -> None:
    if not (added or removed):
        return
    for callback in list(_registry_listeners):
        try:
            callback(added, removed)
        except Exception as error:
            log_event(
                logging.ERROR,
                "a client-registry listener failed",
                error=error,
                added=len(added),
                removed=len(removed),
            )


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
    if stamp == _env_stamp:
        return []

    try:
        env = _accounts_from_disk()
        digests = _current_digests(env)
    except Exception:
        # A half-written `.env` - the account manager backs up and rewrites, so
        # there IS a window - must not take the running server down. The next
        # call sees a new stamp and tries again.
        return []

    if digests == _env_digests:
        _env_stamp = stamp
        return []

    keep = {label: client for label, client in clients.items() if not _replaced(label, digests)}
    try:
        rebuilt = _discover_accounts(env, reuse=keep)
    except Exception as error:
        # A `.env` that no longer describes a valid account set - a duplicate
        # label, an unusable one, or none at all - leaves the WORKING clients in
        # place. Refusing to serve because a file on disk went wrong would be
        # worse than serving what already works. Said out loud, because a reload
        # that quietly did nothing is indistinguishable from one that worked.
        log_event(
            logging.WARNING,
            "account reload rejected; keeping the running accounts",
            error=error,
            accounts=len(clients),
        )
        _env_stamp = stamp
        return []

    before = dict(clients)
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
    # Identity, not label: a re-login keeps the label and replaces the object, and
    # a listener that only watched labels left the new client with no handler.
    _notify_clients_changed(
        {label for label, cl in clients.items() if before.get(label) is not cl},
        set(before) - set(clients),
    )
    return sorted(set(changed))


def _account_label_of(key: str) -> Optional[str]:
    """The account label an environment variable configures, or ``None``.

    The same mapping :func:`_discover_accounts` applies, because :func:`_replaced`
    has to select the same variables it does. It selected them by
    ``key.upper().endswith(label.upper())``, which is wrong in both directions:
    ``TELEGRAM_SESSION_STRING_NETWORK`` ends with ``WORK``, so re-logging in
    `network` retired the live `work` client too - and the DEFAULT account's
    variables carry no suffix at all, so nothing ever matched ``default`` and its
    re-login was noticed, recorded as seen, and then silently ignored. That left
    the server holding the session Telegram had just invalidated, which is the
    exact AuthKeyUnregisteredError this module exists to prevent.
    """
    for prefix in _ACCOUNT_PREFIXES:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        # "" is TELEGRAM_SESSION_STRING/NAME; "S" is only the TELEGRAM_SESSION_STRINGS
        # pool, which has no NAME counterpart. `_discover_accounts` reads all of
        # these as the default account, and anything else as no account at all.
        if suffix == "" or (suffix == "S" and prefix.endswith("STRING")):
            return "default"
        if suffix.startswith("_"):
            try:
                return normalise_account_label(suffix[1:]).lower()
            except ValueError:
                return None
        return None
    return None


def _replaced(label: str, digests: dict) -> bool:
    """Whether this label's session value differs from the one in use."""
    before = {k: v for k, v in _env_digests.items() if _account_label_of(k) == label}
    after = {k: v for k, v in digests.items() if _account_label_of(k) == label}
    return before != after


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
    "on_clients_changed",
    "_RETIRE_DRAIN_SECONDS",
    "drain_retirements",
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
    "NoAccountsConfigured",
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
