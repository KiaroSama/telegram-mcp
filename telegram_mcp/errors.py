"""Turning an exception into something a caller can act on.

Split out of ``runtime.py``, which was the whole shared surface at once. This is
the error half, and it is a LOWER layer: it imports nothing from ``runtime``, so
the dependency runs one way.

What it holds is this project's answer to a specific failure mode - an MCP client
sees only the string a tool returns, so an unclassified exception reaches the
model as prose it cannot reason about. ``ErrorCategory`` gives every failure a
stable machine-readable class, ``log_and_format_error`` writes the diagnostic
detail to the log and returns only what is safe to say, ``TELEGRAM_REFUSALS``
maps the refusals Telegram states plainly onto that plain statement, and
``validate_id`` refuses a malformed identifier before it can reach Telegram.

``PeerNotFound`` lives here rather than upstairs because it IS an error, and
because ``log_and_format_error`` classifies it - a resolution failure's useful
answer is "that chat is not visible to this account", not a traceback.
"""

import logging
import zlib
from enum import Enum
from functools import wraps
from typing import Optional, Union

from telegram_mcp.aliases import (
    AliasID,
    AliasNeedsUser,
    alias_ask_payload,
    apply_alias,
    effective_account,
    is_handle_like,
)
from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import ValidationError


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


TELEGRAM_REFUSALS: dict[str, str] = {
    "BroadcastForbiddenError": (
        "Telegram does not allow this in a broadcast channel. Reaction COUNTS are "
        "visible there, but the list of who reacted is not - that exists only in "
        "groups and private chats."
    ),
    "ChatAdminRequiredError": (
        "This needs admin rights in that chat, and this account does not have them."
    ),
    "UserNotParticipantError": (
        "This account is not a member of that chat, so the request does not apply."
    ),
}


def _telegram_refusal(error: Exception) -> Optional[str]:
    """Telegram's own refusal, as a sentence, or None when it is not one of them."""
    return TELEGRAM_REFUSALS.get(type(error).__name__)


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

    # Same reasoning: an account that cannot see a peer is an ANSWER, not a
    # failure of this server. Under the read-only fan-out it is the ordinary
    # outcome for every account that is not in the chat, and an error code there
    # tells the operator nothing they can act on.
    if isinstance(error, PeerNotFound):
        return (
            "This account cannot see that chat, user or channel. Check the id or "
            "username, or use an account that is a member."
        )

    # A rate limit is an INSTRUCTION, not a failure to report and move on from.
    # An agent handed a generic error code retries, and every retry inside the
    # window extends the penalty - the failure mode this exists to prevent is a
    # model politely hammering Telegram until the account is limited for hours.
    # So the seconds are named and the no-retry is explicit.
    #
    # Placed beside the other two cases this function already treats as answers
    # rather than faults.
    seconds = getattr(error, "seconds", None)
    if seconds is not None and type(error).__name__.startswith("FloodWait"):
        log_event(logging.WARNING, "Rate limited", tool=function_name, wait_seconds=int(seconds))
        if user_message:
            return user_message
        return (
            f"Telegram is rate-limiting this account: it requires {int(seconds)} seconds "
            "before this operation is repeated. Do NOT retry before then - each early "
            "attempt extends the wait. Other accounts and other operations are unaffected."
        )

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
        # crc32, not hash(): CPython salts str.__hash__ per process (PEP 456), so the
        # same tool produced a different code after every restart — this log holds
        # GEN-ERR-256 and GEN-ERR-679 for one get_full_user failure. A code whose whole
        # purpose is to correlate a user's report with a log line has to survive one.
        error_code = f"{prefix_str}-ERR-{zlib.crc32(function_name.encode('utf-8')) % 1000:03d}"

    # One bounded line through the only primitive that writes one. The tool name
    # and the error code are both this project's own strings, so they go in the
    # event itself: a caller quoting a code in a bug report has to be able to
    # grep for it.
    log_event(
        logging.ERROR, f"Error in {function_name} (code {error_code})", error=error, **kwargs
    )

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

    refusal = _telegram_refusal(error)
    if refusal:
        return f"{refusal} (code: {error_code})"

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
            # The login this call runs as, so a saved alias is resolved against the
            # account that saved it. @with_account has already filled this in for a
            # read-only fan-out; an omitted one means the sole configured login.
            account = effective_account(kwargs.get("account"))
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
                            resolved = apply_alias(value, account)
                            if isinstance(resolved, int):
                                # Keep the wording: if the mapping turns out to be
                                # stale, the resolver must name it, not the bare id.
                                return AliasID(resolved, value), None
                            if is_handle_like(value):
                                return value, None
                            # Unknown or ambiguous reference: hand the agent an
                            # instruction to ask the user instead of a dead end.
                            return None, alias_ask_payload(value, account=account)

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


class PeerNotFound(ValueError):
    """This account cannot see that chat, user or channel.

    Normal control flow, not a failure of this server: under the read-only
    fan-out it is the ordinary answer for every account that is not in the chat.
    Rendered as an error CODE it told the operator nothing and looked like a bug -
    `list_topics` came back as GEN-ERR-911 for one account while `list_chats`
    answered the same situation in a sentence.

    Subclasses ValueError so every existing `except ValueError` around the
    resolver keeps working.
    """
