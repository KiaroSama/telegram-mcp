"""Arm, send, restore - the three acts a timed send wears as one name.

`docs/adr/0002` records that this mutates the chat and why it was built anyway.
What that decision REQUIRES of the code is what these pin, and all three failures
are silent ones:

* the restore runs even when the send raised, and even when the send was
  cancelled, because a chat left armed keeps destroying messages nobody meant;
* a restore that FAILS is the loudest thing this server says, because only the
  reply can tell the operator the conversation is not as they left it;
* the timer is put BACK, never to zero - a chat the owner had deliberately armed
  must not come back disarmed because something passed through it.
"""

import asyncio
import json

import pytest

from telegram_mcp import secret_history
from telegram_mcp.tools import secret_timed as st

from secret_fakes import CHAT_ID, SECRET_ID


def _results(raw):
    return json.loads(raw)["results"]


# --- the argument, checked while being wrong is free ---------------------------


@pytest.mark.asyncio
async def test_a_timer_of_zero_names_the_tool_that_means_it(backend):
    answer = await st.send_timed_secret_message(CHAT_ID, "hi", 0, account="acct")

    assert "set_secret_chat_timer(seconds=0)" in answer
    assert backend.ttls == [], "the chat was touched for a request that was refused"


@pytest.mark.asyncio
async def test_a_timer_past_telegrams_ceiling_is_refused(backend):
    answer = await st.send_timed_secret_message(CHAT_ID, "hi", 604801, account="acct")

    assert "one week" in answer
    assert backend.ttls == []


# --- the happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_timer_is_armed_then_put_back(backend):
    answer = _results(await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct"))

    assert backend.ttls == [(SECRET_ID, 30), (SECRET_ID, 0)]
    assert answer["timer_restored"] is True and answer["restored_to"] == 0
    assert "belongs to the chat, not to the message" in answer["note"]


@pytest.mark.asyncio
async def test_a_chat_that_was_already_armed_comes_back_armed(backend):
    """Restoring to zero would silently disarm a chat the owner had set on
    purpose, and nothing would ever say so."""
    backend.status(SECRET_ID).ttl = 3600

    answer = _results(await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct"))

    assert backend.ttls == [(SECRET_ID, 30), (SECRET_ID, 3600)]
    assert answer["restored_to"] == 3600


@pytest.mark.asyncio
async def test_the_sent_message_is_recorded_under_the_timer_it_carried(backend):
    await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct")

    kept = secret_history.read("acct", SECRET_ID, 5)[-1]
    assert kept["is_outgoing"] is True and kept["self_destructs_after_seconds"] == 30


# --- the send fails ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_send_still_disarms_the_chat(backend, monkeypatch):
    """A defect, not a refusal: the caller gets an error code to quote and the
    chat still comes back the way they left it. The second is the point - an
    unexpected failure is exactly when a chat is most likely to be left armed."""

    async def _boom(chat_id, text, entities=None, reply_to=None):
        raise RuntimeError("wire down")

    monkeypatch.setattr(backend, "send_message", _boom)

    answer = await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct")

    assert "GEN-ERR" in answer
    assert backend.ttls == [(SECRET_ID, 30), (SECRET_ID, 0)]


@pytest.mark.asyncio
async def test_a_refusal_is_shown_with_the_timer_confirmed_back(backend, monkeypatch):
    from telethon_secret_chat.errors import MessageRejected

    async def _refused(chat_id, text, entities=None, reply_to=None):
        raise MessageRejected(chat_id=chat_id, reason="too long")

    monkeypatch.setattr(backend, "send_message", _refused)

    answer = await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct")

    assert "timer was put back to 0" in answer
    assert backend.ttls[-1] == (SECRET_ID, 0)


@pytest.mark.asyncio
async def test_a_cancellation_between_arming_and_sending_still_restores(backend, monkeypatch):
    """The `finally` covers what the `except` cannot. A cancelled turn that left
    a chat armed would keep destroying messages with nobody aware."""

    async def _cancelled(chat_id, text, entities=None, reply_to=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(backend, "send_message", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct")

    assert backend.ttls == [(SECRET_ID, 30), (SECRET_ID, 0)]


# --- the restore fails: the loudest case ---------------------------------------


@pytest.mark.asyncio
async def test_a_failed_restore_says_the_chat_is_still_armed(backend, monkeypatch):
    calls = []

    async def _one_way(chat_id, seconds):
        calls.append((int(chat_id), int(seconds)))
        if len(calls) > 1:
            raise RuntimeError("wire down")
        backend.status(chat_id).ttl = int(seconds)

    monkeypatch.setattr(backend, "set_ttl", _one_way)

    answer = _results(await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct"))

    assert answer["outcome"] == "unconfirmed"
    assert answer["sent"] is True
    assert answer["timer_left_on"] == 30 and answer["timer_should_have_been"] == 0
    assert "THE CHAT IS STILL ARMED" in answer["what_to_do"]
    assert f"set_secret_chat_timer(chat_id={CHAT_ID}, seconds=0)" in answer["what_to_do"]


# --- media ---------------------------------------------------------------------


@pytest.fixture
def photo(tmp_path, monkeypatch):
    target = tmp_path / "shot.jpg"
    target.write_bytes(b"\xff\xd8\xff")

    async def _resolve(raw_path, ctx, tool_name):
        return target, None

    monkeypatch.setattr(st, "_resolve_readable_file_path", _resolve)
    return target


@pytest.mark.asyncio
async def test_timed_media_arms_sends_and_restores(backend, photo):
    answer = _results(await st.send_timed_secret_media(CHAT_ID, str(photo), 30, account="acct"))

    assert backend.ttls == [(SECRET_ID, 30), (SECRET_ID, 0)]
    assert answer["kind"] == "photo" and answer["kind_chosen_by"] == "the file"


@pytest.mark.asyncio
async def test_a_refused_kind_never_arms_the_chat(backend, photo):
    """Checked BEFORE the timer moves: a chat must not be left armed for a
    message that was never going to exist."""
    answer = await st.send_timed_secret_media(
        CHAT_ID, str(photo), 30, kind="voice_note", account="acct"
    )

    assert "cannot be sent as voice_note" in answer
    assert backend.ttls == [], "the chat was armed for a send that was refused"


@pytest.mark.asyncio
async def test_a_chat_that_is_not_ready_is_refused_before_the_timer_moves(backend):
    backend.status(SECRET_ID).state.value = "pending"

    assert "still pending" in await st.send_timed_secret_message(CHAT_ID, "hi", 30, account="acct")
    assert backend.ttls == []


# --- overlapping timer changes (ADR 0002: a timed send puts BACK what was there) --------


def _slow_sends(backend, monkeypatch):
    real = backend.send_message

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.02)  # the window another caller used to land in
        return await real(*args, **kwargs)

    monkeypatch.setattr(backend, "send_message", slow)


@pytest.mark.asyncio
async def test_two_overlapping_timed_sends_leave_the_chat_as_it_was(backend, monkeypatch):
    _slow_sends(backend, monkeypatch)

    await asyncio.gather(
        st.send_timed_secret_message(CHAT_ID, "a", 30, account="acct"),
        st.send_timed_secret_message(CHAT_ID, "b", 5, account="acct"),
    )

    assert backend.status(SECRET_ID).ttl == 0
    # Each send armed and restored as a pair; neither interleaved with the other.
    pairs = [backend.ttls[i : i + 2] for i in range(0, len(backend.ttls), 2)]
    assert all(pair[1][1] == 0 for pair in pairs)


@pytest.mark.asyncio
async def test_a_timer_set_during_a_timed_send_is_not_undone(backend, monkeypatch):
    from telegram_mcp.tools import secret_chats

    _slow_sends(backend, monkeypatch)

    async def owner_sets_timer():
        await asyncio.sleep(0.005)  # lands while the timed send is in flight
        await secret_chats.set_secret_chat_timer(CHAT_ID, 60, account="acct")

    await asyncio.gather(
        st.send_timed_secret_message(CHAT_ID, "a", 30, account="acct"),
        owner_sets_timer(),
    )

    assert backend.status(SECRET_ID).ttl == 60
