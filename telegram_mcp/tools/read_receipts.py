"""Read receipts, both directions: marking a chat read, and who has read a message.

``mark_as_read`` moves this account's own read cursor, which the sender sees as
"read" - a seen signal, so the safeguard asks for it while ghost mode is on.
``get_message_read_by`` answers the reverse question for a message this account
sent. Both used to live with the readers they sit beside (``messages_read``,
``chats``); they moved here together because both files had passed the 800-line
ceiling and this is one responsibility, not two leftovers.
"""

from telegram_mcp.runtime import *


@mcp.tool(
    annotations=ToolAnnotations(
        title="Mark As Read",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=True,
        readOnlyHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def mark_as_read(chat_id: Union[int, str], account: str = None) -> str:
    """
    Mark all messages as read in a chat.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        await cl.send_read_acknowledge(entity)
        return f"Marked all messages as read in chat {chat_id}."
    except Exception as e:
        return log_and_format_error("mark_as_read", e, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Message Read By",
        openWorldHint=True,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_message_read_by(
    chat_id: Union[int, str], message_id: int, account: str = None
) -> str:
    """
    List user IDs who have read a specific message.

    Works in small groups and supergroups where read-marker tracking is
    enabled (Telegram exposes read receipts for groups up to a fixed size
    and only for messages sent within the last ~7 days).

    Args:
        chat_id: The chat ID or username.
        message_id: The message ID to check read receipts for.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        from telethon.errors.rpcerrorlist import (
            ChatAdminRequiredError,
            UserNotParticipantError,
            MsgTooOldError,
            PeerIdInvalidError,
        )

        entity = await resolve_entity(chat_id, cl)
        try:
            result = await cl(
                functions.messages.GetMessageReadParticipantsRequest(
                    peer=entity, msg_id=message_id
                )
            )
        except MsgTooOldError:
            return (
                f"Read receipts unavailable for message {message_id} in chat "
                f"{chat_id}: message is too old or read receipts are disabled."
            )
        except ChatAdminRequiredError:
            return (
                f"Cannot read receipts for message {message_id} in chat {chat_id}: "
                f"admin rights are required."
            )
        except UserNotParticipantError:
            return (
                f"Cannot read receipts for message {message_id} in chat {chat_id}: "
                f"you are not a participant of this chat."
            )
        except PeerIdInvalidError:
            return f"Invalid chat: {chat_id}."

        # result is a list of ReadParticipantDate objects in newer Telethon,
        # or a list of user IDs (ints) in older layers. Handle both.
        if not result:
            return f"No read receipts available for message {message_id} in chat " f"{chat_id}."

        readers = []
        for item in result:
            if hasattr(item, "user_id"):
                readers.append(
                    {
                        "user_id": item.user_id,
                        "read_at": item.date.isoformat() if getattr(item, "date", None) else None,
                    }
                )
            else:
                # Older layer: plain int
                readers.append({"user_id": item, "read_at": None})

        return json.dumps(
            {
                "chat_id": str(chat_id),
                "message_id": message_id,
                "read_by": readers,
                "count": len(readers),
            },
            indent=2,
            default=json_serializer,
        )
    except Exception as e:
        return log_and_format_error(
            "get_message_read_by", e, chat_id=chat_id, message_id=message_id
        )


__all__ = ["mark_as_read", "get_message_read_by"]
