"""Secret chats: the parts that can be tested without a live end-to-end session.

No network and no TDLib: the client is a fake that records the requests the
tools build, so these assert on what would be sent and on how an answer is read
back — not on a string that could be right for the wrong reason.

The load-bearing one is `can_be_saved`. These tools exist partly to answer "can
the other side keep this", and the honest answer is Telegram's, not an inference
from the chat being secret. A bug that defaulted it to True, or that downloaded
first and checked after, would be invisible in a passing round-trip and would
quietly defeat the sender's choice — so it is pinned from both directions here.
"""

import json

import pytest

from telegram_mcp.tdlib import NotSignedIn, TDLibError, TDLibUnavailable
from telegram_mcp.tools import secret_chats as sc


class FakeTDLib:
    """Records every request and answers from a scripted table."""

    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or {}

    async def request(self, obj, timeout=30.0):
        self.requests.append(obj)
        answer = self.answers.get(obj["@type"])
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {"@type": "ok"}

    def types(self):
        return [r["@type"] for r in self.requests]


@pytest.fixture
def wire(monkeypatch):
    """Patch the two seams every tool goes through."""

    def _wire(answers=None):
        client = FakeTDLib(answers)
        monkeypatch.setattr(sc, "_account_label", lambda account=None: "acct")

        async def _client(label):
            return client

        monkeypatch.setattr(sc, "secret_client", _client)
        return client

    return _wire


def _results(raw):
    return json.loads(raw)["results"]


# --------------------------------------------------------------------------
# can_be_saved
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_the_sender_protected_is_not_downloaded_at_all(wire):
    """The check has to come BEFORE the transfer, not after.

    Downloading opens the message, which starts the self-destruct countdown. A
    tool that fetched first and then declined to keep the bytes would already
    have destroyed the thing it declined to copy.
    """
    client = wire(
        {
            "getMessage": {
                "@type": "message",
                "id": 5,
                "can_be_saved": False,
                "content": {
                    "@type": "messagePhoto",
                    "photo": {"sizes": [{"photo": {"id": 77}}]},
                },
            }
        }
    )

    result = _results(await sc.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["saved"] is False
    assert "can_be_saved" in result["reason"]
    assert "downloadFile" not in client.types(), "opened a message it then refused to save"


@pytest.mark.asyncio
async def test_a_message_that_may_be_saved_is_downloaded_and_still_says_so(wire):
    """Permission granted is not the same as no warning: the sender still chose a
    disappearing message, and the result says that in both branches."""
    wire(
        {
            "getMessage": {
                "@type": "message",
                "id": 5,
                "can_be_saved": True,
                "content": {
                    "@type": "messagePhoto",
                    "photo": {"sizes": [{"photo": {"id": 77}}]},
                },
            },
            "downloadFile": {
                "@type": "file",
                "size": 2048,
                "local": {"is_downloading_completed": True, "path": "C:/x/y.jpg"},
            },
        }
    )

    result = _results(await sc.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["saved"] is True
    assert result["path"] == "C:/x/y.jpg"
    assert "disappear" in result["note"]


def test_can_be_saved_is_reported_as_telegram_sent_it():
    """Reported, never inferred. A secret chat does not imply False, and a
    missing field does not imply anything either - it is Telegram's default."""
    protected = sc._message_record({"id": 1, "can_be_saved": False, "content": {}})
    allowed = sc._message_record({"id": 2, "can_be_saved": True, "content": {}})

    assert protected["can_be_saved"] is False
    assert allowed["can_be_saved"] is True


# --------------------------------------------------------------------------
# Reading a message
# --------------------------------------------------------------------------


def test_a_running_countdown_is_distinguished_from_the_timer_it_started_from():
    """`self_destruct_in` is what is LEFT; the timer is how long it was. Folding
    them into one number would make an almost-expired message look untouched."""
    record = sc._message_record(
        {
            "id": 3,
            "content": {"@type": "messagePhoto", "photo": {"sizes": []}},
            "self_destruct_type": {
                "@type": "messageSelfDestructTypeTimer",
                "self_destruct_time": 30,
            },
            "self_destruct_in": 4.27,
        }
    )

    assert record["self_destructs_after_seconds"] == 30
    assert record["self_destruct_in_seconds"] == 4.3


def test_an_untouched_disappearing_message_reports_no_countdown():
    """Nothing is running until it is opened, and reporting a countdown that has
    not started would make it look like the message was already read."""
    record = sc._message_record(
        {
            "id": 3,
            "content": {"@type": "messagePhoto", "photo": {"sizes": []}},
            "self_destruct_type": {
                "@type": "messageSelfDestructTypeTimer",
                "self_destruct_time": 30,
            },
        }
    )

    assert "self_destruct_in_seconds" not in record


@pytest.mark.parametrize(
    "content,expected",
    [
        ({"@type": "messagePhoto", "photo": {"sizes": [{"photo": {"id": 9}}]}}, 9),
        ({"@type": "messageVoiceNote", "voice_note": {"voice": {"id": 11}}}, 11),
        ({"@type": "messageText", "text": {"text": "hi"}}, None),
    ],
)
def test_the_downloadable_file_is_found_wherever_telegram_puts_it(content, expected):
    """`save_secret_media` and the record builder must agree about this, or a
    record advertises a file the saver cannot then find."""
    assert sc._media_file_id(content) == expected


def test_the_largest_photo_size_is_the_one_offered():
    """Telegram lists sizes smallest-first. Saving the thumbnail instead of the
    photo is a silent, permanent loss for a message that then self-destructs."""
    content = {
        "@type": "messagePhoto",
        "photo": {"sizes": [{"photo": {"id": 1}}, {"photo": {"id": 2}}, {"photo": {"id": 3}}]},
    }

    assert sc._media_file_id(content) == 3


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_per_message_timer_beyond_telegrams_limit_is_refused(wire, tmp_path):
    """Refused here rather than sent wrong: the same lesson `ephemeral.py`
    already learned, where an over-long timer was silently dropped and the media
    arrived permanent."""
    result = await sc.send_secret_media(
        chat_id=1, file_path=str(tmp_path / "x.jpg"), self_destruct_seconds=90, account="acct"
    )

    assert "0-60" in result


@pytest.mark.asyncio
async def test_text_carries_no_timer_of_its_own(wire):
    """Text self-destructs only via the chat's timer. A per-message field on the
    text request would be silently ignored, so it is not built."""
    client = wire({"sendMessage": {"@type": "message", "id": 12}})

    await sc.send_secret_message(chat_id=1, message="hello", account="acct")

    (sent,) = [r for r in client.requests if r["@type"] == "sendMessage"]
    assert sent["input_message_content"]["@type"] == "inputMessageText"
    assert "self_destruct_type" not in sent["input_message_content"]


@pytest.mark.asyncio
async def test_the_chat_timer_is_set_on_the_chat_not_the_message(wire):
    """The one mechanism that makes text disappear anywhere in Telegram."""
    client = wire()

    result = _results(await sc.set_secret_chat_timer(chat_id=7, seconds=60, account="acct"))

    (sent,) = [r for r in client.requests if r["@type"] == "setChatMessageAutoDeleteTime"]
    assert sent["message_auto_delete_time"] == 60
    assert "from now on" in result["applies_to"]


# --------------------------------------------------------------------------
# The prerequisites, which fail in two different places
# --------------------------------------------------------------------------


def test_a_missing_login_names_the_script_that_fixes_it():
    """An account signed in to Telethon but not TDLib is the one confusing state
    here, and the fix is a command nobody would guess."""
    message = str(NotSignedIn("kgb_verifier", "authorizationStateWaitPhoneNumber"))

    assert "scripts/secret_chat_login.py kgb_verifier" in message
    assert "cannot import a Telethon session" in message
    # And it must say the fix is free. The message used to read "one extra
    # sign-in", which was true and still cost a reader the wrong expectation:
    # the existing login authorises TDLib, so no code is asked for.
    assert "no code" in message


def test_a_missing_library_names_the_install_command():
    """The other prerequisite, fixed somewhere completely different. Telling
    these two apart is the whole reason `secret_chat_status` exists."""
    assert "pip install tdjson" in str(
        TDLibUnavailable(
            "Secret chats need Telegram's own library, which is not installed. "
            "Install it with: pip install tdjson"
        )
    )


@pytest.mark.asyncio
async def test_status_reports_the_login_step_rather_than_failing(monkeypatch):
    """A tool that raised here would leave the caller unable to tell an absent
    dependency from an unsigned-in account."""
    monkeypatch.setattr(
        sc, "tdjson_status", lambda: {"available": True, "tdlib_version": "1.8.67"}
    )
    monkeypatch.setattr(sc, "_account_label", lambda account=None: "acct")

    async def _refuse(label):
        raise NotSignedIn(label, "authorizationStateWaitPhoneNumber")

    monkeypatch.setattr(sc, "secret_client", _refuse)

    result = _results(await sc.secret_chat_status(account="acct"))

    assert result["secret_chats"] == "not signed in"
    assert "secret_chat_login.py acct" in result["fix"]


@pytest.mark.asyncio
async def test_status_reports_the_install_step_when_the_library_is_absent(monkeypatch):
    monkeypatch.setattr(
        sc, "tdjson_status", lambda: {"available": False, "reason": "not installed"}
    )

    result = _results(await sc.secret_chat_status(account="acct"))

    assert result["secret_chats"] == "unavailable"
    assert "tdjson" in result["fix"]


# --------------------------------------------------------------------------
# Opening a secret chat with someone TDLib has never heard of
# --------------------------------------------------------------------------


@pytest.fixture
def peer(monkeypatch):
    """Resolve any reference to one user, the way Telethon would."""

    def _peer(user_id=5899781975):
        class _User:
            id = user_id

        monkeypatch.setattr(sc, "get_client", lambda account=None: object())

        async def _connected(client):
            return None

        async def _resolve(reference, client):
            return _User()

        monkeypatch.setattr(sc, "ensure_connected", _connected)
        monkeypatch.setattr(sc, "resolve_entity", _resolve)
        return user_id

    return _peer


@pytest.mark.asyncio
async def test_tdlib_is_taught_the_user_before_the_chat_is_asked_for(wire, peer):
    """Telethon and TDLib keep SEPARATE databases.

    Resolving the reference populates Telethon's, not TDLib's, and a TDLib
    database created minutes ago knows almost nobody. `createNewSecretChat` on a
    perfectly valid id then fails with a refusal naming neither the user nor the
    reason - which is exactly what a freshly signed-in account hit.
    """
    user_id = peer()
    client = wire({"createNewSecretChat": {"@type": "secretChat", "id": 7, "state": {}}})

    await sc.create_secret_chat(user_id=user_id, account="acct")

    types = client.types()
    assert "createPrivateChat" in types, "TDLib was never told who the user is"
    assert types.index("createPrivateChat") < types.index(
        "createNewSecretChat"
    ), "the user was fetched after the chat was already asked for"


@pytest.mark.asyncio
async def test_a_user_tdlib_already_knows_costs_nothing_extra(wire, peer):
    """A failing lookup must not stop the real call: TDLib may already know
    them, and the verdict belongs to `createNewSecretChat`."""
    user_id = peer()
    client = wire(
        {
            "createPrivateChat": TDLibError(400, "Chat not found"),
            "createNewSecretChat": {"@type": "secretChat", "id": 7, "state": {}},
        }
    )

    answer = await sc.create_secret_chat(user_id=user_id, account="acct")

    assert "createNewSecretChat" in client.types()
    assert "Invitation sent" in answer


@pytest.mark.asyncio
async def test_telegrams_refusal_is_shown_not_filed_under_an_error_code(wire, peer):
    """The reason is one sentence from Telegram - "the user restricts new chats",
    "have no write access". Hiding it behind a code sends the reader to a log to
    find out what the API already said."""
    user_id = peer()
    wire({"createNewSecretChat": TDLibError(400, "USER_RESTRICTED_NEW_CHATS")})

    answer = await sc.create_secret_chat(user_id=user_id, account="acct")

    assert "USER_RESTRICTED_NEW_CHATS" in answer
    assert "error occurred" not in answer.lower(), "the reason was replaced by a code"


@pytest.mark.asyncio
async def test_the_file_goes_inside_the_wrapper_telegram_expects(wire, monkeypatch, tmp_path):
    """The bug that read as a broken TDLib build for an entire evening.

    `inputMessagePhoto.photo` is not an InputFile - it is an `inputPhoto`, whose
    OWN `photo` field holds the file. Passing the file a level too high left that
    inner field null and TDLib answered "InputFile is not specified": an error
    that names the type it wanted and not the place, which sent the search
    through every path format, file id and remote id instead. TDLib's own log,
    once its verbosity was raised, printed `photo = inputPhoto { photo = null`
    and settled it in one line.
    """
    sample = tmp_path / "x.png"
    sample.write_bytes(b"fake-png-bytes")

    async def _path(raw_path, ctx, tool_name):
        return sample, None

    monkeypatch.setattr(sc, "_resolve_readable_file_path", _path)
    client = wire({"sendMessage": {"@type": "message", "id": 5242881, "content": {}}})

    await sc.send_secret_media(chat_id=-1999148344067, file_path=str(sample), account="acct")

    (sent,) = [r for r in client.requests if r["@type"] == "sendMessage"]
    photo = sent["input_message_content"]["photo"]
    assert photo["@type"] == "inputPhoto", "the file was passed a level too high again"
    assert photo["photo"]["@type"] == "inputFileLocal"


@pytest.mark.asyncio
async def test_a_voice_note_is_wrapped_the_same_way(wire, monkeypatch, tmp_path):
    sample = tmp_path / "x.ogg"
    sample.write_bytes(b"OggS")

    async def _path(raw_path, ctx, tool_name):
        return sample, None

    monkeypatch.setattr(sc, "_resolve_readable_file_path", _path)
    client = wire({"sendMessage": {"@type": "message", "id": 5242889, "content": {}}})

    await sc.send_secret_media(
        chat_id=-1999148344067, file_path=str(sample), as_voice=True, account="acct"
    )

    (sent,) = [r for r in client.requests if r["@type"] == "sendMessage"]
    note = sent["input_message_content"]["voice_note"]
    assert note["@type"] == "inputVoiceNote"
    assert note["voice_note"]["@type"] == "inputFileLocal"


@pytest.mark.asyncio
async def test_a_per_message_timer_is_refused_with_the_one_that_works(wire, monkeypatch, tmp_path):
    """Telegram refuses a per-message self-destruct in a secret chat outright:
    "Messages can self-destruct only in private chats". The chat's own timer is
    the mechanism there, so the refusal names it rather than sending a request
    that cannot succeed."""
    sample = tmp_path / "x.png"
    sample.write_bytes(b"fake-png-bytes")

    async def _path(raw_path, ctx, tool_name):
        return sample, None

    monkeypatch.setattr(sc, "_resolve_readable_file_path", _path)
    client = wire()

    answer = await sc.send_secret_media(
        chat_id=-1999148344067, file_path=str(sample), self_destruct_seconds=30, account="acct"
    )

    assert "set_secret_chat_timer" in answer
    assert "Nothing was sent" in answer
    assert "sendMessage" not in client.types(), "it sent anyway"
