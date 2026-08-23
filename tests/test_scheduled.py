"""The scheduled queue: reading it back, editing it, and the repeat period.

No network: the client is a fake that records the requests it was handed, which
is what the assertions are about — the tools' job is to build the right TL
request and to describe what comes back.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import scheduled as mod
from telegram_mcp.tools.scheduled import (
    REPEAT_PERIODS,
    _as_utc,
    _describe,
    _repeat_seconds,
    cancel_scheduled_message,
    edit_scheduled_message,
    list_scheduled_messages,
    schedule_message,
)

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

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _queued(message_id=7, text="ping", period=None, when=None):
    return SimpleNamespace(
        id=message_id,
        message=text,
        date=when or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        schedule_repeat_period=period,
        media=None,
    )


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


# --- the input rules --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01T14:30:00Z", datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)),
        ("2026-09-01T14:30:00", datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)),
        (1788000000, datetime.fromtimestamp(1788000000, tz=timezone.utc)),
    ],
)
def test_a_naive_datetime_is_read_as_utc_like_upstream(value, expected):
    assert _as_utc(value) == expected


@pytest.mark.parametrize(
    ("repeat", "expected"), [("daily", 86400), ("weekly", 604800), ("WEEKLY", 604800)]
)
def test_the_repeat_periods_are_the_values_telegram_accepted(repeat, expected):
    """Probed live: these two pass server validation, 5 seconds does not."""
    assert _repeat_seconds(repeat) == expected


@pytest.mark.parametrize("repeat", [None, "", "none", "off"])
def test_no_repeat_means_none_not_zero(repeat):
    """Zero is a period Telegram would reject; absence must stay absence."""
    assert _repeat_seconds(repeat) is None


def test_an_unknown_repeat_names_the_valid_set_rather_than_reaching_telegram():
    message = _repeat_seconds("hourly")
    assert isinstance(message, str)
    assert "daily" in message and "weekly" in message
    assert "SCHEDULE_REPEAT_PERIOD_INVALID" in message


def test_a_queued_message_reports_its_repeat_by_name():
    described = _describe(_queued(period=REPEAT_PERIODS["weekly"]))
    assert described["repeat"] == "weekly"
    assert described["repeat_seconds"] == 604800


def test_a_repeat_period_telegram_invents_later_is_reported_as_custom():
    """The names are ours; an unrecognised period must not be dropped silently."""
    described = _describe(_queued(period=999))
    assert described["repeat"] == "custom"
    assert described["repeat_seconds"] == 999


# --- the tools --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_queue_says_so_rather_than_returning_an_empty_list(_wire):
    _wire(_Client(queued=[]))
    assert "no scheduled messages" in await list_scheduled_messages(1, account="a")


@pytest.mark.asyncio
async def test_listing_reports_each_queued_message(_wire):
    _wire(_Client(queued=[_queued(7, "one"), _queued(8, "two", period=86400)]))

    payload = json.loads(await list_scheduled_messages(1, account="a"))

    assert payload["scheduled_count"] == 2
    assert [r["message_id"] for r in payload["results"]] == [7, 8]
    assert payload["results"][1]["repeat"] == "daily"


@pytest.mark.asyncio
async def test_scheduling_sends_the_period_telegram_expects(_wire):
    client = _wire(_Client())

    payload = json.loads(await schedule_message(1, "hi", SOON, repeat="daily", account="a"))

    request = client.sent("SendMessageRequest")
    assert request.schedule_repeat_period == 86400
    assert (
        request.schedule_date.tzinfo is not None
    ), "a naive schedule_date is ambiguous on the wire"
    assert payload["results"][0]["repeat_seconds"] == 86400


@pytest.mark.asyncio
async def test_a_past_time_is_refused_before_any_request(_wire):
    client = _wire(_Client())
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    result = await schedule_message(1, "hi", past, account="a")

    assert "must be in the future" in result
    assert client.requests == [], "a doomed request was sent anyway"


@pytest.mark.asyncio
async def test_editing_keeps_the_existing_time_when_none_is_given(_wire):
    """Editing resends schedule_date, so omitting it must not reschedule to now."""
    existing = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    client = _wire(_Client(queued=[_queued(7, "old", when=existing)]))

    await edit_scheduled_message(1, 7, message="new", account="a")

    request = client.sent("EditMessageRequest")
    assert request.schedule_date == existing
    assert request.message == "new"


@pytest.mark.asyncio
async def test_editing_keeps_the_existing_text_when_none_is_given(_wire):
    client = _wire(_Client(queued=[_queued(7, "keep me")]))

    await edit_scheduled_message(1, 7, when=SOON, account="a")

    assert client.sent("EditMessageRequest").message == "keep me"


@pytest.mark.asyncio
async def test_repeat_off_clears_the_period_rather_than_keeping_it(_wire):
    client = _wire(_Client(queued=[_queued(7, "x", period=86400)]))

    await edit_scheduled_message(1, 7, repeat="off", account="a")

    assert client.sent("EditMessageRequest").schedule_repeat_period is None


@pytest.mark.asyncio
async def test_editing_an_id_that_is_not_queued_says_where_ids_come_from(_wire):
    _wire(_Client(queued=[_queued(7)]))

    result = await edit_scheduled_message(1, 999, message="x", account="a")

    assert "not in chat" in result and "list_scheduled_messages" in result


@pytest.mark.asyncio
async def test_cancelling_deletes_from_the_scheduled_queue(_wire):
    client = _wire(_Client(queued=[_queued(7)]))

    result = await cancel_scheduled_message(1, 7, account="a")

    request = client.sent("DeleteScheduledMessagesRequest")
    assert request.id == [7]
    assert "removed from" in result


@pytest.mark.asyncio
async def test_cancelling_a_batch_uses_one_request(_wire):
    client = _wire(_Client())

    await cancel_scheduled_message(1, [7, 8, 9], account="a")

    assert [type(r).__name__ for r in client.requests] == ["DeleteScheduledMessagesRequest"]
    assert client.sent("DeleteScheduledMessagesRequest").id == [7, 8, 9]


@pytest.mark.asyncio
async def test_cancelling_nothing_never_reaches_the_server(_wire):
    client = _wire(_Client())

    result = await cancel_scheduled_message(1, [], account="a")

    assert "empty" in result
    assert client.requests == []
