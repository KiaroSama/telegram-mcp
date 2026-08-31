"""Premium emoji could be SCHEDULED and not SENT.

`schedule_message` and `edit_scheduled_message` have accepted an `entities` list
since they were written — the only way to place a custom emoji, because
`parse_mode` has no syntax for one. `send_message`, `reply_to_message` and
`edit_message` did not. So the server could queue a message with premium emoji
for later and had no way to send the same message now, reply with one, or edit
one into a message.

The rebuilder those scheduled tools used was private to their module, which is
how the gap stayed open: the capability existed, in a place nothing else could
reach. It now lives in `telegram_mcp.entities`, the write-side inverse of
`message_view.describe_entities`.

`effect_id` is the same shape one door over: `get_message_effect` is a whole tool
for READING premium message effects, and nothing could send one.
"""

from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji

from telegram_mcp.entities import rebuild_entities
from telegram_mcp.tools import messages as messages_mod

TEXT = "hello world"
# A custom emoji spanning "hello", and bold over "world".
CUSTOM = {"type": "custom_emoji", "offset": 0, "length": 5, "custom_emoji_id": 55512345}
BOLD = {"type": "bold", "offset": 6, "length": 5}


class Recorder:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, entity, message, **kwargs):
        self.sent.append((message, kwargs))
        return SimpleNamespace(id=1)

    async def edit_message(self, entity, message_id, text, **kwargs):
        self.edited.append((text, kwargs))
        return SimpleNamespace(id=message_id)


@pytest.fixture
def wired(monkeypatch):
    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(messages_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(messages_mod, "resolve_entity", _resolve)
    return client


# --- the rebuilder itself ---------------------------------------------------


def test_a_custom_emoji_is_rebuilt_as_the_telethon_entity():
    built = rebuild_entities([CUSTOM], TEXT)

    (entity,) = built
    assert isinstance(entity, MessageEntityCustomEmoji)
    assert entity.document_id == 55512345
    assert (entity.offset, entity.length) == (0, 5)


def test_a_mixed_list_survives_whole():
    """A three-kind table used to refuse the WHOLE message once a fourth kind
    appeared, so a premium emoji beside one bold word could not be sent at all."""
    built = rebuild_entities([CUSTOM, BOLD], TEXT)

    assert [type(e) for e in built] == [MessageEntityCustomEmoji, MessageEntityBold]


def test_an_offset_past_the_end_refuses_the_whole_call():
    """Refusing beats placing it approximately: a message sent with formatting
    silently moved looks like it worked."""
    result = rebuild_entities([{"type": "bold", "offset": 50, "length": 5}], TEXT)

    assert isinstance(result, str)
    assert "UTF-16" in result or "spans" in result


def test_a_raw_offset_is_refused():
    """`offset_is_raw` marks an offset the viewer could not rebase onto the text
    it returned; it indexes Telegram's original string."""
    result = rebuild_entities([dict(CUSTOM, offset_is_raw=True)], TEXT)

    assert isinstance(result, str)
    assert "raw" in result


# --- the three tools that could not use it ---------------------------------


@pytest.mark.asyncio
async def test_send_message_places_a_premium_emoji(wired):
    await messages_mod.send_message(-100, TEXT, entities=[CUSTOM])

    message, kwargs = wired.sent[-1]
    assert message == TEXT
    (entity,) = kwargs["formatting_entities"]
    assert isinstance(entity, MessageEntityCustomEmoji)


@pytest.mark.asyncio
async def test_reply_to_message_places_a_premium_emoji(wired):
    await messages_mod.reply_to_message(-100, 42, TEXT, entities=[CUSTOM])

    _message, kwargs = wired.sent[-1]
    assert isinstance(kwargs["formatting_entities"][0], MessageEntityCustomEmoji)


@pytest.mark.asyncio
async def test_edit_message_places_a_premium_emoji(wired):
    await messages_mod.edit_message(-100, 42, TEXT, entities=[CUSTOM])

    text, kwargs = wired.edited[-1]
    assert text == TEXT
    assert isinstance(kwargs["formatting_entities"][0], MessageEntityCustomEmoji)
    assert kwargs["parse_mode"] is None, (
        "leaving the default parser on would have it re-read the text and fight "
        "the explicit entities"
    )


@pytest.mark.asyncio
async def test_entities_and_parse_mode_together_are_refused(wired):
    """Two ways to describe the same formatting; Telegram applies one. Sending
    both would silently drop whichever it ignored."""
    result = await messages_mod.send_message(-100, TEXT, parse_mode="md", entities=[CUSTOM])

    assert wired.sent == []
    assert "not both" in result


@pytest.mark.asyncio
async def test_a_bad_entity_stops_the_send_entirely(wired):
    result = await messages_mod.send_message(
        -100, TEXT, entities=[{"type": "bold", "offset": 99, "length": 4}]
    )

    assert wired.sent == [], "a message went out with its formatting dropped"
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_plain_text_still_sends_without_entities(wired):
    """Guard the guard: the common case must not have been broken by widening."""
    await messages_mod.send_message(-100, "just text")

    message, kwargs = wired.sent[-1]
    assert message == "just text"
    assert kwargs["formatting_entities"] is None


# --- effects: read for months, never sendable -------------------------------


@pytest.mark.asyncio
async def test_a_premium_effect_reaches_the_send(wired):
    await messages_mod.send_message(-100, "boom", effect_id=5104841245755180586)

    _message, kwargs = wired.sent[-1]
    assert kwargs["message_effect_id"] == 5104841245755180586


@pytest.mark.asyncio
async def test_a_reply_can_carry_an_effect_too(wired):
    await messages_mod.reply_to_message(-100, 42, "boom", effect_id=777)

    _message, kwargs = wired.sent[-1]
    assert kwargs["message_effect_id"] == 777


def test_the_scheduled_tools_now_share_one_rebuilder():
    """The gap existed because this logic was private to `scheduled.py`. Two
    copies of "what an offset means" would drift the first time either changed.

    The shared name moved from `rebuild_entities` to `build_send_entities` when
    `mention_name` turned out to need the network before it can be built; the
    property under test is unchanged - every sending path goes through ONE
    builder."""
    from telegram_mcp.entities import build_send_entities
    from telegram_mcp.tools import messages as messages_module
    from telegram_mcp.tools import scheduled as scheduled_mod

    assert scheduled_mod.build_send_entities is build_send_entities
    assert messages_module.build_send_entities is build_send_entities


# --------------------------------------------------------------------------
# mention_name: the one kind whose read form is not its write form
# --------------------------------------------------------------------------
#
# Measured live 2026-08-31: sending `messageEntityMentionName` - the form
# Telegram RETURNS, carrying a bare user id - was accepted and the entity was
# then silently dropped. The message arrived with no entities at all and nothing
# reported a problem. Sending needs `inputMessageEntityMentionName`, whose
# user_id is an InputUser: id AND access hash.


def test_tagging_someone_builds_the_INPUT_form_telegram_accepts():
    from telethon.tl.types import InputMessageEntityMentionName, InputUser

    from telegram_mcp.entities import rebuild_entities

    who = InputUser(user_id=5899781975, access_hash=123456789)
    built = rebuild_entities(
        [{"type": "mention_name", "offset": 0, "length": 6, "user_id": 5899781975}],
        "tagged person",
        input_users={5899781975: who},
    )

    assert isinstance(built, list) and len(built) == 1
    entity = built[0]
    assert isinstance(
        entity, InputMessageEntityMentionName
    ), "the READ form was built; Telegram accepts it and drops it without a word"
    assert entity.user_id is who, "the access hash was lost, which is the half that matters"


def test_a_tag_that_cannot_be_resolved_is_refused_rather_than_dropped():
    """An access hash exists only for someone this account has encountered. A
    silent send would look like it worked and tag nobody."""
    from telegram_mcp.entities import rebuild_entities

    refusal = rebuild_entities(
        [{"type": "mention_name", "offset": 0, "length": 6, "user_id": 999}],
        "tagged person",
        input_users={},
    )

    assert isinstance(refusal, str)
    assert "999" in refusal
    assert "access hash" in refusal


def test_without_a_resolver_a_tag_is_refused_not_guessed():
    from telegram_mcp.entities import rebuild_entities

    refusal = rebuild_entities(
        [{"type": "mention_name", "offset": 0, "length": 6, "user_id": 5899781975}],
        "tagged person",
    )

    assert isinstance(refusal, str) and "resolv" in refusal


def test_every_other_entity_kind_needs_no_resolver():
    """The network is touched only for `mention_name`. Bold, a link and a premium
    emoji are built from the request alone, and must not start paying for a
    connection because one rare kind needs it."""
    from telegram_mcp.entities import rebuild_entities

    built = rebuild_entities(
        [
            {"type": "bold", "offset": 0, "length": 6},
            {"type": "text_url", "offset": 7, "length": 6, "url": "https://example.com"},
        ],
        "tagged person",
    )

    assert isinstance(built, list) and len(built) == 2
