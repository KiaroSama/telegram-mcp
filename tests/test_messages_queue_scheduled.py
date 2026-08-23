"""The three older scheduled tools, and the one implementation behind them.

No network: the client is a fake that records the TL requests it was handed. These
three had no tests at all, which is how they drifted away from their newer
counterparts in scheduled.py — only the newer ones supported Telegram's repeat
period, and only the newer schedule_message returned the ID that makes a queued
message findable afterwards.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import messages_queue as mod

SOON = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, queued=()):
        self.requests = []
        self.queued = list(queued)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetScheduledHistoryRequest":
            return SimpleNamespace(messages=self.queued)
        if name == "SendMessageRequest":
            return SimpleNamespace(updates=[SimpleNamespace(id=4242)])
        return SimpleNamespace(updates=[])

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _queued(message_id=7, text="ping", when=None):
    return SimpleNamespace(
        id=message_id,
        message=text,
        date=when or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        schedule_repeat_period=None,
        media=None,
    )


@pytest.fixture
def _wire(monkeypatch):
    """Patch BOTH modules: the wrapper lives in messages_queue, the implementation
    it calls lives in scheduled, and each reads its own module globals."""

    def wire(client):
        from telegram_mcp.tools import scheduled as impl

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        for module in (mod, impl):
            monkeypatch.setattr(module, "get_client", lambda account=None: client)
            monkeypatch.setattr(module, "ensure_connected", _ensure)
            monkeypatch.setattr(module, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_old_send_tool_returns_the_id_it_queued(_wire):
    """Without the ID nothing can list, edit or cancel the message afterwards —
    the whole reason schedule_message was written."""
    client = _wire(_Client())

    payload = json.loads(await mod.send_scheduled_message(1, "hi", SOON, account="a"))

    assert payload["results"][0]["message_id"] == 4242
    assert payload["queued"] is True
    assert client.sent("SendMessageRequest") is not None


@pytest.mark.asyncio
async def test_the_old_send_tool_still_refuses_a_past_time(_wire):
    _wire(_Client())
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    result = await mod.send_scheduled_message(1, "hi", past, account="a")

    assert "must be in the future" in result


@pytest.mark.asyncio
async def test_the_old_list_tool_answers_in_the_project_json_shape(_wire):
    client = _wire(_Client(queued=[_queued(7, "ping"), _queued(8, "pong")]))

    payload = json.loads(await mod.get_scheduled_messages(1, account="a"))

    assert [r["message_id"] for r in payload["results"]] == [7, 8]
    assert payload["scheduled_count"] == 2
    assert client.names == ["GetScheduledHistoryRequest"]


@pytest.mark.asyncio
async def test_the_old_delete_tool_still_removes_a_batch_in_one_request(_wire):
    """delete_scheduled_message's whole advantage over cancel_scheduled_message was
    the batch. Delegating one-at-a-time would turn one round trip into N."""
    client = _wire(_Client())

    result = await mod.delete_scheduled_message(1, [7, 8, 9], account="a")

    assert client.names == ["DeleteScheduledMessagesRequest"]
    assert client.sent("DeleteScheduledMessagesRequest").id == [7, 8, 9]
    assert "7" in result and "8" in result and "9" in result


@pytest.mark.asyncio
async def test_the_old_delete_tool_still_rejects_an_empty_list(_wire):
    client = _wire(_Client())

    result = await mod.delete_scheduled_message(1, [], account="a")

    assert "empty" in result
    assert client.names == [], "an empty list must not reach the server"
