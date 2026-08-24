"""Voting, tallies and voters — the part of a poll that upstream never had.

No network: the client is a fake that records the TL requests it was handed. The
assertions are mostly about those requests, because the one thing these tools do
that nothing else can check is turn a listed index into the opaque `option` bytes
Telegram identifies an answer by.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import polls as mod
from telegram_mcp.tools.polls import (
    _describe,
    _option_bytes,
    get_poll_results,
    get_poll_voters,
    vote_in_poll,
)

# Deliberately not 0, 1, 2: the wire blob is chosen by whoever made the poll and
# a test that used the index as the blob could not tell the mapping from a no-op.
BLOBS = [b"\xa0", b"\xb1", b"\xc2"]


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, message=None, votes=None, fresh=None):
        self.requests = []
        self.message = message
        self.votes = votes
        self.fresh = fresh

    async def get_messages(self, entity, ids=None):
        return self.message

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetPollVotesRequest":
            return self.votes
        if self.fresh is not None:
            return SimpleNamespace(updates=[SimpleNamespace(results=self.fresh)])
        return SimpleNamespace(updates=[])

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _poll(
    options=("yes", "no", "maybe"),
    closed=False,
    quiz=False,
    multiple_choice=False,
    public_voters=True,
):
    return SimpleNamespace(
        question=SimpleNamespace(text="lunch?"),
        answers=[
            SimpleNamespace(text=SimpleNamespace(text=text), option=BLOBS[i])
            for i, text in enumerate(options)
        ],
        closed=closed,
        quiz=quiz,
        multiple_choice=multiple_choice,
        public_voters=public_voters,
    )


def _results(counts=(3, 1, 0), chosen=(), correct=None, total=None, min=False, solution=None):
    return SimpleNamespace(
        min=min,
        total_voters=total if total is not None else sum(counts),
        solution=solution,
        results=[
            SimpleNamespace(
                option=BLOBS[i],
                voters=count,
                chosen=i in chosen,
                correct=(i == correct),
            )
            for i, count in enumerate(counts)
        ],
    )


def _message(poll=None, results=None, message_id=11):
    return SimpleNamespace(
        id=message_id,
        media=SimpleNamespace(poll=poll or _poll(), results=results or _results()),
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


# --- the index -> option-bytes mapping --------------------------------------


def test_an_index_maps_to_the_blob_the_poll_carries_not_to_the_index():
    poll = _poll()
    assert [_option_bytes(poll, i) for i in range(3)] == BLOBS


@pytest.mark.parametrize("index", [-1, 3, 99])
def test_an_index_outside_the_poll_has_no_blob(index):
    assert _option_bytes(_poll(), index) is None


def test_the_tally_is_read_per_option_by_blob_not_by_position():
    """`results.results` need not arrive in the poll's own order."""
    results = _results(counts=(3, 1, 0))
    results.results.reverse()

    described = _describe(_poll(), results)

    assert [o["voters"] for o in described["options"]] == [3, 1, 0]
    assert [o["text"] for o in described["options"]] == ["yes", "no", "maybe"]
    assert described["options"][0]["share_percent"] == 75.0


# --- reading results --------------------------------------------------------


@pytest.mark.asyncio
async def test_results_report_the_kind_of_poll_and_this_accounts_own_vote(_wire):
    _wire(_Client(message=_message(_poll(multiple_choice=True), _results(chosen=(0, 2)))))

    payload = json.loads(await get_poll_results(1, 11, account="a"))
    described = payload["results"][0]

    assert described["multiple_choice"] is True
    assert described["your_votes"] == [0, 2]
    assert payload["option_count"] == 3


@pytest.mark.asyncio
async def test_a_quiz_reports_the_correct_answer_once_telegram_reveals_it(_wire):
    _wire(
        _Client(
            message=_message(
                _poll(quiz=True), _results(chosen=(1,), correct=2, solution="Tuesday, always")
            )
        )
    )

    described = json.loads(await get_poll_results(1, 11, account="a"))["results"][0]

    assert described["quiz"] is True
    assert described["correct_option_index"] == 2
    assert described["options"][2]["correct"] is True
    assert described["quiz_explanation"] == "Tuesday, always"
    assert "correct_option_note" not in described


@pytest.mark.asyncio
async def test_an_unanswered_quiz_says_why_the_answer_is_missing(_wire):
    """Telegram withholds `correct` until the account answers; silence would read as 'none'."""
    _wire(_Client(message=_message(_poll(quiz=True), _results(correct=None))))

    described = json.loads(await get_poll_results(1, 11, account="a"))["results"][0]

    assert described["correct_option_index"] is None
    assert "until this account has answered" in described["correct_option_note"]


@pytest.mark.asyncio
async def test_minimal_results_are_refetched_rather_than_reported_as_no_vote(_wire):
    """`PollResults.min` strips `chosen`, so the stale copy would deny a real vote."""
    client = _wire(
        _Client(
            message=_message(results=_results(chosen=(), min=True)),
            fresh=_results(chosen=(1,)),
        )
    )

    described = json.loads(await get_poll_results(1, 11, account="a"))["results"][0]

    assert client.sent("GetPollResultsRequest").poll_hash == 0, "a cache hash can return nothing"
    assert described["your_votes"] == [1]


@pytest.mark.asyncio
async def test_a_message_without_a_poll_says_so(_wire):
    _wire(_Client(message=SimpleNamespace(id=11, media=None)))
    assert "carries no poll" in await get_poll_results(1, 11, account="a")


# --- voting -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_voting_sends_the_option_bytes_for_the_index_given(_wire):
    client = _wire(_Client(message=_message()))

    await vote_in_poll(1, 11, 2, account="a")

    request = client.sent("SendVoteRequest")
    assert request.options == [BLOBS[2]]
    assert request.msg_id == 11


@pytest.mark.asyncio
async def test_a_multiple_choice_poll_takes_every_index_in_one_request(_wire):
    client = _wire(
        _Client(message=_message(_poll(multiple_choice=True)), fresh=_results(chosen=(0, 2)))
    )

    payload = json.loads(await vote_in_poll(1, 11, [0, 2], account="a"))

    assert client.sent("SendVoteRequest").options == [BLOBS[0], BLOBS[2]]
    assert payload["voted_for"] == [0, 2]
    assert payload["results"][0]["your_votes"] == [0, 2], "the recount comes back with the vote"


@pytest.mark.asyncio
async def test_the_same_index_twice_is_one_vote_not_a_rejected_request(_wire):
    client = _wire(_Client(message=_message(_poll(multiple_choice=True))))

    await vote_in_poll(1, 11, [1, 1], account="a")

    assert client.sent("SendVoteRequest").options == [BLOBS[1]]


@pytest.mark.asyncio
async def test_an_out_of_range_index_is_refused_before_any_vote_is_sent(_wire):
    client = _wire(_Client(message=_message()))

    result = await vote_in_poll(1, 11, 7, account="a")

    assert "does not exist" in result and "0-2" in result
    assert client.sent("SendVoteRequest") is None, "a doomed vote was sent anyway"


@pytest.mark.asyncio
async def test_a_second_choice_on_a_single_choice_poll_is_refused(_wire):
    client = _wire(_Client(message=_message(_poll(multiple_choice=False))))

    result = await vote_in_poll(1, 11, [0, 1], account="a")

    assert "single-choice" in result
    assert client.sent("SendVoteRequest") is None


@pytest.mark.asyncio
async def test_a_closed_poll_is_refused_with_its_reason_not_an_rpc_error(_wire):
    client = _wire(_Client(message=_message(_poll(closed=True))))

    result = await vote_in_poll(1, 11, 0, account="a")

    assert "closed" in result and "get_poll_results" in result
    assert client.sent("SendVoteRequest") is None


@pytest.mark.asyncio
async def test_an_empty_list_retracts_by_sending_no_options(_wire):
    client = _wire(_Client(message=_message()))

    payload = json.loads(await vote_in_poll(1, 11, [], account="a"))

    assert client.sent("SendVoteRequest").options == []
    assert payload["retracted"] is True


# --- voters -----------------------------------------------------------------


def _votes_list():
    return SimpleNamespace(
        count=2,
        next_offset="page2",
        users=[SimpleNamespace(id=50, first_name="Ada", last_name="L")],
        chats=[SimpleNamespace(id=60, title="Team")],
        votes=[
            SimpleNamespace(
                peer=SimpleNamespace(user_id=50),
                options=[BLOBS[0], BLOBS[2]],
                date=datetime(2026, 8, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(peer=SimpleNamespace(channel_id=60), option=BLOBS[1], date=None),
        ],
    )


@pytest.mark.asyncio
async def test_voters_come_back_named_with_the_indexes_they_chose(_wire):
    client = _wire(_Client(message=_message(), votes=_votes_list()))

    payload = json.loads(await get_poll_voters(1, 11, account="a"))

    assert client.sent("GetPollVotesRequest").option is None, "no option means every voter"
    assert payload["voter_count"] == 2
    assert payload["next_offset"] == "page2"
    assert payload["results"][0] == {
        "voter_id": 50,
        "voter_name": "Ada L",
        "option_indexes": [0, 2],
        "date": "2026-08-01T00:00:00+00:00",
    }
    # A single-choice vote carries `option`, not `options`; both must map back.
    assert payload["results"][1]["option_indexes"] == [1]
    assert payload["results"][1]["voter_name"] == "Team"


@pytest.mark.asyncio
async def test_restricting_to_one_option_sends_that_options_bytes(_wire):
    client = _wire(_Client(message=_message(), votes=_votes_list()))

    await get_poll_voters(1, 11, option_index=1, limit=25, account="a")

    request = client.sent("GetPollVotesRequest")
    assert request.option == BLOBS[1]
    assert request.limit == 25


@pytest.mark.asyncio
async def test_an_anonymous_poll_refuses_to_list_voters_and_says_why(_wire):
    client = _wire(_Client(message=_message(_poll(public_voters=False))))

    result = await get_poll_voters(1, 11, account="a")

    assert "anonymous" in result and "get_poll_results" in result
    assert client.sent("GetPollVotesRequest") is None, "Telegram was asked anyway"


@pytest.mark.asyncio
async def test_an_out_of_range_option_is_refused_before_the_voters_are_fetched(_wire):
    client = _wire(_Client(message=_message(), votes=_votes_list()))

    result = await get_poll_voters(1, 11, option_index=9, account="a")

    assert "does not exist" in result and "0-2" in result
    assert client.sent("GetPollVotesRequest") is None


@pytest.mark.asyncio
async def test_create_poll_reports_the_message_it_created(monkeypatch):
    """Found by sweeping the writing tools: it answered "Poll created successfully"
    and nothing else, so the caller could not read results, vote, or take it down.

    The sweep littered Saved Messages because it had no id to clean up with. An
    agent is in the same position, with nobody watching.
    """
    from types import SimpleNamespace

    from telegram_mcp.tools import messages_state

    class _Client:
        async def __call__(self, request):
            return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=4242))])

    monkeypatch.setattr(messages_state, "get_client", lambda account=None: _Client())

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(messages_state, "ensure_connected", _ensure, raising=False)
    monkeypatch.setattr(messages_state, "resolve_entity", _resolve)

    payload = json.loads(await messages_state.create_poll("me", "probe?", ["a", "b"], account="a"))

    assert payload["results"][0]["message_id"] == 4242
    assert payload["created"] is True


def test_create_poll_accepts_a_chat_reference_not_only_a_number():
    """`chat_id: int` rejected "me" and @usernames at argument validation, while
    every neighbouring tool took Union[int, str]."""
    import inspect

    from telegram_mcp.tools import messages_state

    annotation = inspect.signature(messages_state.create_poll).parameters["chat_id"].annotation
    assert annotation is not int, "chat_id is int-only again; 'me' and @usernames cannot be used"


# --- a quiz has to carry its correct answer ---------------------------------


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
