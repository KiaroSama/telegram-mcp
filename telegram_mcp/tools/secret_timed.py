"""Sending one message under a temporary self-destruct timer.

An ordinary chat puts the timer on the MESSAGE, so sending one disappearing
photo changes nothing about the chat. A secret chat cannot: Telegram answers a
per-message timer with "Messages can self-destruct only in private chats", and
the only mechanism it has is the timer belonging to the whole conversation. So
"send one disappearing message" here is three acts wearing one name -- arm the
chat, send, put the timer back.

**This was recommended against and built anyway, at the owner's explicit
direction about their own account.** The reasoning and the rejected alternatives
are in `docs/adr/0002-a-timed-send-mutates-the-chat-to-send-one-message.md`.
What that decision requires of the code is here:

- the restore runs in a ``finally``, so a send that raises -- or a cancellation
  -- still disarms the chat rather than leaving it armed indefinitely;
- a restore that FAILS is reported as loudly as this server can report anything,
  as ``unconfirmed`` with the timer the chat has been left on, because a silent
  restore failure leaves a conversation armed with nobody aware and that is
  strictly worse than the send having failed outright;
- the docstrings say plainly that the other person's messages inside the window
  are caught by the timer too. That follows from the timer being the chat's, and
  no care taken here prevents it -- so it is stated rather than engineered
  around.

The sequence is not atomic and cannot be made atomic. Nothing below pretends
otherwise.
"""

from typing import Optional

from telegram_mcp import secret_history
from telegram_mcp.file_roots import _resolve_readable_file_path
from telegram_mcp.runtime import *
from telegram_mcp.secret_backend import secret_manager
from telegram_mcp.secret_common import account_label, describe_refusal, to_secret_id
from telegram_mcp.secret_compose import formatted_text, timer_lock
from telegram_mcp.secret_limits import require_ready_chat
from telegram_mcp.secret_media_content import infer_kind, validate_kind


def _account_label(account=None) -> str:
    return account_label(account)


__all__ = ["send_timed_secret_media", "send_timed_secret_message"]


def _check_seconds(seconds: int) -> Optional[str]:
    """Telegram's own accepted values, refused here rather than at the protocol."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return f"seconds must be a whole number, not {seconds!r}. Nothing was sent."
    if value < 1:
        return (
            f"seconds must be at least 1, got {value}. A timed send with no timer is just "
            "send_secret_message; to turn a chat's timer OFF use "
            "set_secret_chat_timer(seconds=0). Nothing was sent."
        )
    if value > 604800:
        return (
            f"seconds must be at most 604800 (one week), got {value} — that is Telegram's "
            "own ceiling for a secret chat's timer. Nothing was sent."
        )
    return None


async def _previous_timer(manager, chat_id: int) -> int:
    """The timer already on the chat, so the restore puts BACK rather than off.

    A chat the owner had deliberately armed must not come back disarmed because
    something passed through it.
    """
    return int(getattr(manager.status(int(chat_id)), "ttl", 0) or 0)


async def _send_under_timer(manager, chat_id: int, send, seconds: int):
    """Arm, send, restore. Returns ``(sent_id, send_error, restore_error, previous)``.

    ``send`` is a no-argument coroutine function, so the two tools share this whole
    sequence and differ only in what they hand it. Its exception is CAUGHT rather
    than allowed to propagate, because a caller needs to hear about an armed chat
    even when the reason they are hearing from us at all is that the send failed.
    The ``finally`` covers what the except cannot: a cancellation between arming
    and sending.
    """
    # The previous timer is read INSIDE the lock: read outside, it could be another
    # timed send's temporary value, and restoring that leaves the chat armed.
    async with timer_lock(manager, chat_id):
        previous = await _previous_timer(manager, chat_id)
        await manager.set_ttl(int(chat_id), int(seconds))

        sent_id = None
        send_error = None
        restore_error = None
        try:
            try:
                sent_id = await send()
            except Exception as exc:
                send_error = exc
        finally:
            try:
                await manager.set_ttl(int(chat_id), previous)
            except Exception as exc:
                restore_error = exc

    return sent_id, send_error, restore_error, previous


def _result(chat_id, seconds, previous, sent_id, send_error, restore_error, extra):
    """One answer covering all four combinations of send and restore."""
    if restore_error is not None:
        # The loudest thing this server can say. The chat is not how the caller
        # left it, and only this reply can tell them.
        record = {
            "outcome": "unconfirmed",
            "sent": sent_id is not None,
            "chat_id": int(chat_id),
            "timer_left_on": int(seconds),
            "timer_should_have_been": int(previous),
            "restore_failed": str(restore_error),
            "what_to_do": (
                f"THE CHAT IS STILL ARMED. Its self-destruct timer was set to {seconds} "
                f"seconds for this message and could not be put back to {previous}. "
                f"Everything either of you sends in that chat now disappears after "
                f"{seconds} seconds until it is changed. Fix it with "
                f"set_secret_chat_timer(chat_id={int(chat_id)}, seconds={previous})."
            ),
        }
        if send_error is not None:
            record["send_failed"] = str(send_error)
        record.update(extra)
        return format_tool_result(record)

    if send_error is not None:
        refusal = describe_refusal(send_error)
        if refusal:
            return (
                f"{refusal} The chat's timer was put back to {previous}, so nothing about "
                "the conversation was left changed."
            )
        raise send_error

    record = {
        "sent": True,
        "chat_id": int(chat_id),
        "message_id": sent_id,
        "timer_seconds": int(seconds),
        "timer_restored": True,
        "restored_to": int(previous),
        "note": (
            "The chat's timer was armed for this message and put back afterwards. Anything "
            "the other person sent during that window is under the same timer — it belongs "
            "to the chat, not to the message."
        ),
    }
    record.update(extra)
    return format_tool_result(record)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Timed Secret Message",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
async def send_timed_secret_message(
    chat_id: int, message: str, seconds: int, parse_mode: str = None, account: str = None
) -> str:
    """
    Send one text message that self-destructs, then put the chat's timer back.

    A secret chat has no per-message timer — Telegram refuses one outright — so
    this arms the CHAT's timer, sends, and restores whatever was there before.

    **The window is shared.** While the timer is armed, anything the OTHER
    person sends is under it too, and will disappear on the same schedule. That
    follows from the timer belonging to the conversation rather than to the
    message, and nothing here can prevent it.

    If the timer cannot be put back, the result says `outcome: unconfirmed` and
    names the timer the chat has been left on. Read that field: the message went,
    but the conversation is not as you left it.

    For a timer you want to KEEP, use `set_secret_chat_timer` — it is the honest
    tool for that, and it does not depend on a restore succeeding.

    Args:
        chat_id: From `list_secret_chats`.
        message: The text to send.
        seconds: 1 to 604800 (one week).
        parse_mode: `markdown` or `html`, or unset for plain text.
    """
    bad = _check_seconds(seconds)
    if bad:
        return bad

    try:
        label = _account_label(account)
        manager = await secret_manager(label)
        secret_id = to_secret_id(chat_id)

        refusal = require_ready_chat(manager, secret_id)
        if refusal:
            return refusal

        text, entities = formatted_text(message, parse_mode)

        sent_id, send_error, restore_error, previous = await _send_under_timer(
            manager, secret_id, lambda: manager.send_message(secret_id, text, entities), seconds
        )
        local_copy = None
        if sent_id is not None:
            local_copy = secret_history.record_sent(
                label,
                secret_id,
                secret_history.entry(
                    message_id=sent_id, is_outgoing=True, text=text, ttl=int(seconds)
                ),
            )
        extra = {"local_copy": local_copy} if local_copy else {}
        return _result(chat_id, seconds, previous, sent_id, send_error, restore_error, extra)
    except ValueError as e:
        return str(e)
    except KeyError:
        return f"No secret chat {chat_id} for this login. `list_secret_chats` shows them."
    except Exception as e:
        return describe_refusal(e) or log_and_format_error(
            "send_timed_secret_message", e, chat_id=chat_id
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send Timed Secret Media",
        openWorldHint=True,
        destructiveHint=True,
        readOnlyHint=False,
        idempotentHint=False,
    )
)
@with_account(readonly=False)
async def send_timed_secret_media(
    chat_id: int,
    file_path: str,
    seconds: int,
    kind: str = None,
    caption: str = "",
    account: str = None,
    ctx: Context = None,
) -> str:
    """
    Send one file that self-destructs, then put the chat's timer back.

    The same three acts as `send_timed_secret_message`, for any of the eight
    kinds a secret chat carries. The upload happens while the timer is armed, so
    a large file widens the window.

    **The window is shared.** Anything the OTHER person sends while the timer is
    armed is under it too — it belongs to the chat, not to the message.

    A failed restore is reported as `outcome: unconfirmed` with the timer the
    chat is left on.

    Args:
        chat_id: From `list_secret_chats`.
        file_path: Path to the file, under the same allowed roots as
            `upload_file`.
        seconds: 1 to 604800 (one week).
        kind: One of photo, video, document, audio, animation, sticker,
            video_note, voice_note. Unset chooses from the file.
        caption: Optional; refused for a sticker or video note, which carry none.
    """
    bad = _check_seconds(seconds)
    if bad:
        return bad

    try:
        label = _account_label(account)
        manager = await secret_manager(label)
        secret_id = to_secret_id(chat_id)

        refusal = require_ready_chat(manager, secret_id)
        if refusal:
            return refusal

        path, path_error = await _resolve_readable_file_path(
            raw_path=file_path, ctx=ctx, tool_name="send_timed_secret_media"
        )
        if path_error:
            return path_error

        # Checked BEFORE the timer moves: a refused kind or a caption on a sticker
        # must not leave the chat armed for a message that never existed.
        chosen = validate_kind(str(path), kind or infer_kind(str(path)), caption)

        sent_id, send_error, restore_error, previous = await _send_under_timer(
            manager,
            secret_id,
            lambda: manager.send_file(secret_id, path, caption=caption, kind=chosen),
            seconds,
        )
        local_copy = None
        if sent_id is not None:
            local_copy = secret_history.record_sent(
                label,
                secret_id,
                secret_history.entry(
                    message_id=sent_id,
                    is_outgoing=True,
                    text=caption,
                    kind=chosen,
                    ttl=int(seconds),
                ),
            )
        return _result(
            chat_id,
            seconds,
            previous,
            sent_id,
            send_error,
            restore_error,
            {
                "kind": chosen,
                "kind_chosen_by": "caller" if kind else "the file",
                **({"local_copy": local_copy} if local_copy else {}),
            },
        )
    except ValueError as e:
        return str(e)
    except KeyError:
        return f"No secret chat {chat_id} for this login. `list_secret_chats` shows them."
    except Exception as e:
        return describe_refusal(e) or log_and_format_error(
            "send_timed_secret_media", e, chat_id=chat_id
        )
