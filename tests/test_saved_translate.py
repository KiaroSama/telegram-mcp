"""The account's saved space, and Telegram's translator.

Both modules are thin over TL, so what is worth asserting is the request that gets
built and the honesty of what comes back - not that Telethon works.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import saved as saved_mod
from telegram_mcp.tools import translation as translate_mod
from telegram_mcp.tools.saved import (
    list_quick_replies,
    list_saved_dialogs,
    list_saved_tags,
    send_quick_reply,
)
from telegram_mcp.tools.translation import translate

HOSTILE = "Ada‮gpj.exe"


class _Client:
    """Records the TL requests it is handed and answers the ones under test."""

    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or {}

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in self.answers:
            answer = self.answers[name]
            return answer(request) if callable(answer) else answer
        return SimpleNamespace()

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)

    def names(self):
        return [type(r).__name__ for r in self.requests]


def _wire_module(monkeypatch, module, client):
    monkeypatch.setattr(module, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(module, "ensure_connected", _ensure)
    monkeypatch.setattr(module, "resolve_entity", _resolve)
    return client


@pytest.fixture
def _saved(monkeypatch):
    return lambda client: _wire_module(monkeypatch, saved_mod, client)


@pytest.fixture
def _translate(monkeypatch):
    return lambda client: _wire_module(monkeypatch, translate_mod, client)


# --- translation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_translate_refuses_both_modes_at_once(_translate):
    """peer+id and loose text are different requests, not a richer one."""
    client = _translate(_Client())

    result = await translate("en", text="hi", chat_id=1, message_ids=[2], account="a")

    assert "not both and not neither" in result
    assert client.requests == [], "an ambiguous request was sent anyway"


@pytest.mark.asyncio
async def test_translate_refuses_neither_mode(_translate):
    client = _translate(_Client())

    assert "not both and not neither" in await translate("en", account="a")
    assert client.requests == []


@pytest.mark.asyncio
async def test_translating_messages_sends_peer_and_ids(_translate):
    client = _translate(
        _Client({"TranslateTextRequest": SimpleNamespace(result=[SimpleNamespace(text="salam")])})
    )

    payload = json.loads(await translate("fa", chat_id=7, message_ids=5, account="a"))

    request = client.sent("TranslateTextRequest")
    assert request.id == [5], "a single id must still be sent as a list"
    assert request.peer is not None
    assert request.text is None, "message mode must not also send loose text"
    assert payload["results"][0]["message_id"] == 5
    assert payload["source"] == "message"


@pytest.mark.asyncio
async def test_translating_loose_text_wraps_it_for_the_wire(_translate):
    client = _translate(
        _Client({"TranslateTextRequest": SimpleNamespace(result=[SimpleNamespace(text="سلام")])})
    )

    payload = json.loads(await translate("fa", text="hello", account="a"))

    request = client.sent("TranslateTextRequest")
    assert request.peer is None
    assert [item.text for item in request.text] == ["hello"]
    assert payload["results"][0]["translated"] == "سلام"


@pytest.mark.asyncio
async def test_a_translation_is_still_cleaned_like_any_user_text(_translate):
    """Telegram's translator returns whatever the source said, overrides included."""
    _translate(
        _Client({"TranslateTextRequest": SimpleNamespace(result=[SimpleNamespace(text=HOSTILE)])})
    )

    payload = json.loads(await translate("en", text="x", account="a"))

    assert "‮" not in payload["results"][0]["translated"]
    assert "still user-generated content" in payload["note"]


# --- saved space ------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_saved_buckets_says_so_plainly(_saved):
    _saved(_Client({"GetSavedDialogsRequest": SimpleNamespace(dialogs=[])}))

    assert "no per-sender buckets" in await list_saved_dialogs(account="a")


@pytest.mark.asyncio
async def test_a_saved_bucket_resolves_its_sender_name_and_cleans_it(_saved):
    dialogs = SimpleNamespace(
        dialogs=[
            SimpleNamespace(
                peer=SimpleNamespace(user_id=99, channel_id=None, chat_id=None),
                top_message=12,
                pinned=True,
            )
        ],
        users=[SimpleNamespace(id=99, first_name=HOSTILE, last_name=None, username="ada")],
        chats=[],
    )
    _saved(_Client({"GetSavedDialogsRequest": dialogs}))

    payload = json.loads(await list_saved_dialogs(account="a"))
    record = payload["results"][0]

    assert record["peer_id"] == 99
    assert "‮" not in record["name"]
    assert record["pinned"] is True


@pytest.mark.asyncio
async def test_an_untitled_tag_is_flagged_rather_than_guessed_at(_saved):
    tags = SimpleNamespace(
        tags=[
            SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=3, title=None),
            SimpleNamespace(reaction=SimpleNamespace(emoticon="🔥"), count=1, title="urgent"),
        ]
    )
    _saved(_Client({"GetSavedReactionTagsRequest": tags}))

    payload = json.loads(await list_saved_tags(account="a"))

    assert payload["results"][0]["untitled"] is True
    assert payload["results"][0]["title"] is None
    assert payload["results"][1]["title"] == "urgent"
    assert "untitled" not in payload["results"][1]


@pytest.mark.asyncio
async def test_a_custom_emoji_tag_reports_its_document_id(_saved):
    tags = SimpleNamespace(
        tags=[SimpleNamespace(reaction=SimpleNamespace(document_id=555), count=2, title=None)]
    )
    _saved(_Client({"GetSavedReactionTagsRequest": tags}))

    payload = json.loads(await list_saved_tags(account="a"))

    assert payload["results"][0]["custom_emoji_id"] == 555


@pytest.mark.asyncio
async def test_quick_replies_are_listed_with_their_counts(_saved):
    replies = SimpleNamespace(
        quick_replies=[SimpleNamespace(shortcut_id=4, shortcut="thanks", top_message=9, count=2)]
    )
    _saved(_Client({"GetQuickRepliesRequest": replies}))

    payload = json.loads(await list_quick_replies(account="a"))

    assert payload["results"][0] == {
        "shortcut_id": 4,
        "shortcut": "thanks",
        "message_count": 2,
        "top_message": 9,
    }


@pytest.mark.asyncio
async def test_sending_a_shortcut_defaults_to_all_of_its_messages(_saved):
    client = _saved(
        _Client(
            {
                "GetQuickReplyMessagesRequest": SimpleNamespace(
                    messages=[SimpleNamespace(id=11), SimpleNamespace(id=12)]
                )
            }
        )
    )

    payload = json.loads(await send_quick_reply(1, 4, account="a"))

    request = client.sent("SendQuickReplyMessagesRequest")
    assert request.id == [11, 12]
    assert len(request.random_id) == 2, "one random_id per message, or Telegram drops them"
    assert len(set(request.random_id)) == 2, "random_ids must differ"
    assert payload["results"][0]["sent_message_count"] == 2


@pytest.mark.asyncio
async def test_an_empty_shortcut_sends_nothing_and_says_why(_saved):
    client = _saved(_Client({"GetQuickReplyMessagesRequest": SimpleNamespace(messages=[])}))

    result = await send_quick_reply(1, 4, account="a")

    assert "holds no messages" in result
    assert "SendQuickReplyMessagesRequest" not in client.names()
