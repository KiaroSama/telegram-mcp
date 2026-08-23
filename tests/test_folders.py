"""Folder membership for the account's own chat (Saved Messages).

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which request was built rather than about a returned
string that could be right for the wrong reason.

Telegram stores Saved Messages in a dialog filter as ``InputPeerSelf``, which
``utils.get_peer_id`` refuses to cast. That bites twice: on the ``"me"``
argument, and on a peer already stored in the folder by an official client —
the second one breaks these tools for *every* chat_id, not just for ``"me"``.
"""

from types import SimpleNamespace

import pytest
from telethon import types

from telegram_mcp.tools import folders as mod
from telegram_mcp.tools.folders import add_chat_to_folder, remove_chat_from_folder

SELF_ID = 4242
SELF_PEER = types.InputPeerUser(user_id=SELF_ID, access_hash=99)
OTHER_PEER = types.InputPeerChannel(channel_id=7, access_hash=1)


def _folder(folder_id=1, include=(), pinned=()):
    return types.DialogFilter(
        id=folder_id,
        title=types.TextWithEntities(text="Work", entities=[]),
        pinned_peers=list(pinned),
        include_peers=list(include),
        exclude_peers=[],
    )


class _Client:
    """Records every TL request and answers the ones the folder tools send."""

    def __init__(self, folders=(), me=None):
        self.requests = []
        self.folders = list(folders)
        self.me = me or SELF_PEER

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetDialogFiltersRequest":
            return SimpleNamespace(filters=self.folders)
        if name == "UpdateDialogFilterRequest":
            return True
        raise AssertionError(f"unexpected request {name}")

    async def get_me(self, input_peer=False):
        return self.me

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, peer=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve(chat_id, _client):
            return peer if peer is not None else types.InputPeerSelf()

        monkeypatch.setattr(mod, "resolve_input_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_adding_saved_messages_stores_a_concrete_peer_not_input_peer_self(_wire):
    """InputPeerSelf has no id of its own, so it would not survive the round trip
    through UpdateDialogFilterRequest. Store what the account actually is."""
    client = _wire(_Client(folders=[_folder()]), peer=types.InputPeerSelf())

    result = await add_chat_to_folder(1, "me", account="a")

    assert "UpdateDialogFilterRequest" in client.names, result
    stored = client.sent("UpdateDialogFilterRequest").filter.include_peers
    assert len(stored) == 1
    assert isinstance(stored[0], types.InputPeerUser)
    assert stored[0].user_id == SELF_ID


@pytest.mark.asyncio
async def test_adding_saved_messages_twice_is_idempotent(_wire):
    client = _wire(_Client(folders=[_folder(include=[SELF_PEER])]), peer=types.InputPeerSelf())

    result = await add_chat_to_folder(1, "me", account="a")

    assert "already in folder" in result
    assert "UpdateDialogFilterRequest" not in client.names


@pytest.mark.asyncio
async def test_a_folder_that_already_holds_input_peer_self_does_not_break_other_chats(_wire):
    """A folder populated by an official Telegram client can already contain
    InputPeerSelf. Casting the *stored* peers is what breaks every other chat_id."""
    folder = _folder(include=[types.InputPeerSelf()])
    client = _wire(_Client(folders=[folder]), peer=OTHER_PEER)

    result = await add_chat_to_folder(1, -1007, account="a")

    assert "UpdateDialogFilterRequest" in client.names, result
    stored = client.sent("UpdateDialogFilterRequest").filter.include_peers
    assert any(isinstance(p, types.InputPeerSelf) for p in stored), "the stored self peer was lost"
    assert OTHER_PEER in stored


@pytest.mark.asyncio
async def test_removing_saved_messages_takes_it_out_of_the_folder(_wire):
    client = _wire(
        _Client(folders=[_folder(include=[SELF_PEER, OTHER_PEER])]), peer=types.InputPeerSelf()
    )

    result = await remove_chat_from_folder(1, "me", account="a")

    assert "UpdateDialogFilterRequest" in client.names, result
    stored = client.sent("UpdateDialogFilterRequest").filter.include_peers
    assert SELF_PEER not in stored
    assert stored == [OTHER_PEER]


@pytest.mark.asyncio
async def test_no_folder_tool_leaks_a_telethon_cast_message(monkeypatch):
    """The reported symptom: Telethon's internal cast complaint presented to the
    user as if it were the answer to their question."""

    async def _resolve(chat_id, _client):
        return types.InputPeerSelf()

    monkeypatch.setattr(mod, "resolve_input_entity", _resolve)

    for tool in (add_chat_to_folder, remove_chat_from_folder):
        client = _Client(folders=[_folder(include=[SELF_PEER])])
        monkeypatch.setattr(mod, "get_client", lambda account=None, _c=client: _c)

        result = await tool(1, "me", account="a")

        assert "Cannot cast" not in result, f"{tool.__name__} leaked the cast message"
        assert "InputPeerSelf" not in result, f"{tool.__name__} leaked the peer type name"
        assert client.names  # the tool actually ran rather than short-circuiting
