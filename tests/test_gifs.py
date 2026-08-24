"""Searching for a GIF and sending the one that was found.

`functions.messages.SearchGifsRequest` does not exist in Telethon 1.44, so the
search always fell through to a fallback that read the HISTORY of a chat called
`@gif` instead of asking the inline bot anything. What it returned was a bare
`document.id`, and a document id is not something that can be sent: Telethon
answers `TypeError: Cannot cast int to any kind of InputMedia`, because sending
media by id needs the access hash and file reference too.

The pair is an inline-bot flow: `messages.getInlineBotResults` answers with a
`query_id` and a result `id`, and `messages.sendInlineBotResult` takes that pair
back. So the search hands out an opaque handle carrying exactly what the send
needs, scoped to the account that obtained it.

No network: a fake client records the TL requests it was handed.
"""

import json
from types import SimpleNamespace

import pytest
from telethon.tl.types import InputPeerUser, InputUser

from telegram_mcp.tools import media as mod


class _Client:
    def __init__(self, results=None, next_offset=None, cache_time=300):
        self.requests = []
        self.results = results
        self.next_offset = next_offset
        self.cache_time = cache_time

    async def get_input_entity(self, who):
        return InputPeerUser(user_id=429000, access_hash=7)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetInlineBotResultsRequest":
            return SimpleNamespace(
                query_id=112233,
                results=self.results if self.results is not None else [],
                cache_time=self.cache_time,
                users=[],
                next_offset=self.next_offset,
            )
        return SimpleNamespace(updates=[])

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]


def _result(result_id="abc:def", title="a cat"):
    return SimpleNamespace(id=result_id, type="gif", title=title, description=None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client=None):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(mod, "ensure_connected", _ensure, raising=False)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_search_asks_the_inline_bot_rather_than_a_chats_history(_wire):
    client = _wire(_Client(results=[_result()]))

    await mod.get_gif_search("cat", account="a")

    assert client.names == ["GetInlineBotResultsRequest"]
    request = client.sent("GetInlineBotResultsRequest")
    assert request.query == "cat"
    assert isinstance(request.bot, InputUser), "the bot must be an InputUser"


@pytest.mark.asyncio
async def test_a_result_comes_back_as_a_sendable_handle_not_a_bare_document_id(_wire):
    _wire(_Client(results=[_result("abc:def")]))

    payload = json.loads(await mod.get_gif_search("cat", account="a"))
    handle = payload["results"][0]["gif_id"]

    assert isinstance(handle, str)
    assert "112233" in handle and "abc:def" in handle


@pytest.mark.asyncio
async def test_the_search_reports_the_cursor_for_the_next_page(_wire):
    _wire(_Client(results=[_result()], next_offset="30"))

    payload = json.loads(await mod.get_gif_search("cat", offset="0", account="a"))

    assert payload["next_offset"] == "30"


@pytest.mark.asyncio
async def test_no_results_is_reported_as_an_empty_page_not_as_a_failure(_wire):
    _wire(_Client(results=[]))

    payload = json.loads(await mod.get_gif_search("zzzz", account="a"))

    assert payload["results"] == []
    assert payload["returned"] == 0


@pytest.mark.asyncio
async def test_sending_a_handle_uses_the_inline_result_mechanism(_wire):
    client = _wire(_Client(results=[_result("abc:def")]))
    handle = json.loads(await mod.get_gif_search("cat", account="a"))["results"][0]["gif_id"]

    result = await mod.send_gif(1, handle, account="a")

    sent = client.sent("SendInlineBotResultRequest")
    assert sent is not None, "the GIF was not sent through the inline result"
    assert sent.query_id == 112233
    assert sent.id == "abc:def"
    assert "sent" in result.lower()


@pytest.mark.asyncio
async def test_a_topic_id_becomes_a_reply_target_not_a_raw_integer(_wire):
    client = _wire(_Client(results=[_result()]))
    handle = json.loads(await mod.get_gif_search("cat", account="a"))["results"][0]["gif_id"]

    await mod.send_gif(1, handle, topic_id=99, account="a")

    reply_to = client.sent("SendInlineBotResultRequest").reply_to
    assert getattr(reply_to, "reply_to_msg_id", None) == 99


@pytest.mark.asyncio
async def test_a_bare_document_id_is_refused_with_the_reason(_wire):
    """Telethon answers "Cannot cast int to any kind of InputMedia"; saying so up
    front is more use than the cast error."""
    client = _wire()

    result = await mod.send_gif(1, 123456789, account="a")

    assert "get_gif_search" in result
    assert client.sent("SendInlineBotResultRequest") is None


@pytest.mark.asyncio
async def test_a_handle_from_another_account_is_refused(_wire):
    """The query_id belongs to the session that made the query; replaying it from
    a different account sends nothing and reports nothing useful."""
    client = _wire(_Client(results=[_result()]))
    handle = json.loads(await mod.get_gif_search("cat", account="work"))["results"][0]["gif_id"]

    result = await mod.send_gif(1, handle, account="personal")

    assert "account" in result.lower()
    assert client.sent("SendInlineBotResultRequest") is None


@pytest.mark.asyncio
async def test_a_stale_handle_is_refused_before_the_request(_wire):
    """Telegram caches an inline query for cache_time seconds and then forgets the
    query_id."""
    client = _wire(_Client(results=[_result()], cache_time=0))
    handle = json.loads(await mod.get_gif_search("cat", account="a"))["results"][0]["gif_id"]

    result = await mod.send_gif(1, handle, account="a")

    assert "expired" in result.lower() or "stale" in result.lower()
    assert client.sent("SendInlineBotResultRequest") is None


@pytest.mark.asyncio
async def test_a_handle_that_is_not_one_says_so(_wire):
    client = _wire()

    result = await mod.send_gif(1, "not-a-handle", account="a")

    assert "get_gif_search" in result
    assert client.sent("SendInlineBotResultRequest") is None
