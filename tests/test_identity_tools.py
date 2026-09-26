"""The owner's identity: an owned bot's profile text and the owner's own @username.

Both are asserted on the request, because that is where "whose" and "what" show:
a bot request that loses its `bot` flag edits the caller instead, and a username
request carries the one string Telegram will publish.
"""

import pytest
from telethon.errors import RPCError
from telethon.tl import functions, types

from telegram_mcp.tools import identity_tools as mod

BOT = types.InputPeerUser(user_id=777, access_hash=9)


class Client:
    def __init__(self, fail=None):
        self.sent = []
        self._fail = fail

    async def __call__(self, request):
        self.sent.append(request)
        if self._fail:
            raise RPCError(request=request, message=self._fail, code=400)
        return True


async def _bot(chat_id, _cl=None, _account=None):
    assert chat_id == "@mybot"
    return BOT


# --------------------------------------------------------------- set_bot_info


@pytest.mark.asyncio
async def test_bot_info_sends_only_the_given_fields_for_every_language(wire_client):
    client = wire_client(mod, Client(), resolve=_bot)

    answer = await mod.set_bot_info(bot="@mybot", name="Numera", about="Hi")

    (request,) = client.sent
    assert isinstance(request, functions.bots.SetBotInfoRequest)
    assert request.bot is BOT, "without the flag Telegram edits the caller"
    assert request.lang_code == ""
    assert (request.name, request.about, request.description) == ("Numera", "Hi", None)
    assert "name" in answer and "about" in answer


@pytest.mark.asyncio
async def test_bot_info_with_nothing_to_change_sends_nothing(wire_client):
    client = wire_client(mod, Client(), resolve=_bot)

    answer = await mod.set_bot_info(bot="@mybot")

    assert client.sent == []
    assert "name" in answer and "description" in answer


@pytest.mark.asyncio
async def test_a_bot_the_account_does_not_own_is_refused_by_name(wire_client):
    wire_client(mod, Client(fail="BOT_INVALID"), resolve=_bot)

    answer = await mod.set_bot_info(bot="@mybot", name="X")

    assert "BOT_INVALID" in answer


# ------------------------------------------------------------ set_my_username


@pytest.mark.asyncio
async def test_username_is_sent_without_its_at_sign(wire_client):
    client = wire_client(mod, Client())

    answer = await mod.set_my_username("@refx_nexus_3")

    (request,) = client.sent
    assert isinstance(request, functions.account.UpdateUsernameRequest)
    assert request.username == "refx_nexus_3"
    assert "@refx_nexus_3" in answer


@pytest.mark.asyncio
async def test_an_empty_username_clears_it_and_says_so(wire_client):
    client = wire_client(mod, Client())

    answer = await mod.set_my_username("")

    assert client.sent[0].username == ""
    assert "removed" in answer.lower()


@pytest.mark.asyncio
async def test_a_missing_username_removes_nothing(wire_client):
    """Removal is asked for with "", never arrived at by omission."""
    client = wire_client(mod, Client())

    answer = await mod.set_my_username(None)

    assert client.sent == []
    assert '""' in answer


@pytest.mark.parametrize(
    "code", ["USERNAME_OCCUPIED", "USERNAME_INVALID", "USERNAME_NOT_MODIFIED"]
)
@pytest.mark.asyncio
async def test_telegrams_refusal_is_named(wire_client, code):
    wire_client(mod, Client(fail=code))

    answer = await mod.set_my_username("taken_name")

    assert code in answer


def test_the_channel_username_refusal_points_at_the_tool_that_sets_a_users_handle():
    """It named `update_profile`, which has no username field at all."""
    from telegram_mcp.tools import channel_admin

    user = types.User(id=5, first_name="A")
    hint = channel_admin._not_a_channel(5, user)

    assert "set_my_username" in hint
    assert "update_profile" not in hint
