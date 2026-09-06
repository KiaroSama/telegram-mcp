"""Editing quick-reply shortcuts: creating by side effect, renaming, deleting.

No network. The two behaviours worth pinning:

* Telegram has no "create shortcut" method — a shortcut exists once a message is
  stored under its name, so `add_quick_reply` both creates and appends. A typo
  therefore makes a SECOND shortcut instead of failing, and only the answer can
  say which happened.
* `delete_quick_reply` means two different things depending on one argument.
  Deleting a message is undone by retyping it; deleting the shortcut is not.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import quick_replies as mod


def _shortcut(shortcut_id=1, shortcut="thanks", count=2):
    return SimpleNamespace(shortcut_id=shortcut_id, shortcut=shortcut, count=count)


class _Client:
    def __init__(self, shortcuts=(), messages=()):
        self.requests = []
        self.shortcuts = list(shortcuts)
        self.messages = list(messages)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetQuickRepliesRequest":
            return SimpleNamespace(quick_replies=list(self.shortcuts))
        if name == "GetQuickReplyMessagesRequest":
            return SimpleNamespace(messages=list(self.messages))
        if name == "SendMessageRequest":
            # Storing under a new name is what creates the shortcut.
            wanted = request.quick_reply_shortcut.shortcut
            if all(s.shortcut != wanted for s in self.shortcuts):
                self.shortcuts.append(_shortcut(shortcut_id=99, shortcut=wanted, count=1))
            return SimpleNamespace(updates=[])
        if name in (
            "EditQuickReplyShortcutRequest",
            "DeleteQuickReplyShortcutRequest",
            "DeleteQuickReplyMessagesRequest",
            "EditMessageRequest",
        ):
            return SimpleNamespace(updates=[])
        raise AssertionError(f"unexpected request {name}")

    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def wire(monkeypatch):
    def _wire(client=None):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _build(items, text, account=None):
            return list(items or [])

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "build_send_entities", _build)

        async def _me(_target):
            return SimpleNamespace(id=7)

        client.get_input_entity = _me
        return client

    return _wire


def _record(answer):
    return json.loads(answer)["results"][0]


@pytest.mark.asyncio
async def test_a_new_name_creates_a_shortcut_and_says_so(wire):
    """The typo case. Telegram reports neither creation nor appending, and a
    misspelled name quietly becomes a second shortcut - so the answer has to
    volunteer which happened."""
    client = wire(_Client(shortcuts=[_shortcut(shortcut="thanks")]))

    answer = await mod.add_quick_reply(shortcut="thnaks", text="Thank you!", account="a")

    assert _record(answer)["created"] is True
    assert "A NEW shortcut" in answer
    assert "list_quick_replies" in answer
    assert client.sent("SendMessageRequest").quick_reply_shortcut.shortcut == "thnaks"


@pytest.mark.asyncio
async def test_an_existing_name_appends_without_claiming_creation(wire):
    wire(_Client(shortcuts=[_shortcut(shortcut="thanks")]))

    answer = await mod.add_quick_reply(shortcut="thanks", text="Thanks again", account="a")

    assert _record(answer)["created"] is False
    assert "A NEW shortcut" not in answer


@pytest.mark.asyncio
async def test_the_message_is_addressed_to_this_account_not_a_chat(wire):
    """Nothing is delivered to anybody: the flag is what files it under the name.
    A peer that was not self would send the text to someone."""
    client = wire()

    await mod.add_quick_reply(shortcut="thanks", text="Thank you!", account="a")

    sent = client.sent("SendMessageRequest")
    assert sent.peer.id == 7, "the quick reply was addressed somewhere else"
    assert sent.quick_reply_shortcut is not None


@pytest.mark.asyncio
async def test_a_leading_slash_is_not_part_of_the_name(wire):
    """`/thanks` is how it is typed, not what it is called."""
    client = wire()

    await mod.add_quick_reply(shortcut="/thanks", text="Thank you!", account="a")

    assert client.sent("SendMessageRequest").quick_reply_shortcut.shortcut == "thanks"


@pytest.mark.asyncio
async def test_an_empty_name_or_text_sends_nothing(wire):
    client = wire()

    blank_name = await mod.add_quick_reply(shortcut="  ", text="hi", account="a")
    blank_text = await mod.add_quick_reply(shortcut="thanks", text="   ", account="a")

    assert "needs a shortcut name" in blank_name
    assert "needs the message text" in blank_text
    assert client.requests == []


@pytest.mark.asyncio
async def test_renaming_keeps_the_messages_and_reports_both_names(wire):
    client = wire(_Client(shortcuts=[_shortcut(shortcut_id=1, shortcut="thanks", count=3)]))

    record = _record(await mod.rename_quick_reply(shortcut_id=1, shortcut="ty", account="a"))

    assert client.sent("EditQuickReplyShortcutRequest").shortcut == "ty"
    assert record["was"] == "thanks"
    assert record["now"] == "ty"
    assert record["messages"] == 3


@pytest.mark.asyncio
async def test_an_unknown_shortcut_id_is_refused_before_the_write(wire):
    client = wire(_Client(shortcuts=[_shortcut(shortcut_id=1)]))

    answer = await mod.rename_quick_reply(shortcut_id=42, shortcut="ty", account="a")

    assert "No quick-reply shortcut has id 42" in answer
    assert "EditQuickReplyShortcutRequest" not in client.names()


@pytest.mark.asyncio
async def test_omitting_message_ids_deletes_the_whole_shortcut(wire):
    """The irreversible reading of "delete the quick reply". It has to be the
    explicit one, and the answer says what went."""
    client = wire(_Client(shortcuts=[_shortcut(shortcut_id=1, count=5)]))

    record = _record(await mod.delete_quick_reply(shortcut_id=1, account="a"))

    assert "DeleteQuickReplyShortcutRequest" in client.names()
    assert record["deleted"] == "the whole shortcut"
    assert record["messages_lost"] == 5


@pytest.mark.asyncio
async def test_naming_message_ids_keeps_the_shortcut(wire):
    client = wire(_Client(shortcuts=[_shortcut(shortcut_id=1)]))

    record = _record(
        await mod.delete_quick_reply(shortcut_id=1, message_ids=[10, 11], account="a")
    )

    assert "DeleteQuickReplyShortcutRequest" not in client.names(), "the shortcut was destroyed"
    assert client.sent("DeleteQuickReplyMessagesRequest").id == [10, 11]
    assert record["deleted_message_ids"] == [10, 11]


@pytest.mark.asyncio
async def test_an_empty_id_list_is_refused_rather_than_read_as_delete_everything(wire):
    """`[]` is the shape a caller lands on when a filter matched nothing. Treating
    it as "no ids given" would destroy the shortcut."""
    client = wire(_Client(shortcuts=[_shortcut(shortcut_id=1)]))

    answer = await mod.delete_quick_reply(shortcut_id=1, message_ids=[], account="a")

    assert "Omit it entirely" in answer
    assert "DeleteQuickReplyShortcutRequest" not in client.names()


@pytest.mark.asyncio
async def test_reading_a_shortcut_returns_the_ids_a_deletion_takes(wire):
    wire(
        _Client(
            shortcuts=[_shortcut(shortcut_id=1)],
            messages=[SimpleNamespace(id=10, message="Thank you!", media=None)],
        )
    )

    record = _record(await mod.read_quick_reply(shortcut_id=1, account="a"))

    assert record["message_id"] == 10
    assert record["has_media"] is False
    assert "Thank you!" in record["text"]


def _stored(message_id=1, text="سلام✅", entities=None):
    """A quick-reply message as Telegram returns it."""
    return SimpleNamespace(
        id=message_id,
        message=text,
        media=None,
        entities=entities if entities is not None else [],
    )


@pytest.mark.asyncio
async def test_reading_a_quick_reply_reports_its_entities(wire):
    """The reader could not see what the writer could send.

    `add_quick_reply` has taken an `entities` list all along, and
    `read_quick_reply` reported only `text` - so a shortcut holding a premium
    emoji read back as the bare fallback glyph, and nothing in the package could
    say which custom emoji a saved reply actually carried. The glyph is not the
    emoji: `✅` in the text may be any premium document at all.
    """
    from telethon.tl.types import MessageEntityCustomEmoji

    client = wire(
        _Client(messages=[_stored(entities=[MessageEntityCustomEmoji(4, 1, 5776121630275149735)])])
    )
    del client

    record = _record(await mod.read_quick_reply(1, account="a"))

    assert record["entities"], "no entities reported"
    emoji = [e for e in record["entities"] if e["type"] == "custom_emoji"]
    assert len(emoji) == 1
    # A string, always: through a JSON number this id becomes a DIFFERENT emoji.
    assert emoji[0]["custom_emoji_id"] == "5776121630275149735"
    assert emoji[0]["offset"] == 4
    assert emoji[0]["length"] == 1


@pytest.mark.asyncio
async def test_a_quick_reply_with_no_formatting_reports_no_entities(wire):
    """The ordinary case stays quiet rather than growing an empty key."""
    wire(_Client(messages=[_stored(text="plain")]))

    record = _record(await mod.read_quick_reply(1, account="a"))

    assert "entities" not in record


@pytest.mark.asyncio
async def test_a_stored_reply_can_be_edited_in_place(wire):
    """Editing, not delete-and-re-add.

    `messages.editMessage` takes `quick_reply_shortcut_id`, so a stored reply can
    be corrected where it is. Re-adding would mint a new message id and move the
    reply to the end of the shortcut, which is a different thing from fixing one.
    """
    client = wire(_Client(messages=[_stored()]))

    payload = json.loads(
        await mod.edit_quick_reply(
            shortcut_id=1,
            message_id=1,
            message="سلام✅",
            entities=[
                {"type": "custom_emoji", "offset": 4, "length": 1, "custom_emoji_id": "999"}
            ],
            account="a",
        )
    )

    sent = client.sent("EditMessageRequest")
    assert sent is not None, f"no edit was sent: {client.names()}"
    assert sent.quick_reply_shortcut_id == 1
    assert sent.id == 1
    assert sent.message == "سلام✅"
    assert sent.entities == [
        {"type": "custom_emoji", "offset": 4, "length": 1, "custom_emoji_id": "999"}
    ]
    assert payload["results"][0]["edited"] is True


@pytest.mark.asyncio
async def test_editing_refuses_a_message_the_shortcut_does_not_hold(wire):
    """A wrong id would otherwise be sent to Telegram to be refused there, and
    the shortcut's own messages are already in hand to check against."""
    wire(_Client(messages=[_stored(message_id=1)]))

    refused = await mod.edit_quick_reply(shortcut_id=1, message_id=404, message="x", account="a")

    assert "404" in refused
