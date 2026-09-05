"""Swapping a premium emoji inside an existing message.

No network. Two things carry the weight:

* The 64-bit document id must cross the boundary as a STRING. Through JSON's
  number type 5934007978150595964 becomes 5934007978150595584 — a different
  emoji, silently. That rounding already made `get_custom_emoji` unusable once.
* Every OTHER entity has to survive. The whole reason this tool exists instead of
  "read the text and send it again" is that retyping loses the bold runs, the
  links and the emoji you did not mean to touch.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import custom_emoji_edit as mod

BANNER_ID = 5934007978150595964
NEW_ID = 5789012345678901234


class _Client:
    def __init__(self, message=None):
        self.message = message
        self.edits = []

    async def get_messages(self, entity, ids=None):
        return self.message

    async def edit_message(self, entity, message_id, text, formatting_entities=None):
        self.edits.append(
            SimpleNamespace(message_id=message_id, text=text, entities=formatting_entities)
        )
        return SimpleNamespace(id=message_id)


def _entity(kind, offset, length, **extra):
    described = {"type": kind, "offset": offset, "length": length}
    described.update(extra)
    return described


@pytest.fixture
def wire(monkeypatch):
    def _wire(text="Banner", entities=(), message=object()):
        client = _Client(message=message)
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(target, _client):
            return SimpleNamespace(id=-100123)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        monkeypatch.setattr(mod, "_read", lambda msg: (text, [dict(e) for e in entities]))

        async def _build(items, body, account=None):
            return list(items or [])

        monkeypatch.setattr(mod, "build_send_entities", _build)
        return client

    return _wire


def _payload(answer):
    return json.loads(answer)


@pytest.mark.asyncio
async def test_the_document_id_comes_back_as_a_string(wire):
    """5934007978150595964 through a float is 5934007978150595584 - a different
    emoji entirely, with nothing to say so."""
    wire(entities=[_entity("custom_emoji", 0, 2, custom_emoji_id=BANNER_ID, text="X")])

    record = _payload(await mod.inspect_custom_emoji(chat_id=-100123, message_id=7, account="a"))

    assert record["results"][0]["document_id"] == "5934007978150595964"


@pytest.mark.asyncio
async def test_every_other_entity_survives_the_swap(wire):
    """The reason this tool exists. Retyping the message would lose the bold run
    and the link; only the emoji's id may change."""
    entities = [
        _entity("bold", 0, 6),
        _entity("custom_emoji", 7, 2, custom_emoji_id=BANNER_ID, text="X"),
        _entity("text_url", 10, 4, url="https://example.test"),
    ]
    client = wire(text="Banner X here", entities=entities)

    await mod.replace_custom_emoji(
        chat_id=-100123, message_id=7, new_document_id=str(NEW_ID), account="a"
    )

    (edit,) = client.edits
    kinds = [e["type"] for e in edit.entities]
    assert kinds == ["bold", "custom_emoji", "text_url"], "an entity was dropped"
    assert edit.entities[0] == {"type": "bold", "offset": 0, "length": 6}
    assert edit.entities[2]["url"] == "https://example.test"
    assert edit.entities[1]["custom_emoji_id"] == NEW_ID
    assert edit.text == "Banner X here", "the text was rewritten"


@pytest.mark.asyncio
async def test_omitting_the_old_id_replaces_every_one(wire):
    """The banner case: one emoji repeated across the row."""
    entities = [
        _entity("custom_emoji", 0, 2, custom_emoji_id=BANNER_ID, text="X"),
        _entity("custom_emoji", 2, 2, custom_emoji_id=BANNER_ID, text="X"),
        _entity("custom_emoji", 4, 2, custom_emoji_id=BANNER_ID, text="X"),
    ]
    client = wire(text="XXX", entities=entities)

    answer = await mod.replace_custom_emoji(
        chat_id=-100123, message_id=7, new_document_id=str(NEW_ID), account="a"
    )

    assert _payload(answer)["results"][0]["changed"] == 3
    assert all(e["custom_emoji_id"] == NEW_ID for e in client.edits[0].entities)


@pytest.mark.asyncio
async def test_naming_an_old_id_leaves_the_others_alone(wire):
    """A message mixing several emoji is exactly where replacing all of them is
    the wrong answer."""
    other = 111222333444555666
    entities = [
        _entity("custom_emoji", 0, 2, custom_emoji_id=BANNER_ID, text="X"),
        _entity("custom_emoji", 2, 2, custom_emoji_id=other, text="Y"),
    ]
    client = wire(text="XY", entities=entities)

    answer = await mod.replace_custom_emoji(
        chat_id=-100123,
        message_id=7,
        new_document_id=str(NEW_ID),
        old_document_id=str(BANNER_ID),
        account="a",
    )

    assert _payload(answer)["results"][0]["changed"] == 1
    swapped, untouched = client.edits[0].entities
    assert swapped["custom_emoji_id"] == NEW_ID
    assert untouched["custom_emoji_id"] == other, "an emoji nobody targeted was changed"


@pytest.mark.asyncio
async def test_an_id_the_message_does_not_carry_changes_nothing(wire):
    """Silently replacing all of them instead would be the worst possible reading
    of a mistyped id."""
    client = wire(entities=[_entity("custom_emoji", 0, 2, custom_emoji_id=BANNER_ID, text="X")])

    answer = await mod.replace_custom_emoji(
        chat_id=-100123,
        message_id=7,
        new_document_id=str(NEW_ID),
        old_document_id="999888777666555444",
        account="a",
    )

    assert "Nothing was changed" in answer
    assert str(BANNER_ID) in answer, "it does not say what the message does carry"
    assert client.edits == []


@pytest.mark.asyncio
async def test_a_message_with_no_premium_emoji_is_not_edited(wire):
    client = wire(entities=[_entity("bold", 0, 6)])

    answer = await mod.replace_custom_emoji(
        chat_id=-100123, message_id=7, new_document_id=str(NEW_ID), account="a"
    )

    assert "no premium emoji" in answer
    assert client.edits == []


@pytest.mark.asyncio
async def test_replacing_an_id_with_itself_edits_nothing(wire):
    """Idempotent on purpose: an edit to the identical message burns the edit
    window and shows 'edited' to every reader for no reason."""
    client = wire(entities=[_entity("custom_emoji", 0, 2, custom_emoji_id=NEW_ID, text="X")])

    answer = await mod.replace_custom_emoji(
        chat_id=-100123, message_id=7, new_document_id=str(NEW_ID), account="a"
    )

    assert _payload(answer)["results"][0]["changed"] == 0
    assert client.edits == [], "the message was edited to itself"


@pytest.mark.asyncio
async def test_a_non_numeric_id_is_refused_before_anything_is_read(wire):
    client = wire(entities=[_entity("custom_emoji", 0, 2, custom_emoji_id=BANNER_ID, text="X")])

    answer = await mod.replace_custom_emoji(
        chat_id=-100123, message_id=7, new_document_id="not-an-id", account="a"
    )

    assert "not a number" in answer
    assert "inspect_custom_emoji" in answer, "no way to find a real id was named"
    assert client.edits == []
