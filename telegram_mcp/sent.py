"""What was just sent, addressed by id.

Six senders confirmed a send and returned nothing an agent could act on:
`send_file`, `send_album`, `send_voice`, `send_sticker`, `send_gif` and the rich
path of `send_message`/`reply_to_message`. Every follow-up a caller might want --
edit, react, pin, forward, delete, reply -- needs the id, and the only way back
to it was to re-read the chat and guess which message was the one.

Telethon's friendly methods answer with a `Message` (or a list of them, for an
album). A raw `SendMessageRequest` answers with `Updates`, where the id arrives
in an `UpdateMessageID` -- which is why the rich path could not simply read
`.id` off its result and had been left reporting only `{"sent": true}`.
"""

from typing import Any, List

__all__ = ["sent_message_ids"]


def sent_message_ids(result: Any) -> List[int]:
    """Every message id in what a send just returned, in order.

    Accepts a `Message`, a list of them, or the `Updates` a raw request answers
    with. Returns `[]` rather than raising: a send that succeeded must not be
    reported as a failure because its receipt had an unfamiliar shape.
    """
    if result is None:
        return []

    if isinstance(result, (list, tuple)):
        ids = []
        for item in result:
            ids.extend(sent_message_ids(item))
        return ids

    updates = getattr(result, "updates", None)
    if updates is not None:
        ids = []
        for update in updates:
            # UpdateMessageID carries the id directly; the New*Message updates
            # carry the whole message. An album sends one of each per member.
            ident = getattr(update, "id", None)
            if ident is None:
                ident = getattr(getattr(update, "message", None), "id", None)
            if isinstance(ident, int) and ident not in ids:
                ids.append(ident)
        return ids

    ident = getattr(result, "id", None)
    return [ident] if isinstance(ident, int) else []
