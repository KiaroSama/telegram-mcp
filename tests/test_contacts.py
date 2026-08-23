"""Contact tools: what request they build, and who they build it for.

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which request the tool built rather than about a
returned string that could be right for the wrong reason.
"""

import json
from types import SimpleNamespace

import pytest
from telethon.tl.types import User

from telegram_mcp.tools import contacts as mod
from telegram_mcp.tools.contacts import get_contact_chats


def _user(user_id=4242, first="Ada", last="Lovelace", username="someone"):
    return User(id=user_id, first_name=first, last_name=last, username=username, phone="123")


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, peer_unread=0, common=()):
        self.requests = []
        self.peer_unread = peer_unread
        self.common = list(common)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetPeerDialogsRequest":
            return SimpleNamespace(
                dialogs=[SimpleNamespace(unread_count=self.peer_unread, folder_id=0)]
            )
        raise AssertionError(f"unexpected request {name}")

    async def get_input_entity(self, entity):
        return SimpleNamespace(peer=entity)

    async def get_common_chats(self, entity):
        return self.common

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, entity=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(contact_id, _client):
            return entity if entity is not None else _user()

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_a_contact_given_by_username_still_matches_its_direct_chat(_wire):
    """A username argument stays a str all the way into the tool body — only
    resolve_entity turns it into a User. Matching against the raw argument
    therefore never fires for a username, and the tool reports a confident
    negative instead of the chat that exists."""
    client = _wire(_Client(peer_unread=3), entity=_user(4242))

    result = await get_contact_chats("@someone", account="a")

    assert "No chats found" not in result, "the direct chat was silently omitted"
    assert "GetPeerDialogsRequest" in client.names
    payload = json.loads(result)
    private = [r for r in payload["results"] if r.get("type") == "Private"]
    assert private == [{"chat_id": 4242, "type": "Private", "unread": 3}]
