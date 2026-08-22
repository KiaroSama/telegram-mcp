import argparse
import os
import sys
import json
import time
import asyncio
import sqlite3
import logging
import mimetypes
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Dict, Optional, Union, Any
from pathlib import Path
from urllib.parse import unquote, urlparse

# Third-party libraries
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import Annotations, TextContent, ToolAnnotations
from mcp.shared.exceptions import McpError
from pythonjsonlogger import jsonlogger
from telethon import TelegramClient, functions, types, utils
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession
from telethon.tl.types import (
    User,
    Chat,
    Channel,
    ChatAdminRights,
    ChatBannedRights,
    ChannelParticipantsKicked,
    ChannelParticipantsAdmins,
    InputChatPhoto,
    InputChatUploadedPhoto,
    InputChatPhotoEmpty,
    InputPeerUser,
    InputPeerChat,
    InputPeerChannel,
    DialogFilter,
    DialogFilterChatlist,
    DialogFilterDefault,
    TextWithEntities,
)
import re
import hashlib
import tempfile

try:
    import fcntl  # POSIX advisory locks; unavailable on Windows
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from telegram_mcp.singleton import try_lock_exclusive

from functools import wraps
import telethon.errors.rpcerrorlist
from sanitize import sanitize_user_content, sanitize_name, sanitize_dict, format_tool_result
from telegram_mcp.client_identity import client_identity_kwargs


class ValidationError(Exception):
    """Custom exception for validation errors."""

    pass


def json_serializer(obj):
    """Helper function to convert non-serializable objects for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    # Add other non-serializable types as needed
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def get_entity_type(entity: Any) -> str:
    """Return a normalized, human-readable chat/entity type."""
    if isinstance(entity, User):
        return "User"
    if isinstance(entity, Chat):
        return "Group (Basic)"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        return "Channel" if getattr(entity, "broadcast", False) else "Group"
    return type(entity).__name__


def get_marked_id(entity: Any) -> int:
    """Return a Telethon-compatible marked ID for an entity."""
    if isinstance(entity, Channel):
        return -1000000000000 - entity.id
    if isinstance(entity, Chat):
        return -entity.id
    return entity.id


def get_entity_filter_type(entity: Any) -> Optional[str]:
    """Return list_chats-compatible filter type: user/group/channel."""
    entity_type = get_entity_type(entity)
    if entity_type == "User":
        return "user"
    if entity_type in ("Group (Basic)", "Group", "Supergroup"):
        return "group"
    if entity_type == "Channel":
        return "channel"
    return None


load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")

# The shared HTTP service can be consumed by long-lived MCP clients. Stateless requests keep
# those clients usable across server-process restarts instead of rejecting their next call
# with "No valid session ID provided". Stdio transport remains unaffected.
mcp = FastMCP("telegram", stateless_http=True)

# Annotate all tool results with audience=["user"] so MCP clients know
# the content is user-generated data, not instructions for the model.
# We wrap the low-level request handler (after FastMCP registers it) to inject
# annotations into the final CallToolResult, preserving structured output.
_USER_AUDIENCE = Annotations(audience=["user"])


def _install_annotation_hook() -> None:
    from mcp.types import CallToolRequest, ServerResult, CallToolResult

    original_handler = mcp._mcp_server.request_handlers[CallToolRequest]

    async def annotated_handler(req):
        response = await original_handler(req)
        if isinstance(response, ServerResult) and isinstance(response.root, CallToolResult):
            content = response.root.content
            if content:
                response.root.content = [
                    (
                        block.model_copy(update={"annotations": _USER_AUDIENCE})
                        if isinstance(block, TextContent) and block.annotations is None
                        else block
                    )
                    for block in content
                ]
        return response

    mcp._mcp_server.request_handlers[CallToolRequest] = annotated_handler


_install_annotation_hook()


_EXPOSED_TOOLS_MODES = {"all", "read-only"}
_EXPOSED_TOOLS_ALLOW_SEPARATOR = "+"


def _split_exposed_tools_mode(mode: str) -> tuple[str, list[str]]:
    """Split a normalised exposure mode into its base mode and write allowlist."""
    base, separator, raw_allowlist = mode.partition(_EXPOSED_TOOLS_ALLOW_SEPARATOR)
    if not separator:
        return base, []
    return base, [name.strip() for name in raw_allowlist.split(",") if name.strip()]


def _get_exposed_tools_mode(value: Optional[str] = None) -> str:
    """Return the configured MCP tool exposure mode.

    ``TELEGRAM_EXPOSED_TOOLS=read-only`` keeps only tools annotated with
    ``readOnlyHint=True``. ``read-only+send_message,reply_to_message`` keeps
    those plus the named write tools. The default is ``all`` for backward
    compatibility.
    """
    raw_value = os.getenv("TELEGRAM_EXPOSED_TOOLS", "all") if value is None else value
    mode = raw_value.strip().lower()
    base_mode, allowlist = _split_exposed_tools_mode(mode)
    if base_mode not in _EXPOSED_TOOLS_MODES:
        accepted = ", ".join(sorted(_EXPOSED_TOOLS_MODES))
        raise SystemExit(
            f"Invalid TELEGRAM_EXPOSED_TOOLS '{raw_value}'. Expected one of: {accepted}."
        )
    if _EXPOSED_TOOLS_ALLOW_SEPARATOR not in mode:
        return base_mode
    if base_mode != "read-only":
        raise SystemExit(
            f"Invalid TELEGRAM_EXPOSED_TOOLS '{raw_value}'. The "
            f"'{_EXPOSED_TOOLS_ALLOW_SEPARATOR}tool,tool' allowlist is only valid "
            "with read-only."
        )
    if not allowlist:
        raise SystemExit(
            f"Invalid TELEGRAM_EXPOSED_TOOLS '{raw_value}'. The "
            f"'{_EXPOSED_TOOLS_ALLOW_SEPARATOR}' allowlist must name at least one tool."
        )
    return f"{base_mode}{_EXPOSED_TOOLS_ALLOW_SEPARATOR}{','.join(allowlist)}"


def _apply_exposed_tools_mode(server: FastMCP = mcp, mode: Optional[str] = None) -> list[str]:
    """Prune registered MCP tools according to the configured exposure mode."""
    selected_mode = _get_exposed_tools_mode() if mode is None else _get_exposed_tools_mode(mode)
    base_mode, allowlist = _split_exposed_tools_mode(selected_mode)
    if base_mode == "all":
        return []

    registered = {tool.name for tool in server._tool_manager.list_tools()}
    unknown = sorted(set(allowlist) - registered)
    if unknown:
        # Fail loudly: a typo must not silently degrade into a narrower allowlist
        # that looks like it worked.
        raise SystemExit(
            f"Invalid TELEGRAM_EXPOSED_TOOLS allowlist: unknown tool(s) {', '.join(unknown)}."
        )

    allowed = set(allowlist)
    removed: list[str] = []
    for tool in list(server._tool_manager.list_tools()):
        if tool.name in allowed:
            continue
        annotations = getattr(tool, "annotations", None)
        if not getattr(annotations, "readOnlyHint", False):
            server._tool_manager.remove_tool(tool.name)
            removed.append(tool.name)
    return removed


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


def _parse_bool_env(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
_CONN_VERIFY_INTERVAL: float = 30.0  # seconds between live pings
_RECONNECT_TIMEOUT: float = 30.0  # seconds before a reconnect attempt is abandoned


async def _force_reconnect(cl: TelegramClient):
    """Force disconnect + reconnect regardless of is_connected() state."""
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
        raise RuntimeError(
            "Telegram session is no longer usable: the same session string was "
            "used by another client at the same time (AuthKeyDuplicatedError). "
            "Give each concurrent client its own session via "
            "TELEGRAM_SESSION_STRINGS or TELEGRAM_SESSION_STRING_<LABEL>, then "
            "regenerate the burned session with `uv run session_string_generator.py`."
        ) from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Reconnecting to Telegram timed out after {_RECONNECT_TIMEOUT:.0f}s."
        ) from exc
    if not await cl.is_user_authorized():
        reconnect_logger.warning("Client not authorized after reconnect, calling start()...")
        await asyncio.wait_for(cl.start(), timeout=_RECONNECT_TIMEOUT)
    _last_conn_verified[id(cl)] = time.time()
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
        _last_conn_verified[key] = now
    except (ConnectionError, OSError, asyncio.TimeoutError, Exception):
        await _force_reconnect(cl)


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


# Error code prefix mapping for better error tracing
class ErrorCategory(str, Enum):
    CHAT = "CHAT"
    MSG = "MSG"
    CONTACT = "CONTACT"
    GROUP = "GROUP"
    MEDIA = "MEDIA"
    PROFILE = "PROFILE"
    AUTH = "AUTH"
    ADMIN = "ADMIN"
    FOLDER = "FOLDER"


def log_and_format_error(
    function_name: str,
    error: Exception,
    prefix: Optional[Union[ErrorCategory, str]] = None,
    user_message: str = None,
    **kwargs,
) -> str:
    """
    Centralized error handling function.

    Logs an error and returns a formatted, user-friendly message.

    Args:
        function_name: Name of the function where the error occurred.
        error: The exception that was raised.
        prefix: Error code prefix (e.g., ErrorCategory.CHAT, "VALIDATION-001").
            If None, it will be derived from the function_name.
        user_message: A custom user-facing message to return. If None, a generic one is created.
        **kwargs: Additional context parameters to include in the log.

    Returns:
        A user-friendly error message with an error code.
    """
    # An ask-the-user instruction is normal control flow, not a failure: return it
    # verbatim and never log the user's nickname at ERROR level.
    if isinstance(error, AliasNeedsUser):
        return error.payload

    # Generate a consistent error code
    if isinstance(prefix, str) and prefix == "VALIDATION-001":
        # Special case for validation errors
        error_code = prefix
    else:
        if prefix is None:
            # Try to derive prefix from function name
            for category in ErrorCategory:
                if category.name.lower() in function_name.lower():
                    prefix = category
                    break

        prefix_str = prefix.value if isinstance(prefix, ErrorCategory) else (prefix or "GEN")
        error_code = f"{prefix_str}-ERR-{abs(hash(function_name)) % 1000:03d}"

    # Format the additional context parameters
    context = ", ".join(f"{k}={v}" for k, v in kwargs.items())

    # Log the full technical error
    logger.error(f"Error in {function_name} ({context}) - Code: {error_code}", exc_info=True)

    # Return a user-friendly message
    if user_message:
        return user_message

    # MTProto schema drift must not hide behind the generic code. Telethon releases lag
    # behind production Telegram, and when the server sends an object whose constructor
    # the installed schema does not know, the read buffer desynchronises: some tools fail
    # while their neighbours keep working. Reported as a generic error, that pattern is
    # indistinguishable from "no such user/chat" and sends debugging the wrong way.
    if _is_schema_drift(error):
        return (
            f"MTProto schema mismatch: the installed Telethon does not know an object the "
            f"server sent ({error}). This is NOT a missing user or chat — the data arrived, "
            f"parsing it failed. Upgrade Telethon; if it is already the latest release, its "
            f"schema is behind the current layer (code: {error_code})."
        )

    return f"An error occurred (code: {error_code}). Check mcp_errors.log for details."


def _is_schema_drift(error: Exception) -> bool:
    """True for TypeNotFoundError — the installed TL schema is older than what the server sends."""
    try:
        from telethon.errors.common import TypeNotFoundError
    except Exception:  # telethon missing or moved — not this helper's problem
        return False
    return isinstance(error, TypeNotFoundError)


def validate_id(*param_names_to_validate):
    """
    Decorator to validate chat_id and user_id parameters, including lists of IDs.
    It checks for valid integer ranges, string representations of integers,
    and username formats.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for param_name in param_names_to_validate:
                if param_name not in kwargs or kwargs[param_name] is None:
                    continue

                param_value = kwargs[param_name]

                def validate_single_id(value, p_name):
                    # Handle integer IDs
                    if isinstance(value, int):
                        if not (-(2**63) <= value <= 2**63 - 1):
                            return (
                                None,
                                f"Invalid {p_name}: {value}. ID is out of the valid integer range.",
                            )
                        return value, None

                    # Handle string IDs
                    if isinstance(value, str):
                        try:
                            int_value = int(value)
                            if not (-(2**63) <= int_value <= 2**63 - 1):
                                return (
                                    None,
                                    f"Invalid {p_name}: {value}. ID is out of the valid integer range.",
                                )
                            return int_value, None
                        except ValueError:
                            # Saved aliases are free text ("андрей бекендер"), so they must
                            # be resolved here: this decorator runs before the tool body
                            # ever reaches resolve_entity.
                            resolved = apply_alias(value)
                            if isinstance(resolved, int):
                                # Keep the wording: if the mapping turns out to be
                                # stale, the resolver must name it, not the bare id.
                                return AliasID(resolved, value), None
                            if is_handle_like(value):
                                return value, None
                            # Unknown or ambiguous reference: hand the agent an
                            # instruction to ask the user instead of a dead end.
                            return None, alias_ask_payload(value)

                    # Handle other invalid types
                    return (
                        None,
                        f"Invalid {p_name}: {value}. Type must be an integer or a string.",
                    )

                if isinstance(param_value, list):
                    validated_list = []
                    for item in param_value:
                        validated_item, error_msg = validate_single_id(item, param_name)
                        if error_msg:
                            return log_and_format_error(
                                func.__name__,
                                ValidationError(error_msg),
                                prefix="VALIDATION-001",
                                user_message=error_msg,
                                **{param_name: param_value},
                            )
                        validated_list.append(validated_item)
                    kwargs[param_name] = validated_list
                else:
                    validated_value, error_msg = validate_single_id(param_value, param_name)
                    if error_msg:
                        return log_and_format_error(
                            func.__name__,
                            ValidationError(error_msg),
                            prefix="VALIDATION-001",
                            user_message=error_msg,
                            **{param_name: param_value},
                        )
                    kwargs[param_name] = validated_value

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def format_entity(entity) -> Dict[str, Any]:
    """Helper function to format entity information consistently.

    Names and titles are sanitized to prevent prompt injection.
    """
    result = {"id": get_marked_id(entity)}

    if hasattr(entity, "title"):
        result["name"] = sanitize_name(entity.title)
        result["type"] = "group" if isinstance(entity, Chat) else "channel"
    elif hasattr(entity, "first_name"):
        name_parts = []
        if entity.first_name:
            name_parts.append(entity.first_name)
        if hasattr(entity, "last_name") and entity.last_name:
            name_parts.append(entity.last_name)
        result["name"] = sanitize_name(" ".join(name_parts))
        result["type"] = "user"
        if hasattr(entity, "username") and entity.username:
            result["username"] = entity.username
        if hasattr(entity, "phone") and entity.phone:
            result["phone"] = entity.phone

    return result


# Parse modes that request server-side rich formatting (tables, headings,
# formulas, collapsible sections — the June 2026 "Rich Messages" feature).
# Sending rich messages requires Telegram Premium on the account.
RICH_PARSE_MODES = {"rich", "rich_md", "rich_markdown", "rich_html"}


async def account_is_premium(client) -> bool:
    """Fresh Premium check at call time — Premium can expire or be bought anytime."""
    me = await client.get_me()
    return bool(getattr(me, "premium", False))


def make_rich_input(parse_mode: str, text: str):
    """Build the InputRichMessage payload for a rich parse mode."""
    if parse_mode == "rich_html":
        return types.InputRichMessageHTML(html=text)
    return types.InputRichMessageMarkdown(markdown=text)


def premium_required_result(action: str) -> str:
    """Structured refusal so the agent can degrade gracefully instead of sending garbage."""
    return json.dumps(
        {
            "sent": False,
            "reason": "telegram_premium_required",
            "detail": (
                f"{action} with rich formatting requires Telegram Premium on this account. "
                "Nothing was sent. Reformat without rich-only blocks (tables, headings, "
                "formulas) and retry with parse_mode='md' or 'html'."
            ),
        },
        ensure_ascii=False,
    )


def is_premium_rpc_error(error: Exception) -> bool:
    """True when Telegram rejected a call because the account lacks Premium."""
    return "PREMIUM" in getattr(error, "message", str(error)).upper()


async def _resolve_with_retries(
    getter: str, identifier: Union[int, str], client, label: str, try_marked: bool = True
):
    """Cache warming, reconnect, and marked-ID fallback shared by both resolvers.

    StringSession has no persistent entity cache, so a cold lookup raises ValueError;
    warming via get_dialogs() and retrying fixes it. A bare positive ID may also need
    Telethon's marked chat/channel variants.
    """
    await ensure_connected(client)
    get = getattr(client, getter)
    last_error = None
    try:
        try:
            return await get(identifier)
        except ValueError as error:
            last_error = error
            await client.get_dialogs()
            try:
                return await get(identifier)
            except ValueError as error:
                last_error = error
    except ConnectionError:
        await ensure_connected(client)
        try:
            return await get(identifier)
        except ValueError as error:
            last_error = error
            await client.get_dialogs()
            try:
                return await get(identifier)
            except ValueError as error:
                last_error = error

    if try_marked:
        for candidate in _marked_id_candidates(identifier):
            try:
                return await get(candidate)
            except ValueError as error:
                last_error = error

    raise ValueError(
        f"Could not resolve {label} for {identifier!r}, "
        f"including marked variants {_marked_id_candidates(identifier)}"
    ) from last_error


async def _resolve(getter: str, identifier: Union[int, str], client, label: str) -> Any:
    """Resolve an identifier, turning a failed free-text reference into a question.

    A saved alias resolves here as well as in @validate_id, so tools without that
    decorator understand nicknames too.
    """
    original = identifier
    identifier = apply_alias(identifier)
    if client is None:
        client = get_client()
    try:
        # An id that came from a saved alias is exact; guessing marked variants of
        # it could deliver to a completely unrelated chat.
        from_alias = identifier is not original
        return await _resolve_with_retries(
            getter, identifier, client, label, try_marked=not from_alias
        )
    except (ValueError, *_PEER_ERRORS) as error:
        # An unknown or stale nickname is a question for the user, not a dead end:
        # report the wording they used, never the opaque stored id.
        needs_user = alias_failure(original, identifier)
        if needs_user:
            raise needs_user from error
        raise


async def resolve_entity(identifier: Union[int, str], client=None) -> Any:
    """Resolve an entity, warming the cache and retrying as needed.

    Accepts IDs, usernames, phone numbers, and saved contact aliases.
    """
    return await _resolve("get_entity", identifier, client, "entity")


async def resolve_input_entity(identifier: Union[int, str], client=None) -> Any:
    """Like resolve_entity() but returns an InputPeer."""
    return await _resolve("get_input_entity", identifier, client, "input entity")


def format_message(message) -> Dict[str, Any]:
    """Helper function to format message information consistently.

    Message text is sanitized to prevent prompt injection.
    """
    result = {
        "id": message.id,
        "date": message.date.isoformat(),
        "text": sanitize_user_content(message.message),
    }

    if message.from_id:
        result["from_id"] = utils.get_peer_id(message.from_id)

    if message.media:
        result["has_media"] = True
        result["media_type"] = type(message.media).__name__

    return result


def get_sender_name(message) -> str:
    """Helper function to get sender name from a message.

    Returns a sanitized single-line display name to prevent prompt injection
    via crafted Telegram display names.
    """
    if not message.sender:
        return "Unknown"

    # Check for group/channel title first
    if hasattr(message.sender, "title") and message.sender.title:
        return sanitize_name(message.sender.title)
    elif hasattr(message.sender, "first_name"):
        # User sender
        first_name = getattr(message.sender, "first_name", "") or ""
        last_name = getattr(message.sender, "last_name", "") or ""
        full_name = f"{first_name} {last_name}".strip()
        return sanitize_name(full_name) if full_name else "Unknown"
    else:
        return "Unknown"


def get_sender_username(message) -> Optional[str]:
    """Public @username of the message sender, if any (sanitized)."""
    sender = getattr(message, "sender", None)
    username = getattr(sender, "username", None) if sender else None
    return sanitize_name(username) if username else None


def get_sender_info(message) -> str:
    """Sender display string: name (@username) [id=NNN].

    Always exposes a numeric id (sender or from_id) so a user can be reached via
    tg://user?id=<id> even when no public @username exists.
    """
    name = get_sender_name(message)
    username = get_sender_username(message)
    sid = getattr(message, "sender_id", None)
    suffix = ""
    if username:
        suffix += f" (@{username})"
    if sid:
        suffix += f" [id={sid}]"
    return f"{name}{suffix}"


def get_engagement_info(message) -> str:
    """Helper function to get engagement metrics (views, forwards, reactions) from a message."""
    engagement_parts = []
    views = getattr(message, "views", None)
    if views is not None:
        engagement_parts.append(f"views:{views}")
    forwards = getattr(message, "forwards", None)
    if forwards is not None:
        engagement_parts.append(f"forwards:{forwards}")
    reactions = getattr(message, "reactions", None)
    if reactions is not None:
        results = getattr(reactions, "results", None)
        total_reactions = sum(getattr(r, "count", 0) or 0 for r in results) if results else 0
        engagement_parts.append(f"reactions:{total_reactions}")
    return f" | {', '.join(engagement_parts)}" if engagement_parts else ""


def get_engagement_dict(message) -> Optional[Dict[str, Any]]:
    """Return engagement metrics as a dict for JSON-formatted tool results."""
    result = {}
    views = getattr(message, "views", None)
    if views is not None:
        result["views"] = views
    forwards = getattr(message, "forwards", None)
    if forwards is not None:
        result["forwards"] = forwards
    reactions = getattr(message, "reactions", None)
    if reactions is not None:
        results = getattr(reactions, "results", None)
        result["reactions"] = sum(getattr(r, "count", 0) or 0 for r in results) if results else 0
    return result if result else None


# Two subsystems that live in their own modules: the alias store and file-path
# security. Both are re-exported here so `from telegram_mcp.runtime import *`
# keeps its historic surface - but PATCH THEM AT THE SOURCE, not through this name.
from telegram_mcp.aliases import *  # noqa: F401,F403,E402
from telegram_mcp.file_roots import *  # noqa: F401,F403,E402

__all__ = [name for name in globals() if not name.startswith("__")]
