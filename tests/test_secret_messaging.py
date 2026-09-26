"""Sending, reading and saving inside a secret chat, on the replacement backend.

The four behaviours these pin are the ones the migration could have quietly lost:

* **formatting is reported, not dropped in silence** - which entities cross depends
  on the layer the two devices negotiated, and the answer names each loss;
* **history carries both directions and survives a restart** - the package records
  only what arrived, and only in memory, so this server keeps its own;
* **a reply is refused rather than downgraded** when its target is not here;
* **a file's key lives inside its message**, so a save after a restart is reported
  as impossible rather than answered with an empty path.
"""

import json
from types import SimpleNamespace

import pytest
from telethon.tl import types

from telegram_mcp import secret_history
from telegram_mcp.tools import secret_messaging as sm

from secret_fakes import CHAT_ID, SECRET_ID


def _results(raw):
    return json.loads(raw)["results"]


# --- sending text --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_message_is_sent_and_recorded_as_outgoing(backend):
    answer = _results(await sm.send_secret_message(CHAT_ID, "hello", account="acct"))

    assert answer["sent"] is True
    assert backend.sent[-1].text == "hello"
    kept = secret_history.read("acct", SECRET_ID, 10)
    assert kept[-1]["is_outgoing"] is True and kept[-1]["text"] == "hello"


@pytest.mark.asyncio
async def test_a_delivered_message_is_reported_sent_even_if_the_local_copy_fails(
    backend, monkeypatch
):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(secret_history, "record", broken)
    answer = _results(await sm.send_secret_message(CHAT_ID, "hello", account="acct"))

    assert answer["sent"] is True
    assert "was delivered" in answer["local_copy"]


@pytest.mark.asyncio
async def test_nothing_dropped_means_the_field_is_absent(backend):
    """The caller's signal is the field EXISTING, so an empty list would read as
    a loss that did not happen."""
    answer = _results(await sm.send_secret_message(CHAT_ID, "plain", account="acct"))

    assert "dropped_formatting" not in answer


@pytest.mark.asyncio
async def test_formatting_the_layer_cannot_carry_is_named(backend, monkeypatch):
    """A spoiler needs layer 144. At the floor it is dropped, and a caller who is
    not told has sent a message whose meaning depended on it."""
    backend.status(SECRET_ID).layer = 73
    monkeypatch.setattr(sm, "formatted_text", lambda m, p: (m, [types.MessageEntitySpoiler(0, 3)]))

    answer = _results(await sm.send_secret_message(CHAT_ID, "shh", "markdown", account="acct"))

    assert answer["dropped_formatting"][0]["kind"] == "spoiler"
    assert "Re-send it as plain words" in answer["dropped_note"]


@pytest.mark.asyncio
async def test_a_chat_at_the_negotiated_layer_carries_the_spoiler(backend, monkeypatch):
    monkeypatch.setattr(sm, "formatted_text", lambda m, p: (m, [types.MessageEntitySpoiler(0, 3)]))

    answer = _results(await sm.send_secret_message(CHAT_ID, "shh", "markdown", account="acct"))

    assert "dropped_formatting" not in answer


@pytest.mark.asyncio
async def test_an_unknown_parse_mode_is_refused_before_anything_is_sent(backend):
    answer = await sm.send_secret_message(CHAT_ID, "x", "klingon", account="acct")

    assert "parse_mode must be" in answer
    assert backend.sent == [], "a message went out under a mode that was refused"


@pytest.mark.asyncio
async def test_a_chat_that_is_not_ready_refuses_and_says_what_clears_it(backend):
    backend.status(SECRET_ID).state.value = "pending"

    answer = await sm.send_secret_message(CHAT_ID, "hi", account="acct")

    assert "still pending" in answer and "other side" in answer
    assert backend.sent == []


@pytest.mark.asyncio
async def test_a_closed_chat_says_it_cannot_be_reopened(backend):
    backend.status(SECRET_ID).state.value = "closed"

    assert "cannot be reopened" in await sm.send_secret_message(CHAT_ID, "hi", account="acct")


@pytest.mark.asyncio
async def test_a_rekeying_chat_still_sends(backend):
    """A routine key rotation must not read as an outage: the exchange holds two
    keys and the message goes out under whichever settles."""
    backend.status(SECRET_ID).state.value = "rekeying"

    answer = _results(await sm.send_secret_message(CHAT_ID, "hi", account="acct"))

    assert answer["sent"] is True


# --- replies -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_to_a_message_this_device_never_saw_is_refused(backend):
    answer = await sm.send_secret_message(
        CHAT_ID, "answering", reply_to_message_id=999, account="acct"
    )

    assert "not in this device's copy" in answer
    assert backend.sent == [], "a reply that could not be a reply was sent anyway"


@pytest.mark.asyncio
async def test_a_reply_to_a_known_message_carries_its_id(backend):
    first = _results(await sm.send_secret_message(CHAT_ID, "question", account="acct"))

    answer = _results(
        await sm.send_secret_message(
            CHAT_ID, "answer", reply_to_message_id=first["message_id"], account="acct"
        )
    )

    assert answer["reply_to_message_id"] == first["message_id"]
    assert backend.sent[-1].reply_to == first["message_id"]


# --- sending media -------------------------------------------------------------


@pytest.fixture
def photo(tmp_path, monkeypatch):
    target = tmp_path / "shot.jpg"
    target.write_bytes(b"\xff\xd8\xff")

    async def _resolve(raw_path, ctx, tool_name):
        return target, None

    monkeypatch.setattr(sm, "_resolve_readable_file_path", _resolve)
    return target


@pytest.mark.asyncio
async def test_media_reports_the_kind_and_who_chose_it(backend, photo):
    answer = _results(await sm.send_secret_media(CHAT_ID, str(photo), account="acct"))

    assert answer["kind"] == "photo" and answer["kind_chosen_by"] == "the file"
    assert backend.files[-1].kind == "photo"


@pytest.mark.asyncio
async def test_a_kind_the_file_cannot_be_is_refused_before_the_upload(backend, photo):
    answer = await sm.send_secret_media(CHAT_ID, str(photo), kind="voice_note", account="acct")

    assert "cannot be sent as voice_note" in answer
    assert backend.files == [], "the bytes were uploaded for a kind that was refused"


@pytest.mark.asyncio
async def test_a_caption_on_a_captionless_kind_is_refused(backend, tmp_path, monkeypatch):
    target = tmp_path / "s.webp"
    target.write_bytes(b"RIFF")

    async def _resolve(raw_path, ctx, tool_name):
        return target, None

    monkeypatch.setattr(sm, "_resolve_readable_file_path", _resolve)

    answer = await sm.send_secret_media(
        CHAT_ID, str(target), kind="sticker", caption="hi", account="acct"
    )

    assert "carries no caption" in answer


@pytest.mark.asyncio
async def test_a_per_message_timer_names_the_tool_that_works(backend, photo):
    """Telegram refuses one outright in a secret chat, so the refusal has to point
    at the mechanism that exists rather than just saying no."""
    answer = await sm.send_secret_media(
        CHAT_ID, str(photo), self_destruct_seconds=30, account="acct"
    )

    assert "set_secret_chat_timer" in answer
    assert backend.files == []


@pytest.mark.asyncio
async def test_the_deprecated_flag_and_a_different_kind_are_refused_together(backend, photo):
    answer = await sm.send_secret_media(
        CHAT_ID, str(photo), kind="photo", as_voice=True, account="acct"
    )

    assert "ask for different things" in answer


# --- reading -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_holds_both_directions(backend):
    """The package records only what ARRIVED. A conversation showing one side
    talking is the loss this server's own record exists to prevent."""
    await sm.send_secret_message(CHAT_ID, "mine", account="acct")
    secret_history.record(
        "acct", SECRET_ID, secret_history.entry(message_id=11, is_outgoing=False, text="theirs")
    )

    answer = _results(await sm.read_secret_messages(CHAT_ID, account="acct"))

    assert [m["is_outgoing"] for m in answer["messages"]] == [True, False]


@pytest.mark.asyncio
async def test_an_empty_chat_says_it_is_this_device_only(backend):
    assert "on this device" in await sm.read_secret_messages(CHAT_ID, account="acct")


@pytest.mark.asyncio
async def test_a_limit_outside_the_range_is_refused(backend):
    assert "limit" in (await sm.read_secret_messages(CHAT_ID, 0, account="acct")).lower()


# --- saving media --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_no_longer_in_memory_explains_why_rather_than_failing(backend):
    """The file key travels inside the message. After a restart the text is still
    here and the bytes are unreachable, and those are different facts."""
    secret_history.record(
        "acct", SECRET_ID, secret_history.entry(message_id=77, is_outgoing=False, kind="photo")
    )

    answer = await sm.save_secret_media(CHAT_ID, 77, account="acct")

    assert "file key travels INSIDE the message" in answer
    assert "read_secret_messages still shows the message" in answer


@pytest.mark.asyncio
async def test_a_live_message_is_decrypted_to_the_chosen_path(backend, tmp_path, monkeypatch):
    backend.history[SECRET_ID] = [SimpleNamespace(random_id=77, media=object(), ttl=0, text="")]
    _writable(monkeypatch, tmp_path)

    answer = _results(await sm.save_secret_media(CHAT_ID, 77, account="acct"))

    assert answer["saved"] is True and answer["size_bytes"] == len(b"decrypted")
    assert "sender_restriction_overridden" not in answer


@pytest.mark.asyncio
async def test_a_timed_message_saves_and_says_the_restriction_was_overridden(
    backend, tmp_path, monkeypatch
):
    backend.history[SECRET_ID] = [SimpleNamespace(random_id=78, media=object(), ttl=30, text="")]
    _writable(monkeypatch, tmp_path)

    answer = _results(await sm.save_secret_media(CHAT_ID, 78, account="acct"))

    assert answer["saved"] is True
    assert answer["sender_restriction_overridden"] is True
    assert "chose to have this disappear" in answer["note"]


@pytest.mark.asyncio
async def test_refusing_is_available_but_has_to_be_asked_for(backend, tmp_path, monkeypatch):
    backend.history[SECRET_ID] = [SimpleNamespace(random_id=79, media=object(), ttl=30, text="")]
    _writable(monkeypatch, tmp_path)

    answer = _results(
        await sm.save_secret_media(CHAT_ID, 79, honour_sender_restriction=True, account="acct")
    )

    assert answer["saved"] is False
    assert backend.saved == [], "the file was fetched despite the refusal"


@pytest.mark.asyncio
async def test_a_message_without_media_says_so(backend, tmp_path, monkeypatch):
    backend.history[SECRET_ID] = [SimpleNamespace(random_id=80, media=None, ttl=0, text="hi")]
    _writable(monkeypatch, tmp_path)

    assert "no downloadable media" in await sm.save_secret_media(CHAT_ID, 80, account="acct")


def _writable(monkeypatch, tmp_path):
    """Let the save tool write into `tmp_path` without the roots machinery."""
    import contextlib

    class _Dir:
        path = str(tmp_path)

        def reserve_free_name(self, stem, suffix):
            return f"{stem}{suffix}"

        def discard(self, name):
            (tmp_path / name).unlink(missing_ok=True)

    @contextlib.asynccontextmanager
    async def _open(path, ctx, tool_name):
        yield _Dir(), None

    async def _resolve(raw_path, default_filename, ctx, tool_name):
        return tmp_path / default_filename, None

    monkeypatch.setattr(sm, "_open_verified_directory", _open)
    monkeypatch.setattr(sm, "_resolve_writable_file_path", _resolve)
