"""Listing who reacted to a message, one page at a time.

`messages.getMessageReactionsList` is a cursor API: the answer carries a
`next_offset` and the caller is expected to hand it back. The tool used to drop
both ends of that cursor, so page two was unreachable, and it read the reactor
out of `peer_id.user_id` only -- a channel or a group reacting came back as a
null id with no way to tell which one it was.

No network: a fake client records the TL request it was handed.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel, PeerChat, PeerUser, ReactionEmoji

from telegram_mcp.tools import messages_state as mod


class _Client:
    def __init__(self, answer):
        self.requests = []
        self.answer = answer

    async def __call__(self, request):
        self.requests.append(request)
        return self.answer

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _reaction(peer, emoticon="👍"):
    return SimpleNamespace(
        peer_id=peer,
        reaction=ReactionEmoji(emoticon=emoticon),
        date=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def _answer(reactions, count=None, next_offset=None):
    return SimpleNamespace(
        reactions=reactions,
        count=len(reactions) if count is None else count,
        next_offset=next_offset,
    )


@pytest.fixture
def _wire(monkeypatch):
    def wire(answer):
        client = _Client(answer)
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(mod, "resolve_input_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_next_page_cursor_comes_back_and_can_be_handed_in_again(_wire):
    """Without an offset input the reported `next_offset` names a page nobody
    can ask for."""
    client = _wire(_answer([_reaction(PeerUser(user_id=50))], count=97, next_offset="page2"))

    payload = json.loads(await mod.get_message_reactions(1, 11, offset="page2", account="a"))

    assert client.sent("GetMessageReactionsListRequest").offset == "page2"
    assert payload["next_offset"] == "page2"
    # The server's own total, not the length of this page.
    assert payload["count"] == 97
    assert payload["returned"] == 1


@pytest.mark.asyncio
async def test_a_channel_or_group_reactor_keeps_its_identity(_wire):
    """`peer_id.user_id` is absent for a PeerChannel/PeerChat, so the reactor used
    to be reported as `user_id: null` with nothing to distinguish the two."""
    client = _wire(
        _answer(
            [
                _reaction(PeerUser(user_id=50)),
                _reaction(PeerChannel(channel_id=777)),
                _reaction(PeerChat(chat_id=88)),
            ]
        )
    )
    assert client is not None

    payload = json.loads(await mod.get_message_reactions(1, 11, account="a"))
    kinds = [(r["reactor_kind"], r["reactor_id"]) for r in payload["reactions"]]

    assert kinds == [("user", 50), ("channel", -1000000000777), ("chat", -88)]


@pytest.mark.asyncio
async def test_an_empty_page_is_not_reported_as_no_reactions_at_all(_wire):
    """Page two of an exhausted cursor is empty; that is the end of the list, not
    a message nobody reacted to."""
    _wire(_answer([], count=97, next_offset=None))

    payload = json.loads(await mod.get_message_reactions(1, 11, offset="tail", account="a"))

    assert payload["returned"] == 0
    assert payload["next_offset"] is None
