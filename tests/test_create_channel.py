"""Creating a channel or supergroup, private or public under a chosen username.

No network. The whole point of these assertions is the ORDER of two requests.
Telegram's `channels.createChannel` cannot take a username, so a public chat is
made private and then renamed — and if the name turns out to be taken, the chat
already exists. Every check that can happen before creation therefore has to.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import groups as mod


class _Client:
    """Records the TL requests and answers the ones this tool sends."""

    def __init__(self, username_free=True, rename_error=None):
        self.requests = []
        self.username_free = username_free
        self.rename_error = rename_error

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "CheckUsernameRequest":
            return self.username_free
        if name == "CreateChannelRequest":
            return SimpleNamespace(chats=[SimpleNamespace(id=1234, title=request.title)])
        if name == "UpdateUsernameRequest":
            if self.rename_error:
                raise self.rename_error
            return True
        raise AssertionError(f"unexpected request {name}")

    @property
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

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        return client

    return _wire


def _record(answer):
    return json.loads(answer)["results"][0]


@pytest.mark.asyncio
async def test_no_username_creates_a_private_chat_and_asks_nothing_else(wire):
    """The ordinary case has to stay one request. A availability check for a name
    nobody gave would be a round trip for nothing."""
    client = wire()

    record = _record(await mod.create_channel(title="Ops", account="a"))

    assert client.names == ["CreateChannelRequest"]
    assert record["public"] is False
    assert record["chat_id"] == 1234
    assert "username" not in record


@pytest.mark.asyncio
async def test_a_username_is_checked_BEFORE_the_chat_is_created(wire):
    """The assertion this file exists for. Telegram cannot create a named
    channel in one call, so a taken name discovered afterwards leaves a stray
    private channel the caller never asked for and has to go and delete."""
    client = wire()

    await mod.create_channel(title="Ops", username="ops_room", account="a")

    assert client.names == [
        "CheckUsernameRequest",
        "CreateChannelRequest",
        "UpdateUsernameRequest",
    ], f"wrong order: {client.names}"


@pytest.mark.asyncio
async def test_a_taken_name_creates_nothing_at_all(wire):
    client = wire(_Client(username_free=False))

    answer = await mod.create_channel(title="Ops", username="ops_room", account="a")

    assert "already taken" in answer
    assert "CreateChannelRequest" not in client.names, "a chat was created for a taken name"


@pytest.mark.asyncio
async def test_a_name_that_breaks_a_rule_creates_nothing_either(wire):
    """The rules Telegram states can be checked locally, so they are - and the
    refusal says nothing was created, which is the part that matters."""
    client = wire()

    answer = await mod.create_channel(title="Ops", username="ab", account="a")

    assert "Nothing was created" in answer
    assert client.requests == []


@pytest.mark.asyncio
async def test_a_public_chat_reports_its_address(wire):
    wire()

    record = _record(await mod.create_channel(title="Ops", username="@ops_room", account="a"))

    assert record["public"] is True
    assert record["username"] == "ops_room", "the leading @ was not stripped"
    assert record["link"] == "https://t.me/ops_room"


@pytest.mark.asyncio
async def test_a_failed_rename_says_the_chat_exists_rather_than_reporting_failure(wire):
    """The race this cannot prevent: the name goes between the check and the
    rename. The chat is real and the caller owns it, so an error message that
    looks like nothing happened is how someone ends up with two channels."""
    client = wire(_Client(rename_error=RuntimeError("USERNAME_OCCUPIED")))

    answer = await mod.create_channel(title="Ops", username="ops_room", account="a")
    record = _record(answer)

    assert record["chat_id"] == 1234, "the id of the created chat was lost"
    assert record["public"] is False
    assert "WAS created" in answer
    assert "Do not create another one" in answer
    assert "set_channel_username" in answer, "no way to finish the job was named"
    assert client.sent("UpdateUsernameRequest") is not None


@pytest.mark.asyncio
async def test_megagroup_decides_which_kind_is_reported(wire):
    """A supergroup and a broadcast channel are the same TL call with one flag,
    and they behave nothing alike - the answer has to say which arrived."""
    wire()

    channel = _record(await mod.create_channel(title="Ops", account="a"))
    supergroup = _record(await mod.create_channel(title="Ops", megagroup=True, account="a"))

    assert channel["kind"] == "channel"
    assert supergroup["kind"] == "supergroup"


@pytest.mark.asyncio
async def test_a_private_chat_is_told_how_anyone_joins_it(wire):
    """A private chat with no invite link is unreachable, which is a surprise
    worth heading off at the moment of creation."""
    wire()

    answer = await mod.create_channel(title="Ops", account="a")

    assert "create_invite_link" in answer
