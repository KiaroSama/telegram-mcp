"""Turning a described entity list back into the entities Telegram accepts.

The inverse of `message_view.describe_entities`, and it lives beside nothing
else on purpose: the two have to agree about what an offset MEANS, and they were
drifting apart the moment a second tool needed to send formatted text.

`schedule_message` could place a premium emoji exactly; `send_message` could not,
because this code was private to the scheduled tools. So the server could queue a
message with custom emoji for later and had no way to send the same message now.
"""

from typing import List, Optional

from telethon.tl import types

from telegram_mcp.message_view import entity_kind_from_name

# The dict keys `describe_entities` publishes, mapped to the constructor
# arguments Telethon's entity classes take.
ENTITY_FIELDS = {
    "custom_emoji_id": "document_id",
    "url": "url",
    "user_id": "user_id",
    "language": "language",
    "collapsed": "collapsed",
}


def entity_classes() -> dict:
    """Every entity kind Telethon knows, keyed as `describe_entities` names them.

    Derived from Telethon's own type list rather than hand-written. A table of
    three kinds refused the WHOLE message the moment a fourth appeared, so text
    carrying a premium emoji beside one bold word could not be sent at all - and
    a table also stops covering whatever Telegram adds next, silently.
    """
    classes = {}
    for name in dir(types):
        if not name.startswith("MessageEntity"):
            continue
        candidate = getattr(types, name)
        if isinstance(candidate, type):
            classes[entity_kind_from_name(name)] = candidate
    return classes


async def resolve_mention_users(items: Optional[List[dict]], client) -> dict:
    """`{user_id: InputUser}` for every `mention_name` in `items`.

    Tagging somebody needs their access hash, not just their id, and an access
    hash is only obtainable for a user this account has actually encountered -
    a shared chat, a contact, a resolved username. So this asks the client and
    lets `rebuild_entities` refuse by name whatever comes back missing, rather
    than sending an entity Telegram will drop without comment.

    Never raises: one unresolvable id must not stop the other entities from
    being reported precisely.
    """
    wanted = {
        int(item["user_id"])
        for item in (items or [])
        if item.get("type") == "mention_name" and item.get("user_id") is not None
    }
    resolved = {}
    for user_id in wanted:
        try:
            resolved[user_id] = await client.get_input_entity(user_id)
        except Exception:
            continue
    return resolved


def rebuild_entities(items: Optional[List[dict]], text: str, input_users: Optional[dict] = None):
    """Telethon entities from `inspect_message`-shaped dicts, or an error string.

    **Offsets are UTF-16 code units into `text`**, and that is not a detail a
    caller can get away with skimming. `describe_entities` rebases Telegram's raw
    offsets onto the `text_fidelity` string it returns, so `text` here has to be
    that same value. Hand it the generically sanitized `text` field instead and
    every offset is quietly off by however much the sanitizer removed - a premium
    emoji lands on the wrong character and nothing reports a problem.

    Refuses rather than guesses. An entity that cannot be rebuilt faithfully -
    out of range, marked `offset_is_raw`, or carrying a value the viewer already
    altered - fails the whole call, because a message sent with silently dropped
    formatting looks like it worked.

    `input_users` maps user id -> Telethon `InputUser` and is needed only for
    `mention_name`, the one kind whose read form Telegram will not accept back.
    Callers that can reach the network resolve them first; without it, a
    `mention_name` is refused rather than sent in a form that vanishes.
    """
    if not items:
        return None

    classes = entity_classes()
    units = len(text.encode("utf-16-le")) // 2
    built, problems = [], []

    for item in items:
        kind = item.get("type")
        entity_class = classes.get(kind)
        if entity_class is None:
            problems.append(f"{kind!r} is not an entity kind this Telethon knows")
            continue

        offset, length = item.get("offset"), item.get("length")
        if not isinstance(offset, int) or not isinstance(length, int):
            problems.append(f"{kind} has no usable offset/length")
            continue
        # `offset_is_raw` marks an offset the viewer could NOT rebase onto the text
        # it returned. It indexes Telegram's original string, so using it here
        # would place the entity somewhere else entirely.
        if item.get("offset_is_raw"):
            problems.append(f"{kind} at {offset} is a raw Telegram offset, not one into this text")
            continue
        if offset < 0 or length < 0 or offset + length > units:
            problems.append(
                f"{kind} spans {offset}..{offset + length} but the text is {units} UTF-16 units"
            )
            continue
        # The viewer cleans a sender-supplied url or language tag and says so. The
        # cleaned form is safe to SHOW and wrong to SEND: it is not what the
        # original message carried.
        altered = [k for k in ("url_altered", "language_altered") if item.get(k)]
        if altered:
            problems.append(f"{kind} carries {altered[0]}, so its value is not the original")
            continue

        fields = {"offset": offset, "length": length}
        for source, target in ENTITY_FIELDS.items():
            if source in item:
                fields[target] = item[source]

        # `mention_name` is the one kind whose READ form is not its WRITE form.
        # Telegram returns `messageEntityMentionName`, carrying a bare user id;
        # sending needs `inputMessageEntityMentionName`, whose `user_id` is an
        # InputUser - id AND access hash. Handing back the read form is accepted
        # and then SILENTLY DROPPED: measured live, the message arrived with no
        # entities at all and nothing reported a problem.
        if kind == "mention_name":
            user_id = fields.pop("user_id", None)
            if user_id is None:
                problems.append("mention_name has no user_id, so nobody would be tagged")
                continue
            if input_users is None:
                problems.append(
                    "mention_name cannot be sent without resolving the user first; "
                    "this call did not supply a resolver"
                )
                continue
            resolved = input_users.get(int(user_id))
            if resolved is None:
                problems.append(
                    f"mention_name names user {user_id}, whom this account cannot resolve - "
                    "an access hash is required to tag someone, and it comes from having "
                    "seen them (a shared chat, a contact, a resolved username)"
                )
                continue
            try:
                built.append(
                    types.InputMessageEntityMentionName(
                        offset=offset, length=length, user_id=resolved
                    )
                )
            except TypeError as error:
                problems.append(f"mention_name could not be built: {error}")
            continue

        try:
            built.append(entity_class(**fields))
        except TypeError as error:
            problems.append(f"{kind} could not be built: {error}")

    if problems:
        return "Refused: " + "; ".join(problems) + "."
    return built


async def build_send_entities(items: Optional[List[dict]], text: str, account=None):
    """`rebuild_entities`, having first resolved anyone a `mention_name` tags.

    One function so all four sending paths - send, edit, schedule, quick reply -
    behave the same. The network is touched ONLY when a `mention_name` is
    actually present: every other entity kind is built from the request alone,
    and paying for a connection to discover that would slow the common case for
    the rare one.

    The import is deferred because `connection` builds real clients at import
    time and this module must stay importable without them.
    """
    input_users = None
    if any((item or {}).get("type") == "mention_name" for item in (items or [])):
        from telegram_mcp.connection import ensure_connected, get_client

        client = get_client(account)
        await ensure_connected(client)
        input_users = await resolve_mention_users(items, client)
    return rebuild_entities(items, text, input_users)
