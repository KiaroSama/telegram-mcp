"""Creating a poll, and the three constraints Telegram puts on one.

Split from ``test_polls.py``, which had grown past 850 lines. That file is about
an existing poll - the index-to-option-bytes mapping, reading results, voting,
and listing voters. This one is about making one, where every interesting case
is a REFUSAL:

* a quiz has to carry its correct answer, and the answer is an option INDEX that
  has to survive being turned into the option bytes Telegram wants;
* ``close_date`` lives inside a window rather than merely in the future, and it
  has to still be legal when the request LANDS - which is why one of these tests
  makes entity resolution slow on purpose;
* the option count has a ceiling that comes from the server's own config rather
  than from a constant here.

Nothing is shared with the reading half: these tests bring their own client and
their own fixtures, which is why the split needed no helper module.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import messages_state

# A fixed clock. close_date is validated against a WINDOW, so these tests
# need a now() that cannot drift between the check and the assertion.
_FROZEN = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def _wire_state(monkeypatch):
    """create_poll on a fake client that keeps the requests it was handed."""
    from telegram_mcp.tools import messages_state

    class _StateClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=4242))])

        def sent(self, name):
            return next((r for r in self.requests if type(r).__name__ == name), None)

    client = _StateClient()
    monkeypatch.setattr(messages_state, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(messages_state, "ensure_connected", _ensure, raising=False)
    monkeypatch.setattr(messages_state, "resolve_entity", _resolve)
    return client


@pytest.mark.asyncio
async def test_a_quiz_carries_the_correct_answer_it_was_given(_wire_state):
    """`InputMediaPoll.correct_answers=None` on a `quiz=True` poll marks nothing
    correct, so Telegram grades every answer wrong for every voter."""
    from telegram_mcp.tools import messages_state

    await messages_state.create_poll(
        "me", "2+2?", ["3", "4", "5"], quiz_mode=True, correct_option_index=1, account="a"
    )

    media = _wire_state.sent("SendMediaRequest").media
    assert media.poll.quiz is True
    assert media.correct_answers, "quiz sent with no correct answer at all"
    assert list(media.correct_answers) == [1]


@pytest.mark.asyncio
async def test_a_quiz_without_a_correct_answer_is_refused_before_sending(_wire_state):
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "2+2?", ["3", "4"], quiz_mode=True, account="a"
    )

    assert "correct_option_index" in result
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_a_quiz_correct_index_outside_the_options_is_refused(_wire_state):
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "2+2?", ["3", "4"], quiz_mode=True, correct_option_index=7, account="a"
    )

    assert "0-1" in result
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_a_quiz_cannot_also_be_multiple_choice(_wire_state):
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me",
        "2+2?",
        ["3", "4"],
        quiz_mode=True,
        multiple_choice=True,
        correct_option_index=0,
        account="a",
    )

    assert "multiple" in result.lower()
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_a_correct_index_without_quiz_mode_is_refused(_wire_state):
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "2+2?", ["3", "4"], correct_option_index=0, account="a"
    )

    assert "quiz_mode" in result
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_an_empty_question_or_option_is_refused(_wire_state):
    from telegram_mcp.tools import messages_state

    assert "question" in (await messages_state.create_poll("me", "  ", ["a", "b"], account="a"))
    assert "option" in (await messages_state.create_poll("me", "q?", ["a", " "], account="a"))
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_poll_text_stays_out_of_the_error_report(_wire_state, monkeypatch):
    """The question and its options are user content; an error report is not a
    place to copy them into."""
    from telegram_mcp.tools import messages_state

    seen = {}

    def _fake(name, error, **kwargs):
        seen.update(kwargs)
        return "boom"

    monkeypatch.setattr(messages_state, "log_and_format_error", _fake)

    async def _explode(_chat_id, _client):
        raise RuntimeError("nope")

    monkeypatch.setattr(messages_state, "resolve_entity", _explode)

    await messages_state.create_poll("me", "secret question", ["secret option"], account="a")

    assert "question" not in seen and "options" not in seen


@pytest.fixture
def _in(monkeypatch):
    """An ISO close_date N seconds from a frozen `now`, as a caller writes one.

    Frozen because the boundaries are exact: measured against the wall clock, a
    date built as "now + 5s" is a few microseconds under five seconds by the time
    the tool subtracts, and the boundary case would fail for a reason that has
    nothing to do with the boundary.
    """
    from telegram_mcp.tools import messages_state

    class _Clock(datetime):
        # Frozen, but movable on purpose: `_slow_resolve` charges the round trip
        # that resolving a chat costs, which is the whole point of rechecking the
        # deadline after it rather than before.
        offset = timedelta(0)

        @classmethod
        def advance(cls, seconds):
            cls.offset += timedelta(seconds=seconds)

        @classmethod
        def now(cls, tz=None):
            moment = _FROZEN + cls.offset
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(messages_state, "datetime", _Clock)
    return lambda seconds: (_FROZEN + timedelta(seconds=seconds)).isoformat()


@pytest.mark.asyncio
async def test_a_hundred_day_close_date_is_refused_before_the_poll_is_sent(_wire_state, _in):
    """Only "is it in the future" was checked, so a deadline three months out
    travelled all the way to the RPC to be refused there. Telegram's window ends
    at 2,628,000 seconds."""
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(100 * 86400), account="a"
    )

    assert "close_date" in result
    assert "2628000" in result.replace(",", "")
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [1, 4, 2628001])
async def test_a_close_date_outside_telegrams_window_is_refused(_wire_state, _in, seconds):
    """The window starts at 5 seconds and ends at 2,628,000. Either side of it is
    a round trip spent learning a documented limit."""
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(seconds), account="a"
    )

    assert "close_date" in result
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_a_close_date_in_the_past_still_says_so_plainly(_wire_state, _in):
    from telegram_mcp.tools import messages_state

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(-60), account="a"
    )

    assert "past" in result
    assert _wire_state.sent("SendMediaRequest") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [messages_state.EARLIEST_POLL_CLOSE_SECONDS, 2628000])
async def test_the_ends_of_the_close_window_are_accepted(_wire_state, _in, seconds):
    """Both boundaries are legal values, and an off-by-one here refuses a poll
    Telegram would have taken. The near end is Telegram's 5-second floor plus the
    slack the request needs to still be inside it when it lands."""
    from telegram_mcp.tools import messages_state

    await messages_state.create_poll("me", "q?", ["a", "b"], close_date=_in(seconds), account="a")

    assert _wire_state.sent("SendMediaRequest") is not None


@pytest.fixture
def _slow_resolve(monkeypatch, _wire_state, _in):
    """Charge `resolve_entity` a number of seconds on the frozen clock.

    Resolving a chat is a round trip. `_in` freezes time, so without this the
    clock the tool reads before resolving is the clock it reads after, and the
    whole class of "legal when parsed, expired when sent" is untestable.
    """
    from telegram_mcp.tools import messages_state

    def charge(seconds):
        async def _resolve(chat_id, _client):
            messages_state.datetime.advance(seconds)
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(messages_state, "resolve_entity", _resolve)

    return charge


@pytest.mark.asyncio
async def test_a_deadline_spent_resolving_the_chat_is_refused_without_sending(
    _wire_state, _in, _slow_resolve
):
    """The gap this closes: close_date was checked BEFORE resolve_entity, and
    resolving a chat costs a round trip. A deadline that was comfortably inside
    Telegram's window when parsed could be under the five-second floor by the time
    SendMediaRequest went out, and Telegram refused the poll after the send."""
    from telegram_mcp.tools import messages_state

    _slow_resolve(20)

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(15), account="a"
    )

    assert _wire_state.sent("SendMediaRequest") is None, "sent a poll whose deadline had expired"
    assert "close_date" in result
    assert "later" in result


@pytest.mark.asyncio
async def test_a_deadline_left_below_the_send_margin_is_refused_without_sending(
    _wire_state, _in, _slow_resolve
):
    """Still in the future, but too close to survive serialising and the wire —
    which is the same refusal, arriving from Telegram instead, one send later."""
    from telegram_mcp.tools import messages_state

    _slow_resolve(60 - messages_state.EARLIEST_POLL_CLOSE_SECONDS + 1)

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(60), account="a"
    )

    assert _wire_state.sent("SendMediaRequest") is None
    assert "close_date" in result


@pytest.mark.asyncio
async def test_a_deadline_with_room_to_spare_survives_the_resolution_delay(
    _wire_state, _in, _slow_resolve
):
    """The recheck must not turn a normal round trip into a refusal."""
    from telegram_mcp.tools import messages_state

    _slow_resolve(3)

    await messages_state.create_poll("me", "q?", ["a", "b"], close_date=_in(600), account="a")

    request = _wire_state.sent("SendMediaRequest")
    assert request is not None
    assert request.media.poll.close_date is not None


@pytest.mark.asyncio
async def test_telegrams_bare_five_second_floor_is_not_enough_to_send_from(_wire_state, _in):
    """5 s is what Telegram documents, measured on ITS clock when the request
    lands. Accepting it here means sending something guaranteed to arrive late."""
    from telegram_mcp.tools import messages_state

    assert messages_state.EARLIEST_POLL_CLOSE_SECONDS > 5

    result = await messages_state.create_poll(
        "me", "q?", ["a", "b"], close_date=_in(5), account="a"
    )

    assert _wire_state.sent("SendMediaRequest") is None
    assert "close_date" in result


@pytest.mark.asyncio
async def test_twelve_options_are_accepted_because_telegram_accepts_twelve(_wire_state):
    """`poll_answers_max` is 12 in Telegram's published client config; the tool
    refused anything past a hard-coded 10 and blamed the caller for it."""
    from telegram_mcp.tools import messages_state

    messages_state._poll_answers_max_cache.clear()
    options = [f"option {n}" for n in range(12)]

    await messages_state.create_poll("me", "q?", options, account="a")

    media = _wire_state.sent("SendMediaRequest")
    assert media is not None, "twelve options were refused locally"
    assert len(media.media.poll.answers) == 12


class _ConfigClient:
    """A client that answers help.getAppConfig with a real AppConfig, or blows up."""

    def __init__(self, answers_max=None, explode=False):
        self.requests = []
        self.answers_max = answers_max
        self.explode = explode

    async def __call__(self, request):
        self.requests.append(request)
        if type(request).__name__ == "GetAppConfigRequest":
            if self.explode:
                raise RuntimeError("no config today")
            from telethon.tl.types import JsonNumber, JsonObject, JsonObjectValue
            from telethon.tl.types.help import AppConfig

            return AppConfig(
                hash=1,
                config=JsonObject(
                    value=[
                        JsonObjectValue(
                            key="poll_answers_max",
                            value=JsonNumber(value=float(self.answers_max)),
                        )
                    ]
                ),
            )
        return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=4242))])

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire_config(monkeypatch):
    from telegram_mcp.tools import messages_state

    def use(client):
        messages_state._poll_answers_max_cache.clear()
        monkeypatch.setattr(messages_state, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(messages_state, "ensure_connected", _ensure, raising=False)
        monkeypatch.setattr(messages_state, "resolve_entity", _resolve)
        return client

    return use


@pytest.mark.asyncio
async def test_the_option_ceiling_comes_from_telegrams_own_config(_wire_config):
    """A number written into the source goes stale the next time Telegram moves
    it -- which is exactly how the tool came to refuse 11 and 12. The limit is
    read from the config Telegram publishes for the purpose."""
    from telegram_mcp.tools import messages_state

    client = _wire_config(_ConfigClient(answers_max=6))

    result = await messages_state.create_poll(
        "me", "q?", [f"o{n}" for n in range(7)], account="cfg"
    )

    assert "6 options" in result
    assert client.sent("GetAppConfigRequest") is not None, "the published limit was never read"
    assert client.sent("SendMediaRequest") is None


@pytest.mark.asyncio
async def test_the_published_limit_is_read_once_per_account(_wire_config):
    """A config lookup on every poll is a round trip bought for nothing."""
    from telegram_mcp.tools import messages_state

    client = _wire_config(_ConfigClient(answers_max=12))

    await messages_state.create_poll("me", "q?", ["a", "b"], account="cfg")
    await messages_state.create_poll("me", "q?", ["a", "b"], account="cfg")

    lookups = [r for r in client.requests if type(r).__name__ == "GetAppConfigRequest"]
    assert len(lookups) == 1


@pytest.mark.asyncio
async def test_an_unreadable_config_falls_back_to_the_documented_current_limit(_wire_config):
    """A config lookup that fails must not block poll creation, and must not
    silently allow more than Telegram takes."""
    from telegram_mcp.tools import messages_state

    _wire_config(_ConfigClient(explode=True))

    ok = await messages_state.create_poll("me", "q?", [f"o{n}" for n in range(12)], account="z")
    too_many = await messages_state.create_poll(
        "me", "q?", [f"o{n}" for n in range(13)], account="z"
    )

    assert "created" in ok
    assert "12 options" in too_many
