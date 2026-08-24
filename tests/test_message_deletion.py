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

import asyncio
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
    it, so a single delete reached the other party silently. Both scopes now say
    which one they used."""
    client = _wire(_Client())

    both = await mod.delete_message(1, 5, revoke=True, account="a")
    assert client.deleted[-1][2] is True
    assert "both" in both

    mine = await mod.delete_message(1, 5, revoke=False, account="a")
    assert client.deleted[-1][2] is False
    assert "both" not in mine


# --- the call itself has to be bounded, not only the loop around it ---------


class _HangingClient:
    """Answers the first few deletes, then never returns from the next one."""

    def __init__(self, answers=()):
        self.requests = []
        self.answers = list(answers)
        self.cancelled = False

    async def __call__(self, request):
        self.requests.append(request)
        if self.answers:
            offset, pts = self.answers.pop(0)
            return SimpleNamespace(offset=offset, pts_count=pts)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def delete_messages(self, entity, message_ids, revoke=True):
        return [SimpleNamespace(pts_count=1)]


@pytest.mark.asyncio
async def test_a_delete_that_never_returns_is_abandoned_at_the_deadline(_wire, monkeypatch):
    """The deadline was only consulted between calls, so one RPC that never came
    back outlived it entirely. That is not a budget the tool holds, it is a
    budget the server is free to opt out of."""
    monkeypatch.setattr(mod, "_DELETE_HISTORY_DEADLINE_SECONDS", 0.05)
    client = _wire(_HangingClient())

    started = time.monotonic()
    result = await asyncio.wait_for(mod.delete_chat_history(1, account="a"), timeout=5)
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"the hung call was never abandoned ({elapsed:.2f}s)"
    assert "incomplete" in result.lower()
    assert client.cancelled, "the abandoned request was left running in the background"


@pytest.mark.asyncio
async def test_progress_made_before_a_hung_call_is_reported_honestly(_wire, monkeypatch):
    """Giving up must not throw away what the earlier passes actually deleted."""
    monkeypatch.setattr(mod, "_DELETE_HISTORY_DEADLINE_SECONDS", 0.2)
    _wire(_HangingClient(answers=[(40, 12)]))

    result = await asyncio.wait_for(mod.delete_chat_history(1, account="a"), timeout=5)

    assert "12" in result, "the messages that were deleted went unreported"
    assert "incomplete" in result.lower()


@pytest.mark.asyncio
async def test_an_exhausted_budget_never_starts_another_delete(_wire, monkeypatch):
    """Zero remaining time is not enough time for one more call."""
    monkeypatch.setattr(mod, "_DELETE_HISTORY_DEADLINE_SECONDS", 0.0)
    client = _wire(_Client(offsets=(50, 0), pts_counts=(1, 1)))

    result = await mod.delete_chat_history(1, account="a")

    assert client.requests == [], "a delete was sent with no budget left to bound it"
    assert "incomplete" in result.lower()


# --- deleting for everyone is a choice, not a default ----------------------


@pytest.mark.asyncio
async def test_deleting_one_message_does_not_reach_the_other_party_by_default(_wire):
    """`revoke=True` by default made the least alarming-sounding call the most
    destructive one available: an agent asked to tidy its own view took the
    message out of the recipient's chat too, irreversibly."""
    client = _wire(_Client())

    mine = await mod.delete_message(1, 5, account="a")

    assert client.deleted[-1][2] is False, "the default still deleted for everyone"
    assert "both" not in mine
    assert "you only" in mine


@pytest.mark.asyncio
async def test_deleting_for_everyone_is_available_when_it_is_asked_for(_wire):
    client = _wire(_Client())

    both = await mod.delete_message(1, 5, revoke=True, account="a")

    assert client.deleted[-1][2] is True
    assert "both" in both
