"""Opening a Mini App: which TL method each way in selects, and what comes back.

No network. The assertions are about the REQUEST built, because that is the whole
decision this tool makes — Telegram has three different methods for "open the
Mini App" and picking the wrong one either fails or opens a different app.

The other thing worth a test is the warning. The returned URL is a credential:
its initData identifies the account to the app. A refactor that trims the answer
to "just the url" would be a silent downgrade, so the warning is asserted on.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import mini_apps as mod


class _Client:
    """Records the TL requests and answers with a launch URL."""

    def __init__(self, result=None):
        self.requests = []
        self.result = result if result is not None else _url_result()

    async def __call__(self, request):
        self.requests.append(request)
        return self.result

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]


def _url_result(url="https://app.example/?tgWebAppData=signed", **extra):
    fields = {"query_id": None, "fullsize": None, "fullscreen": None}
    fields.update(extra)
    return SimpleNamespace(url=url, **fields)


@pytest.fixture
def wire(monkeypatch):
    def _wire(client=None):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(target, _client):
            return SimpleNamespace(peer=str(target))

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_input_entity", _resolve)
        return client

    return _wire


def _payload(answer):
    """The record inside a format_tool_result answer."""
    return json.loads(answer)["results"]


@pytest.mark.asyncio
async def test_no_app_named_opens_the_bots_main_one(wire):
    """A bot's profile opens its MAIN app, and that is its own TL method. Falling
    back to RequestWebViewRequest with no url would open nothing."""
    client = wire()

    await mod.open_mini_app(bot="@somebot", account="a")

    assert client.names == ["RequestMainWebViewRequest"]


@pytest.mark.asyncio
async def test_a_short_name_opens_that_named_app(wire):
    """t.me/<bot>/<app> is a named app: messages.requestAppWebView, and the app is
    an InputBotAppShortName - the bot alone would open the wrong thing."""
    client = wire()

    await mod.open_mini_app(bot="@somebot", short_name="wallet", account="a")

    (sent,) = client.requests
    assert type(sent).__name__ == "RequestAppWebViewRequest"
    assert type(sent.app).__name__ == "InputBotAppShortName"
    assert sent.app.short_name == "wallet"


@pytest.mark.asyncio
async def test_a_button_url_opens_exactly_that_app(wire):
    """The url an inline webview button carries goes through requestWebView, whose
    `url` field is what says WHICH app - the same bot can have several."""
    client = wire()

    await mod.open_mini_app(bot="@somebot", url="https://app.example/x", account="a")

    (sent,) = client.requests
    assert type(sent).__name__ == "RequestWebViewRequest"
    assert sent.url == "https://app.example/x"


@pytest.mark.asyncio
async def test_both_ways_at_once_is_refused_rather_than_one_picked(wire):
    """short_name and url name different apps. Honouring one silently would open
    an app the caller did not ask for and report success."""
    client = wire()

    answer = await mod.open_mini_app(
        bot="@somebot", short_name="wallet", url="https://app.example/x", account="a"
    )

    assert "not both" in answer
    assert client.requests == [], "a request went out for an ambiguous call"


@pytest.mark.asyncio
async def test_the_peer_defaults_to_the_bot_and_is_overridable(wire):
    """A Mini App reads the peer as its context. Defaulting to the bot is what a
    client does; passing a chat has to actually change it."""
    client = wire()

    await mod.open_mini_app(bot="@somebot", account="a")
    await mod.open_mini_app(bot="@somebot", chat_id=-100123, account="a")

    default_peer, explicit_peer = (r.peer for r in client.requests)
    assert default_peer.peer == "@somebot", "the default peer is not the bot"
    assert explicit_peer.peer == "-100123"


@pytest.mark.asyncio
async def test_the_url_comes_back_whole_and_labelled_a_credential(wire):
    """initData in the fragment identifies the account to the app, so the string is
    as good as a session for that app. Truncating it would make it useless without
    making it safe, so it is returned whole WITH the warning."""
    signed = "https://app.example/?tgWebAppData=user%3D%7B%22id%22%3A7%7D%26hash%3Dabc"
    client = wire(_Client(_url_result(url=signed)))

    answer = await mod.open_mini_app(bot="@somebot", account="a")

    assert signed in answer, "the URL was altered or truncated"
    assert "CREDENTIAL" in answer, "nothing says the URL identifies the account"
    assert client.names == ["RequestMainWebViewRequest"]


@pytest.mark.asyncio
async def test_a_query_id_is_reported_as_a_string(wire):
    """Telegram's query_id is a 64-bit integer and JSON has no such thing: through a
    float it comes back a DIFFERENT id. The same rounding already made
    get_custom_emoji unusable once."""
    wire(_Client(_url_result(query_id=5934007978150595964)))

    answer = await mod.open_mini_app(bot="@somebot", account="a")

    assert _payload(answer)["query_id"] == "5934007978150595964"


@pytest.mark.asyncio
async def test_a_bot_with_no_such_app_is_explained(wire):
    """Telegram answers the request and simply returns no url. Reporting that as a
    success with an empty field would send the caller to a browser with nothing."""
    wire(_Client(_url_result(url=None)))

    answer = await mod.open_mini_app(bot="@somebot", account="a")

    assert "no URL" in answer
    assert "main app" in answer, "it does not say which kind of app was asked for"
