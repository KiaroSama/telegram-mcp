"""Identity MCP tools: an owned bot's profile text, and the owner's own @username.

Names, bios and photos of the account itself live in `profile.py`; group and
channel titles, photos and usernames in `groups.py` and `channel_admin.py`.
"""

from telethon.errors import RPCError

from telegram_mcp.runtime import *


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Bot Info",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=True,
        readOnlyHint=False,
    )
)
@with_account(readonly=False)
@validate_id("bot")
async def set_bot_info(
    bot: Union[int, str],
    name: str = None,
    about: str = None,
    description: str = None,
    lang_code: str = "",
    account: str = None,
) -> str:
    """
    Change a bot this account owns: its name, about text and description.

    Each field is optional and only the ones given change. `about` is the short
    text on the bot's profile; `description` is what a new user sees before
    pressing Start. The bot's @username cannot be changed here - only BotFather
    does that.

    Args:
        bot: The owned bot (ID or @username). Telegram refuses a bot you do not own.
        name: New display name.
        about: New profile "about" text.
        description: New description shown in an empty chat with the bot.
        lang_code: Two-letter language to set it for; empty means every language.
    """
    changed = [
        field
        for field, value in (("name", name), ("about", about), ("description", description))
        if value is not None
    ]
    if not changed:
        return "set_bot_info needs at least one of name, about or description."
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        target = await resolve_input_entity(bot, cl)
        await cl(
            functions.bots.SetBotInfoRequest(
                lang_code=lang_code or "",
                bot=target,
                name=name,
                about=about,
                description=description,
            )
        )
        return f"Bot {bot} updated: {', '.join(changed)}."
    except RPCError as e:
        return f"Telegram refused to change bot {bot}: {e.message}."
    except Exception as e:
        return log_and_format_error("set_bot_info", e, bot=bot)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set My Username",
        openWorldHint=True,
        destructiveHint=True,
        idempotentHint=True,
        readOnlyHint=False,
    )
)
@with_account(readonly=False)
async def set_my_username(username: str, account: str = None) -> str:
    """
    Set, change or remove this account's own public @username.

    The old username is freed the moment the new one is set, and anyone may
    claim it. An empty string ("") removes the username; the account can then
    be found only by phone number or through a shared chat. Group and channel
    usernames are `set_channel_username`.

    Args:
        username: The new username, with or without the leading @. "" removes it.
    """
    if username is None:
        # Removal is asked for, never arrived at by omission.
        return 'set_my_username needs a username. To remove yours, pass an empty string ("").'
    name = username.strip().lstrip("@")
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        await cl(functions.account.UpdateUsernameRequest(username=name))
        return f"Username set to @{name}." if name else "Username removed."
    except RPCError as e:
        return f"Telegram refused the username: {e.message}."
    except Exception as e:
        return log_and_format_error("set_my_username", e, username=username)
