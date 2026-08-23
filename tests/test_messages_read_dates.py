"""list_messages' date filters: the parsed bounds, and the two error strings.

No network: the client is a fake that records the kwargs it was handed and yields
a fixed set of messages. This file exists because the date-parsing branch had no
coverage at all, which is what let two unreachable `except AttributeError` blocks
sit there claiming to be Python-version fallbacks.
"""

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import messages_read as mod


def _msg(day, text="hi"):
    # `reply_to` must exist: list_messages reads `msg.reply_to` directly rather
    # than through getattr, so a SimpleNamespace without it raises AttributeError.
    return SimpleNamespace(
        id=day,
        date=dt.datetime(2026, 5, day, 12, 0, tzinfo=dt.timezone.utc),
        message=text,
        sender=SimpleNamespace(first_name="Ada", last_name=None, title=None),
        reply_to=None,
    )


class _Client:
    """Yields a fixed history and records what it was asked for."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.iter_kwargs = []

    def iter_messages(self, entity, **kwargs):
        self.iter_kwargs.append(kwargs)
        messages = self.messages

        class _Iter:
            def __aiter__(self):
                self._rest = list(messages)
                return self

            async def __anext__(self):
                if not self._rest:
                    raise StopAsyncIteration
                return self._rest.pop(0)

        return _Iter()


@pytest.fixture
def _wire(monkeypatch):
    def wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id, title="Chat")

        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["from_date", "to_date"])
async def test_a_malformed_date_is_refused_by_name(_wire, field):
    _wire(_Client([]))

    result = await mod.list_messages(1, **{field: "05/01/2026"}, account="a")

    assert result == f"Invalid {field} format. Use YYYY-MM-DD."


@pytest.mark.asyncio
async def test_the_bounds_are_utc_aware_so_comparing_against_a_message_date_works(_wire):
    """Telegram message dates are timezone-aware. A naive bound would raise
    `TypeError: can't compare offset-naive and offset-aware datetimes` the moment it
    met one, so both bounds must carry UTC."""
    # Newest -> oldest: the search branch documents and relies on that order, and
    # breaks out as soon as it drops below from_date.
    client = _wire(_Client([_msg(20), _msg(10), _msg(1)]))

    result = await mod.list_messages(
        1, search_query="hi", from_date="2026-05-05", to_date="2026-05-15", account="a"
    )

    # list_messages renders through format_tool_result, i.e. JSON - not the
    # "ID: <n>" line format that format_message_line produces for other tools.
    # A naive bound surfaces here as an error string instead of JSON.
    assert result.startswith("{"), f"expected JSON, got: {result}"
    ids = [record["id"] for record in json.loads(result)["results"]]

    # Only the 10th falls inside [2026-05-05T00:00Z, 2026-05-15T23:59:59.999999Z]
    assert ids == [10]
    assert client.iter_kwargs == [{"search": "hi"}]
