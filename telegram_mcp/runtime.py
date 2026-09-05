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
import weakref
import zlib
from contextlib import contextmanager
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Dict, Optional, Union, Any
from pathlib import Path
from urllib.parse import unquote, urlparse

# Third-party libraries
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import Annotations, TextContent, ToolAnnotations
from mcp.shared.exceptions import MCPError
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
import traceback

# The error layer moved next door. Re-exported because every tools module
# does `from telegram_mcp.runtime import *` and reaches these names there.
from telegram_mcp.errors import (  # noqa: F401  (re-exported)
    ErrorCategory,
    PeerNotFound,
    TELEGRAM_REFUSALS,
    _is_schema_drift,
    _telegram_refusal,
    log_and_format_error,
    validate_id,
)

try:
    import fcntl  # POSIX advisory locks; unavailable on Windows
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from telegram_mcp.safe_log import (  # noqa: F401 - historic re-exports
    log_event,
    safe_exception,
    safe_value,
    _safe_context_value,
    _safe_exception,
)
from telegram_mcp.singleton import try_lock_exclusive

from functools import wraps
import telethon.errors.rpcerrorlist
from sanitize import sanitize_user_content, sanitize_name, sanitize_dict, format_tool_result
from telegram_mcp.client_identity import client_identity_kwargs

# Every name above is part of this module's PUBLIC surface, not just its own working
# set. `__all__` below is computed from `globals()`, and 31 tool modules reach these
# through `from telegram_mcp.runtime import *` without importing them again - so an
# import that looks unused here is one a consumer is using bare. Pruning them by
# flake8's F401 removes 29 names that other modules depend on, and static analysis
# cannot see the breakage because a star import makes it stop guessing. Do not tidy
# this block; add to it deliberately.


def json_serializer(obj):
    """Helper function to convert non-serializable objects for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        # Same decode-then-sanitize as `sanitize.sanitize_dict`: this is the other
        # last line of defence, and fixing only one of the two moves the gap.
        return sanitize_user_content(obj.decode("utf-8", errors="replace"), max_length=4096)
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
    """Return a Telethon-compatible marked ID for an entity.

    `None` is refused by name. Telethon's `get_me()` answers None for a client
    that is connected but no longer authorised, and that None used to arrive
    here and fail as `'NoneType' object has no attribute 'id'` -- a message that
    names neither the entity nor the reason, so the reader looks at the wrong
    half of the system. Every caller formats something it fetched; saying which
    fetch came back empty is the difference between a lead and a puzzle.
    """
    if entity is None:
        raise ValueError(
            "No entity to format: the lookup returned nothing. For your own account "
            "that means this login is no longer authorised - the session was replaced "
            "or revoked since the server started."
        )
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


# The shared HTTP service can be consumed by long-lived MCP clients. Stateless requests keep
# those clients usable across server-process restarts instead of rejecting their next call
# with "No valid session ID provided". Stdio transport remains unaffected.
def _server_version() -> str:
    """This package's version, for the `serverInfo` a client is shown.

    FastMCP filled this in on its own; `MCPServer` takes it explicitly, and
    passing nothing makes the handshake report `"version": ""` - an empty string
    is worse than a wrong one, because a client renders it as a blank rather than
    falling back. Reporting THIS package's version is also more useful than the
    SDK's, which is what 1.x happened to show.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("telegram-mcp")
    except Exception:  # not installed as a distribution, e.g. a source checkout
        return "0+unknown"


# `stateless_http` was a constructor argument under FastMCP; in mcp 2.x it is a
# parameter of run_streamable_http_async, so it moved to `runner._serve`.
mcp = MCPServer("telegram", version=_server_version())

# Annotate all tool results with audience=["user"] so MCP clients know
# the content is user-generated data, not instructions for the model.
# Installed as server middleware so it runs for every tools/call, whatever the
# transport, and injects annotations into the final CallToolResult while
# preserving structured output.
_USER_AUDIENCE = Annotations(audience=["user"])


def _annotate_for_user(content: list) -> list:
    """Mark every unannotated content block as user data.

    Every block type MCP defines carries an ``annotations`` field - text,
    image, audio, resource link and embedded resource alike - and a tool that
    returns a screenshot is handing back user data exactly as much as one that
    returns a message body. This used to test ``isinstance(block, TextContent)``,
    so an image came back with nothing said about it at all. Asking the model
    for the field instead of listing the classes also means a block type added
    by a later MCP release is covered on arrival.
    """
    annotated = []
    for block in content:
        fields = getattr(type(block), "model_fields", {})
        if "annotations" in fields and getattr(block, "annotations", None) is None:
            block = block.model_copy(update={"annotations": _USER_AUDIENCE})
        annotated.append(block)
    return annotated


class _UserAudienceMiddleware:
    """Stamp every tool result as user data, on the way out.

    Under mcp 1.x this reached into `mcp._mcp_server.request_handlers` and
    replaced the `CallToolRequest` entry. 2.x removed `_mcp_server`; the
    supported seam is the server's middleware chain, which every request passes
    through regardless of transport. That is a better fit than it looks: the old
    hook could only ever see the one handler it swapped, while middleware sees
    the result whatever produced it.

    The unwrapping is deliberately permissive. A result may arrive as a bare
    `CallToolResult` or wrapped in a `ServerResult`, and this must annotate
    either without caring which - a shape it does not recognise is passed
    through untouched rather than dropped, because failing to annotate is a
    smaller harm than swallowing a tool's answer.
    """

    async def __call__(self, ctx, call_next):
        from mcp.types import CallToolResult

        result = await call_next(ctx)

        target = result
        if not isinstance(target, CallToolResult):
            target = getattr(result, "root", None)
        if isinstance(target, CallToolResult) and target.content:
            target.content = _annotate_for_user(target.content)
        return result


def _install_annotation_hook() -> None:
    """Append the stamper to the middleware chain, exactly once."""
    if any(isinstance(m, _UserAudienceMiddleware) for m in mcp.middleware):
        return
    mcp.middleware.append(_UserAudienceMiddleware())


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


def _is_read_only(annotations) -> bool:
    """Whether a tool declares itself read-only, under either SDK spelling.

    This decides what `TELEGRAM_EXPOSED_TOOLS=read-only` KEEPS, so reading the
    wrong attribute name is not cosmetic. mcp 1.x spelled the field
    `readOnlyHint`; 2.x renamed it `read_only_hint` and kept the old spelling
    only as a construction alias - so every `ToolAnnotations(readOnlyHint=True)`
    in this codebase still builds, while `getattr(a, "readOnlyHint", False)`
    silently returned the default for every tool. Read-only mode would have
    stripped the entire registry.

    Both names are accepted so the check cannot break again on whichever
    spelling the installed SDK happens to use, and the default stays False:
    a tool that does not clearly say it is read-only is not treated as one.
    """
    for attribute in ("read_only_hint", "readOnlyHint"):
        value = getattr(annotations, attribute, None)
        if value is not None:
            return bool(value)
    return False


def _apply_exposed_tools_mode(server: MCPServer = mcp, mode: Optional[str] = None) -> list[str]:
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
        if not _is_read_only(annotations):
            server._tool_manager.remove_tool(tool.name)
            removed.append(tool.name)
    return removed


# The account/connection layer. Re-exported so the tool modules' star imports keep
# working - but patch it at the source, not through this name.
from telegram_mcp.settings import (  # noqa: F401,E402
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    ValidationError,
    _parse_bool_env,
)
from telegram_mcp.connection import *  # noqa: F401,F403,E402

# The module, not the star-imported `clients` dict: the registry is rebound (by a
# test, by a reload), and a name bound here at import time would keep pointing at
# whatever it held then. Underscored so `from runtime import *` does not push a
# module named `connection` into every tool's namespace.
from telegram_mcp import connection as _connection  # noqa: E402

# Telegram's own "this request does not apply here" answers. They are not failures
# of ours and not transport errors: the server understood the request and declined
# it because of what the peer IS. Reported as a generic code they are indistinguishable
# from a bug, which sent an hour of debugging the wrong way; each one below was
# observed against a real account, not copied from a list.


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


# One dialog warm per burst, not one per miss.
#
# get_dialogs() downloads the ENTIRE dialog list, and it is the most expensive
# call Telethon makes on a large account. Several tools resolve peers in a loop -
# get_folder does it once per include_peer, exclude_peer and pinned_peer - so a
# folder holding cold peers used to pay one full download PER PEER and still
# render "Unknown" for each. create_group, invite_to_group and set_privacy_settings
# have the same shape.
#
# A TTL rather than a boolean: a peer that appears AFTER the warm still has to
# become resolvable, and "warmed once at startup" would make that never happen.
# Short enough that a new chat is reachable within seconds; long enough that one
# tool call's loop shares a single download.
_DIALOG_WARM_SECONDS = 30.0
_dialog_warmed: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


async def _warm_dialogs_once(client) -> bool:
    """Warm the entity cache at most once per _DIALOG_WARM_SECONDS per client.

    Returns True when a warm actually happened, so a caller can skip the retry
    that would otherwise ask the same question against an unchanged cache.
    """
    now = time.monotonic()
    last = _dialog_warmed.get(client)
    if last is not None and now - last < _DIALOG_WARM_SECONDS:
        return False
    _dialog_warmed[client] = now
    await client.get_dialogs()
    return True


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
            # A skipped warm means the cache is unchanged since the last one, so
            # the retry below would ask the same question and get the same answer.
            if await _warm_dialogs_once(client):
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
            if await _warm_dialogs_once(client):
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

    raise PeerNotFound(
        f"Could not resolve {label} for {identifier!r}, "
        f"including marked variants {_marked_id_candidates(identifier)}"
    ) from last_error


def _account_for_client(client) -> Optional[str]:
    """Which login a client belongs to, for callers that hold only the client.

    Most tools call `resolve_entity(chat_id, cl)` and never pass their label on.
    Reverse-looking the client up is what makes every one of them scope its
    aliases correctly without touching a single call site - and a client from
    nowhere (a test fake, a hand-built one) resolves nothing rather than
    borrowing another account's contacts.
    """
    for label, candidate in _connection.clients.items():
        if candidate is client:
            return label
    return None


async def _resolve(
    getter: str,
    identifier: Union[int, str],
    client,
    label: str,
    account: Optional[str] = None,
) -> Any:
    """Resolve an identifier, turning a failed free-text reference into a question.

    A saved alias resolves here as well as in @validate_id, so tools without that
    decorator understand nicknames too.
    """
    original = identifier
    if client is None:
        client = get_client()
    account = effective_account(account) or _account_for_client(client)
    identifier = apply_alias(identifier, account)
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
        needs_user = alias_failure(original, identifier, account)
        if needs_user:
            raise needs_user from error
        raise


async def resolve_entity(identifier: Union[int, str], client=None, account: str = None) -> Any:
    """Resolve an entity, warming the cache and retrying as needed.

    Accepts IDs, usernames, phone numbers, and saved contact aliases. `account`
    is optional: it is inferred from `client` when a caller does not pass it.
    """
    return await _resolve("get_entity", identifier, client, "entity", account)


async def resolve_input_entity(
    identifier: Union[int, str], client=None, account: str = None
) -> Any:
    """Like resolve_entity() but returns an InputPeer."""
    return await _resolve("get_input_entity", identifier, client, "input entity", account)


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
