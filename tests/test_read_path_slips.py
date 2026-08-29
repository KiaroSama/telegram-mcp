"""Four small slips in the paths that report facts back to the calling model.

Each one makes the server state something untrue, and an agent has no way to tell:

* a deleted account is reported as a chat literally named ``None``;
* the ``/c/`` permalink truncates a channel id past 10^10 into a different,
  valid-looking one, so the link points at another chat;
* the Saved-Messages bucket lookup keys users and chats into one dict by bare
  id, and those id spaces overlap;
* the poll-option cache keys on the raw account string while ``get_client``
  ignores it in single-account mode, so one client gets two caches.

No network anywhere: fakes record what they were handed.
"""

import datetime
import json
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, ChatPhotoEmpty, PeerChannel, PeerUser, User

from telegram_mcp import message_view
from telegram_mcp.tools import chats, messages_state, saved


def _parse(result):
    return json.loads(result.split("\n\n")[0])


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


# --- A: a deleted account has no name, and "None" is not one ----------------


class _QuietClient:
    """Answers get_chat's dialog lookup with nothing to report."""

    async def get_input_entity(self, entity):
        return object()

    async def __call__(self, request):
        return SimpleNamespace(dialogs=[])

    async def get_messages(self, entity, limit=1):
        return []


@pytest.mark.asyncio
async def test_a_deleted_account_is_not_reported_as_a_chat_named_none(monkeypatch):
    """`f"{entity.first_name}"` turns a missing name into the four-character
    string "None" before sanitize_name can ever see it, and an agent then quotes
    that to the user as a real name."""
    entity = User(
        id=4242, first_name=None, last_name=None, username=None, phone=None, deleted=True
    )
    monkeypatch.setattr(chats, "get_client", lambda account=None: _QuietClient())
    monkeypatch.setattr(chats, "resolve_entity", _async_return(entity))

    payload = _parse(await chats.get_chat(chat_id=4242, account=None))

    assert payload["name"] == "[empty]"


# --- B: the /c/ permalink must not truncate --------------------------------


def _channel(chat_id):
    return SimpleNamespace(id=chat_id, username=None, broadcast=True, megagroup=False)


@pytest.mark.parametrize(
    ("chat_id", "expected"),
    [
        # A bare entity id past 10^10 -- what `msg.chat.id` carries.
        (12345678901, "12345678901"),
        # The marked form, which is what the forward path carries.
        (-10012345678901, "12345678901"),
        # A bare id that merely begins with 100 is not a marked id.
        (1001234, "1001234"),
    ],
)
def test_the_permalink_carries_the_whole_channel_id(chat_id, expected):
    """`abs(chat_id) % 10**10` stripped the -100 marker only while the bare id
    stayed under 10^10; past that it yields a different, valid-looking id."""
    link = message_view.message_permalink(SimpleNamespace(id=55), chat=_channel(chat_id))

    assert link == f"https://t.me/c/{expected}/55"


# --- C: user and chat ids overlap, so a bare id is not a key ----------------


class _SavedClient:
    def __init__(self, answer):
        self.answer = answer

    async def __call__(self, request):
        return self.answer


@pytest.mark.asyncio
async def test_a_channel_sharing_a_users_id_does_not_steal_its_bucket(monkeypatch):
    """`chats` is iterated second, so on a collision it overwrote the user and
    the person's bucket came back wearing the channel's title."""
    answer = SimpleNamespace(
        dialogs=[
            SimpleNamespace(peer=PeerUser(user_id=777), top_message=9, pinned=False),
            SimpleNamespace(peer=PeerChannel(channel_id=555), top_message=3, pinned=False),
        ],
        users=[User(id=777, first_name="Nadia", last_name=None, username="nadia", phone=None)],
        chats=[
            Channel(
                id=777,
                title="Announcements",
                photo=ChatPhotoEmpty(),
                date=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
                broadcast=True,
            ),
            Channel(
                id=555,
                title="Release Notes",
                photo=ChatPhotoEmpty(),
                date=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
                broadcast=True,
            ),
        ],
    )
    monkeypatch.setattr(saved, "get_client", lambda account=None: _SavedClient(answer))
    monkeypatch.setattr(saved, "ensure_connected", _async_return(None))

    records = _parse(await saved.list_saved_dialogs(account="a"))["results"]

    assert records[0]["name"] == "Nadia"
    # The reported id has to be feedable into the other tools, and only a marked
    # one identifies which of the three id spaces it came from.
    assert records[1]["peer_id"] == -1000000000555


# --- D: one client must not get two caches ---------------------------------


class _ConfigClient:
    def __init__(self):
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        return SimpleNamespace(
            config=SimpleNamespace(value=[SimpleNamespace(key="poll_answers_max", value=None)])
        )


@pytest.mark.asyncio
async def test_the_poll_ceiling_is_cached_once_per_client_not_per_spelling(monkeypatch):
    """`get_client` ignores the account argument in single-account mode, so None
    and the real label reach one client -- and paid for two GetAppConfig round
    trips because the cache keyed on the raw string."""
    from telegram_mcp import runtime

    monkeypatch.setattr(runtime, "clients", {"solo": object()})
    monkeypatch.setattr(messages_state, "ensure_connected", _async_return(None))
    monkeypatch.setattr(messages_state, "_poll_answers_max_cache", {})
    client = _ConfigClient()

    await messages_state._poll_answers_max(client, None)
    await messages_state._poll_answers_max(client, "SOLO")

    assert client.calls == 1, "one client asked Telegram the same question twice"
    assert list(messages_state._poll_answers_max_cache) == ["solo"]
