"""Finding a contact's chat must not download every dialog on the account.

No network: the client is a fake that records the TL requests and get_dialogs calls
it was handed. get_contact_chats wants exactly one peer's dialog, which is what
GetPeerDialogsRequest is for — the same request get_chat already uses (see
tests/test_get_chat.py).
"""

import json
from types import SimpleNamespace

import pytest
from telethon.tl.types import User

from telegram_mcp.tools import contacts as mod

FAKE_INPUT_PEER = object()


def _user(user_id, first="Ada", last="Lovelace", username="ada", phone="123"):
    return User(id=user_id, first_name=first, last_name=last, username=username, phone=phone)


class _ContactClient:
    """Answers the contact and dialog requests these tools send, and records them."""

    def __init__(self, contacts=(), dialogs=(), peer_unread=0, has_peer_dialog=True):
        self.contacts = list(contacts)
        self.dialogs = list(dialogs)
        self.peer_unread = peer_unread
        self.has_peer_dialog = has_peer_dialog
        self.requests = []
        self.dialog_limits = []
        self.peer_dialog_peers = None

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetContactsRequest":
            return SimpleNamespace(users=self.contacts)
        if name == "GetPeerDialogsRequest":
            self.peer_dialog_peers = [p.peer for p in request.peers]
            if not self.has_peer_dialog:
                return SimpleNamespace(dialogs=[])
            return SimpleNamespace(
                dialogs=[SimpleNamespace(unread_count=self.peer_unread, folder_id=0)]
            )
        raise AssertionError(f"unexpected request {name}")

    async def get_dialogs(self, limit=None, **kwargs):
        self.dialog_limits.append(limit)
        return list(self.dialogs)

    async def get_input_entity(self, entity):
        return FAKE_INPUT_PEER

    async def get_common_chats(self, entity):
        return []

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, entity=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return entity

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        monkeypatch.setattr(mod, "get_marked_id", lambda e: e.id)
        monkeypatch.setattr(mod, "get_entity_type", lambda e: "Private")
        return client

    return wire


@pytest.mark.asyncio
async def test_finding_a_contacts_direct_chat_scans_the_dialog_list_once(_wire):
    """The old shape was `for contact: for dialog:` over a list fetched in full —
    quadratic in the number of matched contacts."""
    contacts = [_user(1, "Ada"), _user(2, "Adam")]
    dialogs = [SimpleNamespace(entity=_user(index), unread_count=index) for index in range(1, 200)]
    client = _wire(_ContactClient(contacts=contacts, dialogs=dialogs))

    payload = json.loads(await mod.get_direct_chat_by_contact("Ada", account="a"))

    assert len(client.dialog_limits) == 1, "the dialog list was fetched more than once"
    assert {r["chat_id"] for r in payload["results"]} == {1, 2}
    assert [r["unread"] for r in payload["results"]] == [1, 2]


@pytest.mark.asyncio
async def test_one_contacts_chat_is_resolved_by_peer_not_by_listing_everything(_wire):
    contact = _user(42)
    client = _wire(_ContactClient(peer_unread=7), entity=contact)

    payload = json.loads(await mod.get_contact_chats(42, account="a"))

    assert client.dialog_limits == [], "get_contact_chats still lists every dialog"
    assert client.peer_dialog_peers == [FAKE_INPUT_PEER]
    assert payload["results"][0] == {"chat_id": 42, "type": "Private", "unread": 7}


@pytest.mark.asyncio
async def test_a_contact_with_no_direct_chat_is_reported_not_invented(_wire):
    """An empty `dialogs` list is a normal answer — the contact exists but this
    account has never opened a chat with them."""
    contact = _user(42)
    client = _wire(_ContactClient(has_peer_dialog=False), entity=contact)

    result = await mod.get_contact_chats(42, account="a")

    assert "No chats found" in result
    assert client.peer_dialog_peers == [FAKE_INPUT_PEER]
    assert client.dialog_limits == [], "get_contact_chats still lists every dialog"
