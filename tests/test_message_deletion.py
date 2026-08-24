"""Deleting messages: saying what was actually deleted, and for whom.

`messages.deleteHistory` answers with `messages.affectedHistory`, whose `offset`
is a continuation signal: a positive value means the method has to be called
again with the same parameters until it reaches zero. One call and a "history
cleared" report is therefore a claim the server never made.

The loop that repeats it is bounded three ways -- an iteration ceiling, a wall
deadline, and a progress check -- because a server that keeps answering with the
same offset would otherwise spin forever.

No network: a fake client records the TL requests it was handed.
"""

import time
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import messages as mod


class _Client:
    """Answers DeleteHistoryRequest with a scripted sequence of offsets."""

    def __init__(self, offsets=(0,), pts_counts=None, delay=0.0):
        self.requests = []
        self.offsets = list(offsets)
        self.pts_counts = list(pts_counts or [len(offsets) and 5] * len(self.offsets))
        self.delay = delay
        self.deleted = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        index = min(len(self.requests) - 1, len(self.offsets) - 1)
        return SimpleNamespace(offset=self.offsets[index], pts_count=self.pts_counts[index])

    async def delete_messages(self, entity, message_ids, revoke=True):
        self.deleted.append((entity, message_ids, revoke))
        return [SimpleNamespace(pts_count=1)]


@pytest.fixture
def _wire(monkeypatch):
    def wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_a_positive_offset_is_repeated_until_the_server_says_zero(_wire):
    """A single call with offset=50 left most of the history in place and still
    reported it cleared."""
    client = _wire(_Client(offsets=(50, 10, 0), pts_counts=(50, 10, 3)))

    result = await mod.delete_chat_history(1, account="a")

    assert len(client.requests) == 3, "the continuation offset was ignored"
    assert "63" in result, "the per-call counts were not aggregated"
    assert "cleared" in result


@pytest.mark.asyncio
async def test_an_offset_that_stops_shrinking_aborts_instead_of_spinning(_wire):
    """A server that answers with the same offset forever must not become an
    infinite loop inside a tool call."""
    client = _wire(_Client(offsets=(50, 50, 50, 50), pts_counts=(1, 1, 1, 1)))

    result = await mod.delete_chat_history(1, account="a")

    assert len(client.requests) == 2, "no progress check; it kept asking"
    assert "incomplete" in result.lower()
    assert "50" in result


@pytest.mark.asyncio
async def test_the_loop_gives_up_at_its_iteration_ceiling(_wire, monkeypatch):
    """Steady progress that never reaches zero is still bounded."""
    monkeypatch.setattr(mod, "_DELETE_HISTORY_MAX_PASSES", 4)
    client = _wire(_Client(offsets=(100, 90, 80, 70, 60), pts_counts=(1, 1, 1, 1, 1)))

    result = await mod.delete_chat_history(1, account="a")

    assert len(client.requests) == 4
    assert "incomplete" in result.lower()


@pytest.mark.asyncio
async def test_the_loop_gives_up_at_its_wall_deadline(_wire, monkeypatch):
    """Progress that is real but far too slow is bounded by time, not only by
    pass count."""
    # A sleep here models the RPC the deadline exists for; `sleep` guarantees a
    # lower bound, so one pass always overshoots a 10ms budget.
    monkeypatch.setattr(mod, "_DELETE_HISTORY_DEADLINE_SECONDS", 0.01)
    client = _wire(_Client(offsets=(100, 90, 80, 70, 0), pts_counts=(1, 1, 1, 1, 1), delay=0.02))

    result = await mod.delete_chat_history(1, account="a")

    assert len(client.requests) < 5
    assert "incomplete" in result.lower()


@pytest.mark.asyncio
async def test_deleting_one_message_says_whether_it_was_revoked(_wire):
    """`revoke` defaulted to Telethon's True with no parameter and no mention of
    it, so a single delete reached the other party silently."""
    client = _wire(_Client())

    both = await mod.delete_message(1, 5, account="a")
    assert client.deleted[-1][2] is True
    assert "both" in both

    mine = await mod.delete_message(1, 5, revoke=False, account="a")
    assert client.deleted[-1][2] is False
    assert "both" not in mine
