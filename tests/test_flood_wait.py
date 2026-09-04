"""A rate limit is an instruction, not an error code.

An agent handed "an error occurred (GEN-ERR-123)" retries. Every retry inside a
FloodWait window extends the penalty, so the one failure mode worth a dedicated
test is a model politely hammering Telegram until the account is limited for
hours. Ported from upstream chigwell/telegram-mcp (PR #204, issue #180).
"""

import pytest
from telethon.errors.rpcerrorlist import FloodWaitError

from telegram_mcp.runtime import log_and_format_error


def test_a_rate_limit_names_the_seconds_and_forbids_a_retry():
    answer = log_and_format_error("send_message", FloodWaitError(request=None), chat_id=7)

    assert "ERR-" not in answer, "a rate limit came back as an opaque error code"
    assert "Do NOT retry" in answer


def test_the_wait_reported_is_telegrams_own_number():
    error = FloodWaitError(request=None)
    error.seconds = 420

    answer = log_and_format_error("send_message", error)

    assert "420 seconds" in answer, f"the wait was not passed through: {answer}"


def test_an_ordinary_error_still_gets_its_code():
    """The short-circuit must not swallow everything: a code is how a user's
    report is correlated with a log line."""
    answer = log_and_format_error("send_message", ValueError("something else"))

    assert "ERR-" in answer
    assert "Do NOT retry" not in answer


def test_an_error_that_merely_has_seconds_is_not_treated_as_a_rate_limit():
    """`seconds` is a common attribute name. Matching on it alone would turn an
    unrelated failure into a confident instruction to wait."""

    class _Timeout(Exception):
        seconds = 30

    answer = log_and_format_error("send_message", _Timeout("timed out"))

    assert "Do NOT retry" not in answer
    assert "ERR-" in answer


@pytest.mark.parametrize("seconds", [0, 1, 86400])
def test_every_wait_length_is_reported_rather_than_rounded(seconds):
    error = FloodWaitError(request=None)
    error.seconds = seconds

    assert f"{seconds} seconds" in log_and_format_error("get_history", error)
