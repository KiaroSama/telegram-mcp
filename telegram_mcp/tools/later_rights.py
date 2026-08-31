"""Admin rights the announced TL layer cannot carry.

`edit_admin_rights` in :mod:`telegram_mcp.tools.moderation` sets rights through
Telethon, and that is the right tool for almost all of them. It has one hard
ceiling, measured rather than assumed:

    A live channel, one `channels.editAdmin` from the channel's own creator,
    carrying flags.18, flags.19 and flags.20. Telegram ACCEPTED it and replied
    "admin rights updated". On read-back only flags.18 was there.

Telegram masks flags newer than the layer the client announced, and it does so
silently -- no error, a successful reply, the flag simply absent. Telethon
announces layer 227 (`telethon.tl.alltlobjects.LAYER`) and was archived in
February 2026, so that number will never rise. Serialising the bits correctly,
which `moderation.py` does and proves on the wire, is necessary and not
sufficient.

TDLib announces layer 229. This module is the way through, and it is deliberately
narrow: **one right at a time, read-modify-write entirely inside TDLib.**

The read-modify-write matters more than it looks. The obvious design -- take the
rights `get_admins` reports and translate them into TDLib's field names -- would
need a mapping, and that mapping does not exist cleanly: MTProto has
`manage_ranks` and `manage_linked_peers` with no TDLib counterpart, TDLib has
`can_manage_tags` with no MTProto one, and `other` versus `can_manage_chat` is a
guess. A wrong entry in a permissions map does not fail loudly; it grants or
revokes the wrong right. So no mapping is built: the current rights come from
TDLib, one field is changed, and they go back to TDLib. The valid field names
are whatever the object came back holding, which cannot drift out of date.

The cost is the one this whole route carries: TDLib needs its own login per
account (``python scripts/secret_chat_login.py <account>``), because it cannot
read a Telethon session.
"""

from typing import Union

from telegram_mcp.runtime import *
from telegram_mcp.tdlib import (
    NotSignedIn,
    TDLibError,
    TDLibUnavailable,
    account_label,
    secret_client,
)

__all__ = ["get_admin_rights_via_tdlib", "set_admin_right"]


# Named because it is the reason this module exists, and because a caller
# reaching for it will have the MTProto name in mind from `edit_admin_rights`.
# Only rights whose correspondence is unambiguous belong here; a guess would be
# the mapping this module was written to avoid.
_MTPROTO_ALIASES = {
    "manage_welcome_messages": "can_send_welcome_messages",
}


def _member(user_id: int) -> dict:
    return {"@type": "messageSenderUser", "user_id": int(user_id)}


async def _current_status(client, chat_id: int, user_id: int) -> dict:
    """The member's status object, or a refusal explaining which case it is.

    Raises `ValueError` with a sentence rather than returning a half-answer:
    setting a right on someone who is not an administrator is a promotion, which
    is a different decision and belongs to `promote_admin`.
    """
    member = await client.request(
        {"@type": "getChatMember", "chat_id": int(chat_id), "member_id": _member(user_id)}
    )
    status = member.get("status") or {}
    kind = status.get("@type")
    if kind == "chatMemberStatusCreator":
        raise ValueError(
            f"User {user_id} is the creator of this chat. A creator holds every right "
            "implicitly and Telegram does not store them as editable flags, so there is "
            "nothing here to switch on."
        )
    if kind != "chatMemberStatusAdministrator":
        raise ValueError(
            f"User {user_id} is not an administrator here (status: {kind}). Promote them "
            "first with promote_admin; this tool changes one right on an existing admin "
            "rather than granting admin status."
        )
    return status


def _resolve_right(name: str, rights: dict) -> str:
    """The TDLib field name for `name`, validated against this very object.

    Checked against the keys that just came back rather than a list written
    here, so the accepted set is exactly what the installed TDLib supports and
    cannot go stale.
    """
    wanted = _MTPROTO_ALIASES.get(name, name)
    if wanted in rights:
        return wanted
    available = sorted(k for k in rights if k != "@type")
    raise ValueError(
        f"Unknown right {name!r}. This TDLib build carries: {', '.join(available)}. "
        f"(MTProto names accepted as aliases: {', '.join(sorted(_MTPROTO_ALIASES))}.)"
    )


async def _apply_right(client, chat_id: int, user_id: int, right: str, enabled: bool) -> dict:
    """Switch one right and read the result back from Telegram.

    Separated from the tool around it because `edit_admin_rights` needs exactly
    this and nothing else: it has already resolved the account and does not want
    a formatted string back.
    """
    status = await _current_status(client, chat_id, user_id)
    rights = dict(status.get("rights") or {})
    field = _resolve_right(right, rights)
    before = bool(rights.get(field))

    rights[field] = bool(enabled)
    rights["@type"] = "chatAdministratorRights"
    await client.request(
        {
            "@type": "setChatMemberStatus",
            "chat_id": chat_id,
            "member_id": _member(user_id),
            "status": {
                "@type": "chatMemberStatusAdministrator",
                "can_be_edited": status.get("can_be_edited", True),
                "rights": rights,
            },
        }
    )

    confirmed = await _current_status(client, chat_id, user_id)
    after_rights = confirmed.get("rights") or {}
    after = bool(after_rights.get(field))

    record = {
        "chat_id": chat_id,
        "user_id": user_id,
        "right": field,
        "before": before,
        "after": after,
        "applied": after == bool(enabled),
    }
    if not record["applied"]:
        record["note"] = (
            "Telegram accepted the request and the right is still "
            f"{after}. That is Telegram declining it, not a transport failure - "
            "check that this account may grant it in this chat."
        )
    # A right the caller did not name must not have moved. Reported rather
    # than trusted, because this tool rewrites the whole rights object.
    moved = sorted(
        k
        for k, v in after_rights.items()
        if k not in ("@type", field) and bool(v) != bool((status.get("rights") or {}).get(k))
    )
    if moved:
        record["unexpectedly_changed"] = moved
    return record


async def finish_later_rights(account, chat_id: int, user_id: int, rights: list) -> dict:
    """Deliver, over TDLib, the rights the MTProto connection could not carry.

    `edit_admin_rights` sends every right in one `channels.editAdmin`, and
    Telegram silently drops the ones newer than the layer Telethon announces.
    Reporting that was the previous behaviour and it was honest, but it left the
    caller holding a right they had asked for and not received. TDLib speaks the
    current layer, so the remainder is finished there.

    Returns what happened to each name rather than raising: the MTProto half of
    the call already succeeded, so nothing here may turn a partial success into
    an exception that reads like total failure.
    """
    outcome = {"delivered": [], "failed": {}, "unmappable": []}
    routable = []
    for name in rights:
        (routable if name in _MTPROTO_ALIASES else outcome["unmappable"]).append(name)
    if not routable:
        return outcome

    try:
        client = await secret_client(account_label(account))
    except (NotSignedIn, TDLibUnavailable, ValueError) as exc:
        # Every name the caller asked about has to land in exactly one bucket.
        # Returning early with an untouched `routable` would drop those names
        # from the report entirely, which is the silence this whole path exists
        # to end.
        for name in routable:
            outcome["failed"][name] = str(exc)
        return outcome

    for name in routable:
        try:
            record = await _apply_right(client, chat_id, user_id, name, True)
        except (ValueError, TDLibError) as exc:
            outcome["failed"][name] = str(exc)
            continue
        if record["applied"]:
            outcome["delivered"].append(name)
        else:
            outcome["failed"][name] = record.get("note", "Telegram declined it.")
    return outcome


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Admin Rights (TDLib)", openWorldHint=True, readOnlyHint=True
    )
)
@with_account(readonly=True)
@validate_id("chat_id", "user_id")
async def get_admin_rights_via_tdlib(
    chat_id: Union[int, str], user_id: Union[int, str], account: str = None
) -> str:
    """
    Read one admin's rights through TDLib, which sees the ones Telethon cannot.

    `get_admins` reads through Telethon and is the right tool for everyday use.
    It cannot show a right newer than layer 227, because Telegram serialises its
    reply for the layer the client announced. This reads the same rights on
    layer 229, so a flag set here is visible here.

    Requires the account's separate TDLib login; the error says so by name.

    Args:
        chat_id: The chat, as a TDLib chat id (channels use the -100... form).
        user_id: The administrator to read.

    Note: field names are Telegram's own; nothing here is user-generated content.
    """
    try:
        label = account_label(account)
        client = await secret_client(label)
        status = await _current_status(client, int(chat_id), int(user_id))
        rights = {k: v for k, v in (status.get("rights") or {}).items() if k != "@type"}
        return format_tool_result(
            {
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "can_be_edited": status.get("can_be_edited"),
                "rights": rights,
                "granted": sorted(k for k, v in rights.items() if v),
            }
        )
    except (NotSignedIn, TDLibUnavailable, ValueError) as e:
        return str(e)
    except Exception as e:
        return log_and_format_error(
            "get_admin_rights_via_tdlib", e, chat_id=chat_id, user_id=user_id
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Admin Right", openWorldHint=True, destructiveHint=True, idempotentHint=True
    )
)
@with_account(readonly=False)
@validate_id("chat_id", "user_id")
async def set_admin_right(
    chat_id: Union[int, str],
    user_id: Union[int, str],
    right: str,
    enabled: bool = True,
    account: str = None,
) -> str:
    """
    Switch ONE admin right on or off, including the ones Telethon cannot deliver.

    Use `edit_admin_rights` for ordinary changes: it needs no extra login and
    sets everything at once. Use this when that tool reports a right it could not
    deliver -- `manage_welcome_messages` is the case this was built for.

    Every other right the admin holds is preserved exactly: the current rights
    are read from TDLib, one field is changed, and the whole object goes back.
    Nothing is translated between Telethon's names and TDLib's, because a wrong
    entry in a permissions mapping revokes a right silently.

    The result reports the rights BEFORE and AFTER, read back from Telegram, so
    a flag that did not take is visible rather than assumed. That is not
    ceremony: the bug this tool exists to route around was a request Telegram
    accepted while dropping the flag.

    Args:
        chat_id: The chat, as a TDLib chat id (channels use the -100... form).
        user_id: An existing administrator. Promoting someone is `promote_admin`.
        right: TDLib's field name, e.g. `can_send_welcome_messages`. The MTProto
            name `manage_welcome_messages` is accepted for it. An unknown name
            is refused with the list this TDLib build actually supports.
        enabled: True to grant, False to revoke.
    """
    try:
        client = await secret_client(account_label(account))
        record = await _apply_right(client, int(chat_id), int(user_id), right, bool(enabled))
        return format_tool_result(record)
    except (NotSignedIn, TDLibUnavailable, ValueError) as e:
        return str(e)
    except TDLibError as e:
        return f"Telegram refused this: {e}"
    except Exception as e:
        return log_and_format_error("set_admin_right", e, chat_id=chat_id, user_id=user_id)
