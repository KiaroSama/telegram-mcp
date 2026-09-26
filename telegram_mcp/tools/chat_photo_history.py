"""replace_chat_photo: a group's or channel's new photo, with the old ones gone (FR-009).

Telegram keeps every earlier chat photo as a "photo changed" service message, and a
chat's photo list is built from those messages - so `edit_chat_photo` alone leaves
every previous photo in the list. This tool sets the new one, then deletes exactly
the photo messages that were there before it.
"""

from telethon.tl.types import (
    Channel,
    Chat,
    InputChatUploadedPhoto,
    InputMessagesFilterChatPhotos,
    MessageActionChatEditPhoto,
)

from telegram_mcp.runtime import *


async def _photo_message_ids(cl, entity) -> list:
    """Ids of the chat's "photo changed" messages, newest first."""
    # ponytail: one page of 100; a chat with more photo changes keeps the rest.
    messages = await cl.get_messages(entity, limit=100, filter=InputMessagesFilterChatPhotos)
    return [
        m.id
        for m in messages
        if isinstance(getattr(m, "action", None), MessageActionChatEditPhoto)
    ]


@mcp.tool(
    annotations=ToolAnnotations(
        title="Replace Chat Photo",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=False,
        readOnlyHint=False,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def replace_chat_photo(
    chat_id: Union[int, str],
    file_path: str = None,
    ctx: Optional[Context] = None,
    account: str = None,
) -> str:
    """
    Replace a group's or channel's photo so only the new one remains in its photo list.

    Sets the new photo first, then deletes every earlier "photo changed" message -
    those are the old photos people see in the chat's photo list. Nothing is deleted
    if the new photo could not be set. Without `file_path`, the current photo stays
    and only the older ones are deleted. Needs admin rights to change the photo and
    delete messages.

    Args:
        chat_id: The group or channel.
        file_path: The new picture. Omit it to only remove the older photos.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        entity = await resolve_entity(chat_id, cl)
        if not isinstance(entity, (Channel, Chat)):
            return f"{chat_id} is not a group or channel; use set_profile_photo for an account."
        before = await _photo_message_ids(cl, entity)
        if file_path is None:
            old = before[1:]  # the newest is the current photo
        else:
            async with _open_verified_source(
                raw_path=file_path, ctx=ctx, tool_name="replace_chat_photo"
            ) as (source, path_error):
                if path_error:
                    return path_error
                uploaded = InputChatUploadedPhoto(file=await cl.upload_file(source.handle))
                if isinstance(entity, Channel):
                    await cl(functions.channels.EditPhotoRequest(channel=entity, photo=uploaded))
                else:
                    await cl(
                        functions.messages.EditChatPhotoRequest(chat_id=entity.id, photo=uploaded)
                    )
            old = before
        if old:
            await cl.delete_messages(entity, old, revoke=True)
        action = "photo replaced" if file_path is not None else "current photo kept"
        return f"Chat {chat_id}: {action}; {len(old)} older photo(s) deleted."
    except Exception as e:
        return log_and_format_error("replace_chat_photo", e, chat_id=chat_id, file_path=file_path)
