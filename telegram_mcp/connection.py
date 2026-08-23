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
import logging
import os
import re
import sys
import tempfile
import time
from functools import wraps
from typing import Any, List, Optional

from pythonjsonlogger import jsonlogger
from telethon import TelegramClient, functions
from telethon.errors import AuthKeyDuplicatedError, RPCError
from telethon.sessions import StringSession

from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.settings import TELEGRAM_API_HASH, TELEGRAM_API_ID, ValidationError
from telegram_mcp.settings import _parse_bool_env
from telegram_mcp.singleton import try_lock_exclusive

logger = logging.getLogger("telegram_mcp")


# ---------------------------------------------------------------------------
# Multi-account configuration
# ---------------------------------------------------------------------------


_PROXY_TYPES_SOCKS_HTTP = {"socks5", "socks4", "http"}
_PROXY_TYPES_ALL = _PROXY_TYPES_SOCKS_HTTP | {"mtproxy"}


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
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValidationError(
            f"TELEGRAM_PROXY_PORT must be an integer, got '{port_raw}'."
        ) from exc

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


def _build_client(session: Any, label: str) -> TelegramClient:
    """Construct a ``TelegramClient`` honoring per-label proxy configuration."""
    proxy, connection = _build_proxy_for_label(label)
    kwargs: dict[str, Any] = {}
    if proxy is not None:
        kwargs["proxy"] = proxy
    if connection is not None:
        kwargs["connection"] = connection
    kwargs.update(client_identity_kwargs())
    return TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH, **kwargs)


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
    raise RuntimeError(
        f"All {len(pool)} pooled Telegram session(s) are already claimed by other "
        "live clients, so this one has no session to use. Add another session to "
        "TELEGRAM_SESSION_STRINGS (generate it with "
        "`uv run session_string_generator.py`) — one slot per concurrent client — "
        "or stop one of the other clients."
    )


def _discover_accounts() -> dict[str, TelegramClient]:
    """Scan env vars to build account label -> TelegramClient mapping.

    Detection rules:
    - TELEGRAM_SESSION_STRING_<LABEL> / TELEGRAM_SESSION_NAME_<LABEL> -> multi-mode
    - TELEGRAM_SESSION_STRINGS (whitespace/comma/semicolon separated) -> a pool
      of interchangeable sessions for the default account; each process claims a
      free slot to avoid AuthKeyDuplicatedError (takes precedence for "default")
    - Unsuffixed TELEGRAM_SESSION_STRING / TELEGRAM_SESSION_NAME -> label "default"
    - If both suffixed and unsuffixed exist -> unsuffixed becomes "default"

    Each client is constructed via :func:`_build_client`, which applies any
    matching ``TELEGRAM_PROXY_*`` configuration (optionally per-label).
    """
    accounts: dict[str, TelegramClient] = {}

    prefix_str = "TELEGRAM_SESSION_STRING_"
    prefix_name = "TELEGRAM_SESSION_NAME_"

    for key, value in os.environ.items():
        if key.startswith(prefix_str) and value:
            label = key[len(prefix_str) :].lower()
            accounts[label] = _build_client(StringSession(value), label)
        elif key.startswith(prefix_name) and value:
            label = key[len(prefix_name) :].lower()
            accounts[label] = _build_client(value, label)

    # Backward-compatible unsuffixed variables. A pool (TELEGRAM_SESSION_STRINGS)
    # takes precedence for the default account and claims a free session slot.
    session_pool = _parse_session_pool()
    session_string = os.getenv("TELEGRAM_SESSION_STRING")
    session_name = os.getenv("TELEGRAM_SESSION_NAME")

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


def get_client(account: str = None) -> TelegramClient:
    """Resolve account label to TelegramClient."""
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
      concurrently, prefixes each result with [label], concatenates.
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
                return label, await fn(*args, **kw)

            results = await asyncio.gather(*(_call_for(label) for label in clients))
            return "\n\n".join(f"[{label}]\n{result}" for label, result in results)

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
        reconnect_logger = logging.getLogger("telegram_mcp")
        reconnect_logger.warning("Forcing reconnect...")
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
            raise RuntimeError(_BURNED_SESSION_MESSAGE) from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Reconnecting to Telegram timed out after {_RECONNECT_TIMEOUT:.0f}s."
            ) from exc
        if not await cl.is_user_authorized():
            reconnect_logger.error(
                "Client not authorized after reconnect; refusing interactive login"
            )
            # A raise, not a call to Telethon's start(). That method defaults to
            # `phone=lambda: input(...)` — a synchronous read inside a coroutine, which
            # blocks the whole event loop, and on stdio it reads the same stdin the MCP
            # protocol speaks over. Wrapping it in asyncio.wait_for cannot save us: the
            # timeout is scheduled on the very loop input() has stopped, so it never
            # fires. The server would hang silently and permanently. runner.py refuses
            # the same thing at startup for the same reason.
            raise RuntimeError(
                "Telegram session is no longer authorized. Interactive phone login is "
                "disabled for the MCP server because it runs over stdio. Regenerate the "
                "session with `uv run session_string_generator.py` and update "
                "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env; "
                "`Manage-Accounts.ps1` does both for a labelled account."
            )
        _last_conn_verified[key] = time.time()
        reconnect_logger.warning("Forced reconnect successful")


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
        raise RuntimeError(_BURNED_SESSION_MESSAGE) from exc
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


# Setup robust logging with both file and console output
logger = logging.getLogger("telegram_mcp")
logger.setLevel(logging.ERROR)  # Set to ERROR for production, INFO for debugging

# Create console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)  # Set to ERROR for production, INFO for debugging

# Create file handler with absolute path. Keep the legacy location next to
# top-level main.py, even though runtime code now lives inside telegram_mcp/.
package_dir = os.path.dirname(os.path.abspath(__file__))
script_dir = os.path.dirname(package_dir)
log_file_path = os.path.join(script_dir, "mcp_errors.log")

try:
    file_handler = logging.FileHandler(log_file_path, mode="a")  # Append mode
    file_handler.setLevel(logging.ERROR)

    # Create formatters
    # Console formatter remains in the old format
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    console_handler.setFormatter(console_formatter)

    # File formatter is now JSON
    json_formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler.setFormatter(json_formatter)

    # Add handlers to logger
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.info(f"Logging initialized to {log_file_path}")
except Exception as log_error:
    print(f"WARNING: Error setting up log file: {log_error}", file=sys.stderr)
    # Fallback to console-only logging
    logger.addHandler(console_handler)
    logger.error(f"Failed to set up log file handler: {log_error}")


__all__ = [
    "_BURNED_SESSION_MESSAGE",
    "_CONN_VERIFY_INTERVAL",
    "_PROXY_TYPES_ALL",
    "_PROXY_TYPES_SOCKS_HTTP",
    "_RECONNECT_LOCKS",
    "_RECONNECT_TIMEOUT",
    "_SESSION_LOCKS",
    "_acquire_session",
    "_build_client",
    "_build_proxy_for_label",
    "_discover_accounts",
    "_force_reconnect",
    "_get_proxy_env",
    "_last_conn_verified",
    "_parse_session_pool",
    "clients",
    "console_handler",
    "ensure_connected",
    "get_client",
    "is_multi_mode",
    "log_file_path",
    "logger",
    "package_dir",
    "script_dir",
    "with_account",
]
