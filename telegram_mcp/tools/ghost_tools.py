"""Ghost mode and the safeguard, as tools the owner can reach by talking to the agent.

"Turn ghost mode off for the work account" becomes ``set_ghost_mode``; turning it off
is a gated call, so the owner still approves it outside the chat with the model.
The settings themselves, and every rule, live in ``telegram_mcp.safeguard``.
"""

import json
from typing import Optional, Union

from telegram_mcp import connection
from telegram_mcp.runtime import *
from telegram_mcp.safeguard import channels as approvals
from telegram_mcp.safeguard import ghost, grants, policy, taint


def _known_account(account: Optional[str]) -> Optional[str]:
    if account is None:
        return None
    connection.refresh_accounts()
    label = account.lower()
    if label not in connection.clients:
        raise ValueError(
            f"Unknown account '{account}'. Available accounts: {', '.join(connection.clients)}"
        )
    return label


async def _chat_keys(label: str, chat_id: Union[int, str]) -> list:
    """The chat as given, plus its numeric id when it resolves - the gated tools may be
    called with either spelling."""
    keys = [chat_id]
    try:
        entity = await resolve_entity(chat_id, get_client(label))
        marked = get_marked_id(entity)
        if str(marked) != str(chat_id):
            keys.append(marked)
    except Exception:
        pass
    return keys


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Ghost Mode",
        openWorldHint=False,
        destructiveHint=False,
        idempotentHint=True,
        readOnlyHint=False,
    )
)
async def set_ghost_mode(
    enabled: bool, account: str = None, chat_id: Union[int, str] = None
) -> str:
    """
    Turn ghost mode on or off for every account, one account, or one chat of an account.

    While ghost mode is on, nothing this server does tells anyone the owner saw or is
    typing: no read markers, story views, listened marks, typing, or view counts, and the
    account is reported offline after the server's own activity. Turning it ON runs at
    once; turning it OFF asks the owner for approval first.

    Args:
        enabled: True for ghost mode on, False for off.
        account: One account's label; omit for every account.
        chat_id: One chat of that account (needs `account`); omit for the whole account.
    """
    try:
        label = _known_account(account)
        if chat_id is not None and label is None:
            return "Error: a chat setting needs the account it belongs to (pass `account`)."
        if chat_id is None:
            view = ghost.set_mode(enabled, account=label)
        else:
            for key in await _chat_keys(label, chat_id):
                view = ghost.set_mode(enabled, account=label, chat=key)
        scope = "chat" if chat_id is not None else "account" if label else "all accounts"
        note = (
            "Reads send no seen signal here, and the account shows offline after this "
            "server acts."
            if view["effective"]
            else "Seen signals and presence behave normally here."
        )
        return json.dumps(
            {
                "ghost_mode": bool(enabled),
                "scope": scope,
                "account": label,
                "chat_id": chat_id,
                "effective": view["effective"],
                "note": note,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error("set_ghost_mode", e, account=account, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Ghost Mode",
        openWorldHint=False,
        destructiveHint=False,
        idempotentHint=True,
        readOnlyHint=True,
    )
)
async def get_ghost_mode(account: str = None, chat_id: Union[int, str] = None) -> str:
    """
    Show ghost mode: the default, the account's setting, the chat's setting, which one is
    in effect, and the seen signals it suppresses.

    Args:
        account: An account label; omit to see only the default.
        chat_id: A chat of that account.
    """
    try:
        label = _known_account(account)
        return json.dumps(ghost.describe(label, chat_id), ensure_ascii=False)
    except Exception as e:
        return log_and_format_error("get_ghost_mode", e, account=account, chat_id=chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Safeguard Status",
        openWorldHint=False,
        destructiveHint=False,
        idempotentHint=True,
        readOnlyHint=True,
    )
)
async def safeguard_status(account: str = None) -> str:
    """
    Show how the safeguard is set up: which approval channels exist, the approval time
    limit, every "always approve" grant (tool, chat, account), every folder granted
    "always allow", and the bulk-send and untrusted-content thresholds. Never shows a token, a code, or message text.

    Args:
        account: An account label for its Saved Messages channel and untrusted-content
            count; omit for all accounts.
    """
    try:
        label = _known_account(account)
        token, owners = approvals.bot_settings()
        guard = next((m for m in mcp.middleware if type(m).__name__ == "Safeguard"), None)
        labels = [label] if label else list(connection.clients)
        window = policy.SendWindow()
        return json.dumps(
            {
                "installed": guard is not None,
                "channels": {
                    "dialog": "used when the client supports approval dialogs",
                    "approval_bot": {
                        "configured": bool(token),
                        "allowed_users": (
                            len(owners) if owners else "every account this server runs"
                        ),
                    },
                    "saved_messages": {name: "available" for name in labels},
                },
                "approval_timeout_seconds": approvals.timeout_seconds(),
                "always_approved": grants.list_all(),
                "always_allowed_folders": grants.list_folders(),
                "bulk_send": {"chats": window.limit, "within_seconds": window.window},
                "untrusted_content": {
                    "passage_characters": taint.PASSAGE_LENGTH,
                    "remembered": {name: taint.fragment_count(name) for name in labels},
                },
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error("safeguard_status", e, account=account)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Revoke Always Approval",
        openWorldHint=False,
        destructiveHint=False,
        idempotentHint=True,
        readOnlyHint=False,
    )
)
async def revoke_always_approval(
    tool: str = None, chat_id: Union[int, str] = None, account: str = None, folder: str = None
) -> str:
    """
    Stop an "always approve" (one tool in one chat) or an "always allow" (one folder),
    so the safeguard asks again.

    Runs without asking: it only makes the safeguard stricter. `safeguard_status` lists
    every grant and every allowed folder.

    Args:
        tool: The tool name the grant covers, e.g. "delete_message". With chat_id.
        chat_id: The chat the grant covers, as listed by safeguard_status.
        account: The account the grant belongs to; omit when only one account runs.
        folder: A folder granted "always allow", exactly as safeguard_status lists it.
            Give this instead of tool and chat_id.
    """
    try:
        if folder:
            removed = grants.revoke_folder(folder)
            return json.dumps({"revoked": removed, "folder": folder}, ensure_ascii=False)
        if not tool or chat_id in (None, ""):
            return (
                "Give either folder (an always-allowed folder) or tool and chat_id "
                "(an always-approved tool in a chat); safeguard_status lists both."
            )
        label = _known_account(account)
        if label is None and len(connection.clients) == 1:
            label = next(iter(connection.clients))
        removed = grants.revoke(label, tool, chat_id)
        return json.dumps(
            {"revoked": removed, "account": label, "tool": tool, "chat_id": chat_id},
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error("revoke_always_approval", e, tool=tool, chat_id=chat_id)


__all__ = ["set_ghost_mode", "get_ghost_mode", "safeguard_status", "revoke_always_approval"]
