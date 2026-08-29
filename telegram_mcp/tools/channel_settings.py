"""The reversible channel and supergroup settings a moderator reaches for.

`docs/api-coverage.md` measures 42 of 59 `channels.*` requests as unreached, the
largest gap in the project. This module takes the half of Phase 1 that is
reversible: join gates and visibility. Every one is a single request, admin-gated
by Telegram itself, and undoable by calling the same tool with the opposite
value.

Deliberately NOT here: the Structural group (`DeleteChannel`,
`ConvertToGigagroup`). Those are irreversible and need a confirmation protocol
this codebase does not have yet - designing it is its own decision, not a
detail of this module.

Separate from `channel_admin.py` (665 lines) because these are settings rather
than administration, and because appending six tools there would push one file
past the point where its responsibility is still one thing.
"""

from telegram_mcp.runtime import *


async def _require_channel(chat_id, cl):
    """The resolved entity, or a sentence explaining why it cannot be one.

    Every setting here is channel-only. A basic group reaching Telegram would
    come back as a raw RPC error naming a type the caller never mentioned, so it
    is refused here in words instead.
    """
    entity = await resolve_entity(chat_id, cl)
    if not isinstance(entity, Channel):
        return None, (
            f"Chat {chat_id} is a basic group, not a channel or supergroup, so this "
            "setting does not exist for it. Convert it to a supergroup first."
        )
    return entity, None


async def _toggle(tool_name, chat_id, account, build, describe):
    """Resolve, refuse a non-channel, send one request, say what is now true."""
    try:
        cl = get_client(account)
        await ensure_connected(cl)
        entity, refusal = await _require_channel(chat_id, cl)
        if refusal:
            return refusal
        await cl(build(entity))
        title = sanitize_name(getattr(entity, "title", str(chat_id)))
        return describe(title)
    except telethon.errors.rpcerrorlist.ChatAdminRequiredError:
        return "Cannot change this setting: admin privileges are required."
    except Exception as e:
        return log_and_format_error(tool_name, e, chat_id=chat_id)


# --- join gates -------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Join To Send", openWorldHint=True, destructiveHint=False, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_join_to_send(chat_id: Union[int, str], enabled: bool, account: str = None) -> str:
    """
    Require members to join a supergroup before they can post in it.

    Args:
        chat_id: The supergroup ID or username.
        enabled: True to require joining, False to let non-members post.

    Reversible: call again with the opposite value.
    """
    return await _toggle(
        "set_join_to_send",
        chat_id,
        account,
        lambda entity: functions.channels.ToggleJoinToSendRequest(channel=entity, enabled=enabled),
        lambda title: (
            f"Members must now join {title} before posting."
            if enabled
            else f"Non-members may now post in {title}."
        ),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Join Request", openWorldHint=True, destructiveHint=False, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_join_request(chat_id: Union[int, str], enabled: bool, account: str = None) -> str:
    """
    Require admin approval before someone joins.

    Args:
        chat_id: The channel or supergroup ID or username.
        enabled: True to hold joins for approval, False to admit directly.

    Applies to invite links as well as to the public join button. Reversible.
    """
    return await _toggle(
        "set_join_request",
        chat_id,
        account,
        lambda entity: functions.channels.ToggleJoinRequestRequest(
            channel=entity, enabled=enabled
        ),
        lambda title: (
            f"Joins to {title} now wait for admin approval."
            if enabled
            else f"Joins to {title} are admitted without approval."
        ),
    )


# --- visibility -------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Prehistory Hidden",
        openWorldHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_prehistory_hidden(
    chat_id: Union[int, str], hidden: bool, account: str = None
) -> str:
    """
    Hide messages sent before a member joined.

    Args:
        chat_id: The supergroup ID or username.
        hidden: True to hide earlier history from new members, False to show it.

    Reversible, but note what reversing means: making history visible again
    exposes every earlier message to everyone who has joined since it was hidden.
    """
    return await _toggle(
        "set_prehistory_hidden",
        chat_id,
        account,
        lambda entity: functions.channels.TogglePreHistoryHiddenRequest(
            channel=entity, enabled=hidden
        ),
        lambda title: (
            f"New members of {title} can no longer read earlier messages."
            if hidden
            else f"New members of {title} can now read the whole history."
        ),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Participants Hidden",
        openWorldHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_participants_hidden(
    chat_id: Union[int, str], hidden: bool, account: str = None
) -> str:
    """
    Hide the member list from non-admins.

    Args:
        chat_id: The supergroup ID or username.
        hidden: True to hide the member list, False to show it.

    Reversible. Telegram allows this only above a member threshold and answers
    with an error below it.
    """
    return await _toggle(
        "set_participants_hidden",
        chat_id,
        account,
        lambda entity: functions.channels.ToggleParticipantsHiddenRequest(
            channel=entity, enabled=hidden
        ),
        lambda title: (
            f"The member list of {title} is hidden from non-admins."
            if hidden
            else f"The member list of {title} is visible again."
        ),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Signatures", openWorldHint=True, destructiveHint=False, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_signatures(
    chat_id: Union[int, str],
    enabled: bool,
    show_profiles: bool = False,
    account: str = None,
) -> str:
    """
    Sign channel posts with the name of the admin who sent them.

    Args:
        chat_id: The broadcast channel ID or username.
        enabled: True to sign posts with the author's name.
        show_profiles: True to link each signature to the author's profile.
            Ignored when `enabled` is False.

    Two flags, not one: Telegram's request carries `signatures_enabled` and
    `profiles_enabled` separately, and linking a signature to a profile discloses
    more than the name alone. Reversible.
    """
    return await _toggle(
        "set_signatures",
        chat_id,
        account,
        lambda entity: functions.channels.ToggleSignaturesRequest(
            channel=entity,
            signatures_enabled=enabled,
            profiles_enabled=bool(enabled and show_profiles),
        ),
        lambda title: (
            f"Posts in {title} are signed"
            + (" and linked to author profiles." if enabled and show_profiles else ".")
            if enabled
            else f"Posts in {title} are no longer signed."
        ),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set View Forum As Messages",
        openWorldHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)
@with_account(readonly=False)
@validate_id("chat_id")
async def set_view_forum_as_messages(
    chat_id: Union[int, str], enabled: bool, account: str = None
) -> str:
    """
    Show a forum as one flat message list rather than as topics.

    Args:
        chat_id: The forum supergroup ID or username.
        enabled: True to present the forum as a plain chat.

    This is a per-account display preference, not a change to the chat itself:
    it affects how THIS account sees the forum. Reversible.
    """
    return await _toggle(
        "set_view_forum_as_messages",
        chat_id,
        account,
        lambda entity: functions.channels.ToggleViewForumAsMessagesRequest(
            channel=entity, enabled=enabled
        ),
        lambda title: (
            f"{title} is shown to this account as a flat message list."
            if enabled
            else f"{title} is shown to this account as topics."
        ),
    )
