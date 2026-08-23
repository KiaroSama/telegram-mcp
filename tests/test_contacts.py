"""Contact tools: what request they build, and who they build it for.

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which request the tool built rather than about a
returned string that could be right for the wrong reason.

These tools add and delete contacts and block people on a real account. The
recorder's `names` and `sent()` are how a test tells "did the right thing" from
"said the right thing while sending nothing".
"""

import json
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, InputPhoneContact, InputUser, User

from telegram_mcp.tools import contacts as mod
from telegram_mcp.tools.contacts import (
    add_contact,
    block_user,
    delete_contact,
    get_contact_chats,
    import_contacts,
    unblock_user,
)


def _user(user_id=4242, first="Ada", last="Lovelace", username="someone", access_hash=777):
    return User(
        id=user_id,
        first_name=first,
        last_name=last,
        username=username,
        phone="123",
        access_hash=access_hash,
    )


def _channel(channel_id=7):
    return Channel(
        id=channel_id,
        title="Ops Room",
        photo=None,
        date=None,
        creator=False,
        left=False,
        broadcast=True,
        megagroup=False,
    )


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, resolved=(), imported=(), peer_unread=0, common=(), updates=("ok",)):
        self.requests = []
        self.resolved = list(resolved)
        self.imported = list(imported)
        self.peer_unread = peer_unread
        self.common = list(common)
        self.updates = list(updates)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "ResolveUsernameRequest":
            return SimpleNamespace(users=self.resolved, chats=[])
        if name == "AddContactRequest":
            return SimpleNamespace(updates=self.updates)
        if name == "ImportContactsRequest":
            return SimpleNamespace(imported=self.imported)
        if name in ("DeleteContactsRequest", "BlockRequest", "UnblockRequest"):
            return SimpleNamespace(updates=[])
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


# --- finding a contact's chat -----------------------------------------------


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


# --- adding a contact --------------------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_contact_by_username_resolves_first_then_adds(_wire):
    """A username is not an id. The resolve has to happen first, and the add has to
    carry the access_hash that came back — not a guess."""
    client = _wire(_Client(resolved=[_user(4242, access_hash=777)]))

    await add_contact(username="@bob", first_name="Ada", last_name="Lovelace", account="a")

    assert client.names == ["ResolveUsernameRequest", "AddContactRequest"]
    assert client.sent("ResolveUsernameRequest").username == "bob", "the @ was not stripped"
    added = client.sent("AddContactRequest")
    assert isinstance(added.id, InputUser)
    assert (added.id.user_id, added.id.access_hash) == (4242, 777)
    assert (added.first_name, added.last_name) == ("Ada", "Lovelace")


@pytest.mark.asyncio
async def test_a_username_that_resolves_to_no_user_adds_nothing(_wire):
    client = _wire(_Client(resolved=[]))

    result = await add_contact(username="ghost", account="a")

    assert "not found" in result
    assert "AddContactRequest" not in client.names, "a contact was added for nobody"


@pytest.mark.asyncio
async def test_a_username_that_resolves_to_a_channel_adds_nothing(_wire):
    """A channel is not a person; adding it as a contact would be nonsense."""
    client = _wire(_Client(resolved=[_channel()]))

    result = await add_contact(username="ops_room", account="a")

    assert "not a user" in result
    assert "AddContactRequest" not in client.names


@pytest.mark.asyncio
async def test_adding_a_contact_by_phone_sends_one_import_request(_wire):
    """Exactly one request is also the proof that the ImportError/AttributeError
    fallback at contacts.py:425-448 — which sends a plain dict instead of an
    InputPhoneContact — was not silently taken."""
    client = _wire(_Client(imported=["ok"]))

    await add_contact(phone="+15550100", first_name="Ada", last_name="Lovelace", account="a")

    assert client.names == ["ImportContactsRequest"]
    (entry,) = client.sent("ImportContactsRequest").contacts
    assert isinstance(entry, InputPhoneContact), "the fallback dict path was taken"
    assert entry.phone == "+15550100"
    assert (entry.first_name, entry.last_name) == ("Ada", "Lovelace")


@pytest.mark.asyncio
async def test_adding_a_contact_with_neither_phone_nor_username_sends_nothing(_wire):
    client = _wire(_Client())

    result = await add_contact(first_name="Ada", account="a")

    assert "Either phone or username" in result
    assert client.requests == [], "a contact with no identifier was sent anyway"


# --- deleting and blocking ---------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_contact_sends_the_resolved_user(_wire):
    """The request must carry the resolved entity, not the argument it came from."""
    user = _user(4242)
    client = _wire(_Client(), entity=user)

    await delete_contact("@someone", account="a")

    assert client.names == ["DeleteContactsRequest"]
    assert client.sent("DeleteContactsRequest").id == [user]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, request_name", [(block_user, "BlockRequest"), (unblock_user, "UnblockRequest")]
)
async def test_blocking_and_unblocking_send_the_resolved_user(_wire, tool, request_name):
    user = _user(4242)
    client = _wire(_Client(), entity=user)

    await tool("@someone", account="a")

    assert client.names == [request_name]
    assert client.sent(request_name).id == user


# --- importing in bulk -------------------------------------------------------


# Was xfail(strict=True) when this test was written: contacts.py built
# functions.contacts.InputPhoneContact, which does not exist, so every import_contacts
# call raised AttributeError and returned an error code instead of importing anybody.
# Fixed to types.InputPhoneContact; the test is a regression guard now.
@pytest.mark.asyncio
async def test_import_contacts_builds_one_input_phone_contact_per_row(_wire):
    """Each row needs its own client_id: Telegram keys the import result by it, so
    duplicate ids would collapse two people into one."""
    client = _wire(_Client(imported=["a", "b"]))

    await import_contacts(
        [
            {"phone": "+15550100", "first_name": "Ada", "last_name": "Lovelace"},
            {"phone": "+15550101", "first_name": "Alan", "last_name": "Turing"},
        ],
        account="a",
    )

    assert client.names == ["ImportContactsRequest"]
    sent = client.sent("ImportContactsRequest").contacts
    assert [type(c).__name__ for c in sent] == ["InputPhoneContact", "InputPhoneContact"]
    assert [c.phone for c in sent] == ["+15550100", "+15550101"]
    assert len({c.client_id for c in sent}) == 2, "two rows shared one client_id"
