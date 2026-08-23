"""The invite-link lifecycle: minting links and redeeming them.

Every tool here handles an invite hash, which is what separates this module
from the direct ``invite_to_group`` in ``groups.py`` -- that one names its users
by ID and never touches a link.

Two halves. Minting: ``get_invite_link`` and ``export_chat_invite``, which ask
Telegram for a chat's link. Redeeming: ``join_chat_by_link`` and
``import_chat_invite``, which differ only in whether they are handed a full URL
or the bare hash. Both redeem paths strip the same ``+`` prefix, probe with
``CheckChatInvite`` before joining, and decode the same lifecycle states
(expired, invalid, already a participant) out of error text -- keeping them in
one file is what makes that duplicated decoding visible.

``get_invite_link`` and ``export_chat_invite`` overlap heavily and differ only
in their fallback tail; that is history rather than design, and it is left as
found.
"""

from telegram_mcp.runtime import *


@mcp.tool(
    annotations=ToolAnnotations(title="Get Invite Link", openWorldHint=True, readOnlyHint=True)
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_invite_link(chat_id: Union[int, str], account: str = None) -> str:
    """
    Get the invite link for a group or channel.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)

        # Try using ExportChatInviteRequest first
        try:
            from telethon.tl import functions

            result = await cl(functions.messages.ExportChatInviteRequest(peer=entity))
            return result.link
        except AttributeError:
            # If the function doesn't exist in the current Telethon version
            logger.warning("ExportChatInviteRequest not available, using alternative method")
        except Exception as e1:
            # If that fails, log and try alternative approach
            logger.warning(f"ExportChatInviteRequest failed: {e1}")

        # Alternative approach using cl.export_chat_invite_link
        try:
            invite_link = await cl.export_chat_invite_link(entity)
            return invite_link
        except Exception as e2:
            logger.warning(f"export_chat_invite_link failed: {e2}")

        # Last resort: Try directly fetching chat info
        try:
            if isinstance(entity, (Chat, Channel)):
                full_chat = await cl(functions.messages.GetFullChatRequest(chat_id=entity.id))
                if hasattr(full_chat, "full_chat") and hasattr(full_chat.full_chat, "invite_link"):
                    return full_chat.full_chat.invite_link or "No invite link available."
        except Exception as e3:
            logger.warning(f"GetFullChatRequest failed: {e3}")

        return "Could not retrieve invite link for this chat."
    except Exception as e:
        logger.exception(f"get_invite_link failed (chat_id={chat_id})")
        return log_and_format_error("get_invite_link", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(title="Export Chat Invite", openWorldHint=True, readOnlyHint=True)
)
@with_account(readonly=True)
@validate_id("chat_id")
async def export_chat_invite(chat_id: Union[int, str], account: str = None) -> str:
    """
    Export a chat invite link.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)

        # Try using ExportChatInviteRequest first
        try:
            from telethon.tl import functions

            result = await cl(functions.messages.ExportChatInviteRequest(peer=entity))
            return result.link
        except AttributeError:
            # If the function doesn't exist in the current Telethon version
            logger.warning("ExportChatInviteRequest not available, using alternative method")
        except Exception as e1:
            # If that fails, log and try alternative approach
            logger.warning(f"ExportChatInviteRequest failed: {e1}")

        # Alternative approach using cl.export_chat_invite_link
        try:
            invite_link = await cl.export_chat_invite_link(entity)
            return invite_link
        except Exception as e2:
            logger.warning(f"export_chat_invite_link failed: {e2}")
            return log_and_format_error("export_chat_invite", e2, chat_id=chat_id)

    except Exception as e:
        logger.exception(f"export_chat_invite failed (chat_id={chat_id})")
        return log_and_format_error("export_chat_invite", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Import Chat Invite", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
async def import_chat_invite(hash: str, account: str = None) -> str:
    """
    Import a chat invite by hash.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        # Remove any prefixes like '+' if present
        if hash.startswith("+"):
            hash = hash[1:]

        # Try checking the invite before joining
        try:
            from telethon.errors import (
                InviteHashExpiredError,
                InviteHashInvalidError,
                UserAlreadyParticipantError,
                ChatAdminRequiredError,
                UsersTooMuchError,
            )

            # Try to check invite info first (will often fail if not a member)
            invite_info = await cl(functions.messages.CheckChatInviteRequest(hash=hash))
            if hasattr(invite_info, "chat") and invite_info.chat:
                # If we got chat info, we're already a member
                chat_title = sanitize_name(getattr(invite_info.chat, "title", "Unknown Chat"))
                return f"You are already a member of this chat: {chat_title}"
        except Exception:
            # This often fails if not a member - just continue
            pass

        # Join the chat using the hash
        try:
            result = await cl(functions.messages.ImportChatInviteRequest(hash=hash))
            if result and hasattr(result, "chats") and result.chats:
                chat_title = sanitize_name(getattr(result.chats[0], "title", "Unknown Chat"))
                return f"Successfully joined chat: {chat_title}"
            return "Joined chat via invite hash."
        except Exception as join_err:
            err_str = str(join_err).lower()
            if "expired" in err_str:
                return "The invite hash has expired and is no longer valid."
            elif "invalid" in err_str:
                return "The invite hash is invalid or malformed."
            elif "already" in err_str and "participant" in err_str:
                return "You are already a member of this chat."
            elif "admin" in err_str:
                return "Cannot join this chat - requires admin approval."
            elif "too much" in err_str or "too many" in err_str:
                return "Cannot join this chat - it has reached maximum number of participants."
            else:
                raise  # Re-raise to be caught by the outer exception handler

    except Exception as e:
        logger.exception(f"import_chat_invite failed (hash={hash})")
        return log_and_format_error("import_chat_invite", e, hash=hash)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Join Chat By Link", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
async def join_chat_by_link(link: str, account: str = None) -> str:
    """
    Join a chat by invite link.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        # Extract the hash from the invite link
        if "/" in link:
            hash_part = link.split("/")[-1]
            if hash_part.startswith("+"):
                hash_part = hash_part[1:]  # Remove the '+' if present
        else:
            hash_part = link

        # Try checking the invite before joining
        try:
            # Try to check invite info first (will often fail if not a member)
            invite_info = await cl(functions.messages.CheckChatInviteRequest(hash=hash_part))
            if hasattr(invite_info, "chat") and invite_info.chat:
                # If we got chat info, we're already a member
                chat_title = sanitize_name(getattr(invite_info.chat, "title", "Unknown Chat"))
                return f"You are already a member of this chat: {chat_title}"
        except Exception:
            # This often fails if not a member - just continue
            pass

        # Join the chat using the hash
        result = await cl(functions.messages.ImportChatInviteRequest(hash=hash_part))
        if result and hasattr(result, "chats") and result.chats:
            chat_title = sanitize_name(getattr(result.chats[0], "title", "Unknown Chat"))
            return f"Successfully joined chat: {chat_title}"
        return "Joined chat via invite hash."
    except Exception as e:
        err_str = str(e).lower()
        if "expired" in err_str:
            return "The invite hash has expired and is no longer valid."
        elif "invalid" in err_str:
            return "The invite hash is invalid or malformed."
        elif "already" in err_str and "participant" in err_str:
            return "You are already a member of this chat."
        logger.exception(f"join_chat_by_link failed (link={link})")
        return f"Error joining chat: {e}"


__all__ = [
    "get_invite_link",
    "export_chat_invite",
    "import_chat_invite",
    "join_chat_by_link",
]
