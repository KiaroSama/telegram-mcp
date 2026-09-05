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
from pathlib import Path

import pytest

from telegram_mcp.tdlib import NotSignedIn, TDLibError, TDLibUnavailable
from telegram_mcp.tools import secret_chats as sc
from telegram_mcp.tools import secret_messaging as sm

# The tools live in two modules now. A function looks a name up in ITS OWN
# module globals, so a seam shared by both halves has to be patched in both
# or the patch succeeds while missing the caller under test.
_MODULES = (sc, sm)


def _patch_both(monkeypatch, name, value):
    for module in _MODULES:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


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
        _patch_both(monkeypatch, "_account_label", lambda account=None: "acct")

        async def _client(label):
            return client

        _patch_both(monkeypatch, "secret_client", _client)
        return client

    return _wire


def _results(raw):
    return json.loads(raw)["results"]


# --------------------------------------------------------------------------
# can_be_saved
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_the_sender_protected_is_not_downloaded_at_all(wire):
    """With `honour_sender_restriction=True`, the check comes BEFORE the transfer,
    so a refusal fetches nothing.

    This test used to justify the ordering by saying a download opens the message
    and starts the countdown. Measured against TDLib, that is false: fetching a
    timer-armed photo left `self_destruct_in` at 0, because VIEWING starts the
    countdown, not downloading. The ordering is still right for the plain reason
    - a call that refuses should not pull the bytes down first.
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

    result = _results(
        await sm.save_secret_media(
            chat_id=1, message_id=5, honour_sender_restriction=True, account="acct"
        )
    )

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

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["saved"] is True
    assert result["path"] == "C:/x/y.jpg"
    assert "disappear" in result["note"]


def test_can_be_saved_is_reported_as_telegram_sent_it():
    """Reported, never inferred. A secret chat does not imply False, and a
    missing field does not imply anything either - it is Telegram's default."""
    protected = sm._message_record({"id": 1, "can_be_saved": False, "content": {}})
    allowed = sm._message_record({"id": 2, "can_be_saved": True, "content": {}})

    assert protected["can_be_saved"] is False
    assert allowed["can_be_saved"] is True


# --------------------------------------------------------------------------
# Reading a message
# --------------------------------------------------------------------------


def test_a_running_countdown_is_distinguished_from_the_timer_it_started_from():
    """`self_destruct_in` is what is LEFT; the timer is how long it was. Folding
    them into one number would make an almost-expired message look untouched."""
    record = sm._message_record(
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
    record = sm._message_record(
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
    assert sm._media_file_id(content) == expected


def test_the_largest_photo_size_is_the_one_offered():
    """Telegram lists sizes smallest-first. Saving the thumbnail instead of the
    photo is a silent, permanent loss for a message that then self-destructs."""
    content = {
        "@type": "messagePhoto",
        "photo": {"sizes": [{"photo": {"id": 1}}, {"photo": {"id": 2}}, {"photo": {"id": 3}}]},
    }

    assert sm._media_file_id(content) == 3


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_per_message_timer_beyond_telegrams_limit_is_refused(wire, tmp_path):
    """Refused here rather than sent wrong: the same lesson `ephemeral.py`
    already learned, where an over-long timer was silently dropped and the media
    arrived permanent."""
    result = await sm.send_secret_media(
        chat_id=1, file_path=str(tmp_path / "x.jpg"), self_destruct_seconds=90, account="acct"
    )

    assert "0-60" in result


@pytest.mark.asyncio
async def test_text_carries_no_timer_of_its_own(wire):
    """Text self-destructs only via the chat's timer. A per-message field on the
    text request would be silently ignored, so it is not built."""
    client = wire({"sendMessage": {"@type": "message", "id": 12}})

    await sm.send_secret_message(chat_id=1, message="hello", account="acct")

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
    _patch_both(
        monkeypatch, "tdjson_status", lambda: {"available": True, "tdlib_version": "1.8.67"}
    )
    _patch_both(monkeypatch, "_account_label", lambda account=None: "acct")

    async def _refuse(label):
        raise NotSignedIn(label, "authorizationStateWaitPhoneNumber")

    _patch_both(monkeypatch, "secret_client", _refuse)

    result = _results(await sc.secret_chat_status(account="acct"))

    assert result["secret_chats"] == "not signed in"
    assert "secret_chat_login.py acct" in result["fix"]


@pytest.mark.asyncio
async def test_status_reports_the_install_step_when_the_library_is_absent(monkeypatch):
    _patch_both(
        monkeypatch, "tdjson_status", lambda: {"available": False, "reason": "not installed"}
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

        _patch_both(monkeypatch, "get_client", lambda account=None: object())

        async def _connected(client):
            return None

        async def _resolve(reference, client):
            return _User()

        _patch_both(monkeypatch, "ensure_connected", _connected)
        _patch_both(monkeypatch, "resolve_entity", _resolve)
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

    _patch_both(monkeypatch, "_resolve_readable_file_path", _path)
    client = wire({"sendMessage": {"@type": "message", "id": 5242881, "content": {}}})

    await sm.send_secret_media(chat_id=-1999148344067, file_path=str(sample), account="acct")

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

    _patch_both(monkeypatch, "_resolve_readable_file_path", _path)
    client = wire({"sendMessage": {"@type": "message", "id": 5242889, "content": {}}})

    await sm.send_secret_media(
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

    _patch_both(monkeypatch, "_resolve_readable_file_path", _path)
    client = wire()

    answer = await sm.send_secret_media(
        chat_id=-1999148344067, file_path=str(sample), self_destruct_seconds=30, account="acct"
    )

    assert "set_secret_chat_timer" in answer
    assert "Nothing was sent" in answer
    assert "sendMessage" not in client.types(), "it sent anyway"


# --------------------------------------------------------------------------
# overriding can_be_saved, and surviving the timer
# --------------------------------------------------------------------------


def _restricted_photo(with_timer=True):
    """What TDLib actually returns for a timer-armed secret photo.

    Measured on a live chat: can_be_saved false, the countdown NOT started, and
    the file undownloaded on arrival - `path` empty, `is_downloading_completed`
    false - so the saver has to fetch it.
    """
    message = {
        "@type": "message",
        "id": 5,
        "can_be_saved": False,
        "content": {
            "@type": "messagePhoto",
            "photo": {
                "sizes": [
                    {
                        "photo": {
                            "id": 1248,
                            "local": {"path": "", "is_downloading_completed": False},
                        }
                    }
                ]
            },
        },
    }
    if with_timer:
        message["self_destruct_type"] = {
            "@type": "messageSelfDestructTypeTimer",
            "self_destruct_time": 30,
        }
        message["self_destruct_in"] = 0.0
    return message


@pytest.mark.asyncio
async def test_refusing_is_available_but_has_to_be_asked_for(wire):
    """Both behaviours stay reachable. The owner chose saving as the default for
    their own account; a caller that wants the polite one says so."""
    client = wire({"getMessage": _restricted_photo()})

    result = _results(
        await sm.save_secret_media(
            chat_id=1, message_id=5, honour_sender_restriction=True, account="acct"
        )
    )

    assert result["saved"] is False
    assert "honour_sender_restriction" in result["detail"]
    assert "downloadFile" not in client.types(), "it fetched a message it refused"


@pytest.mark.asyncio
async def test_a_restricted_message_saves_and_says_that_it_was_restricted(
    tmp_path, monkeypatch, wire
):
    """Saving needs no argument - that is the owner's decision for their own
    account. What must never soften is the one boolean: a save that keeps
    someone's disappearing photo while reporting an ordinary success is the one
    outcome this tool must not produce."""
    from telegram_mcp import file_roots

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    tdlib_copy = tmp_path / "tdlib-side.jpg"
    tdlib_copy.write_bytes(b"secret-photo-bytes")

    wire(
        {
            "getMessage": _restricted_photo(),
            "downloadFile": {
                "@type": "file",
                "size": 18,
                "local": {"is_downloading_completed": True, "path": str(tdlib_copy)},
            },
        }
    )

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["saved"] is True
    assert result["sender_restriction_overridden"] is True


@pytest.mark.asyncio
async def test_media_under_a_timer_is_copied_out_of_tdlibs_directory(tmp_path, monkeypatch, wire):
    """The technical heart of it. TDLib deletes its OWN copy when the message
    self-destructs, so returning a path inside its database is a save that
    evaporates - the caller would hold a filename for bytes that are already
    gone. The durable copy is what `path` names."""
    from telegram_mcp import file_roots

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    tdlib_copy = tmp_path / "tdlib-side.jpg"
    tdlib_copy.write_bytes(b"secret-photo-bytes")

    wire(
        {
            "getMessage": _restricted_photo(),
            "downloadFile": {
                "@type": "file",
                "size": 18,
                "local": {"is_downloading_completed": True, "path": str(tdlib_copy)},
            },
        }
    )

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    kept = Path(result["path"])
    assert kept != tdlib_copy, "it handed back TDLib's own copy, which will be deleted"
    assert kept.read_bytes() == b"secret-photo-bytes", "the copy is not the media"
    assert result["tdlib_path"] == str(tdlib_copy), "the ephemeral path is not distinguished"
    assert "deletes" in result["kept_because"]


@pytest.mark.asyncio
async def test_media_with_no_timer_is_left_where_tdlib_put_it(tmp_path, monkeypatch, wire):
    """No timer means TDLib keeps its copy, so a second one would double the disk
    for nothing. Copying unconditionally is the easy mistake here."""
    from telegram_mcp import file_roots

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    tdlib_copy = tmp_path / "tdlib-side.jpg"
    tdlib_copy.write_bytes(b"ordinary-bytes")

    message = _restricted_photo(with_timer=False)
    message["can_be_saved"] = True
    wire(
        {
            "getMessage": message,
            "downloadFile": {
                "@type": "file",
                "size": 14,
                "local": {"is_downloading_completed": True, "path": str(tdlib_copy)},
            },
        }
    )

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["path"] == str(tdlib_copy)
    assert "tdlib_path" not in result, "it copied a file that was not going to be deleted"
    assert "sender_restriction_overridden" not in result


@pytest.mark.asyncio
async def test_a_file_tdlib_already_holds_is_not_fetched_again(tmp_path, monkeypatch, wire):
    """A completed local copy is used as-is. Measured, a secret photo usually
    arrives undownloaded so this is normally a miss - but re-fetching what is
    already on disk is a wasted round trip on a message that is expiring."""
    from telegram_mcp import file_roots

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    already = tmp_path / "already-there.jpg"
    already.write_bytes(b"held-bytes")

    message = _restricted_photo()
    message["can_be_saved"] = True
    message["content"]["photo"]["sizes"][0]["photo"]["local"] = {
        "is_downloading_completed": True,
        "path": str(already),
    }
    client = wire({"getMessage": message})

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert "downloadFile" not in client.types(), "it re-fetched a file it already had"
    assert Path(result["path"]).read_bytes() == b"held-bytes"


@pytest.mark.asyncio
async def test_the_byte_count_falls_back_to_the_file_when_tdlib_omits_it(
    tmp_path, monkeypatch, wire
):
    """Measured live: TDLib answered a secret-chat download with no `size`, so the
    result reported `size_bytes: null` - a receipt that cannot say how much it
    kept. The file on disk always knows."""
    from telegram_mcp import file_roots

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    tdlib_copy = tmp_path / "tdlib-side.jpg"
    tdlib_copy.write_bytes(b"0123456789")

    wire(
        {
            "getMessage": _restricted_photo(),
            "downloadFile": {
                "@type": "file",
                # No `size`, exactly as TDLib returned it.
                "local": {"is_downloading_completed": True, "path": str(tdlib_copy)},
            },
        }
    )

    result = _results(await sm.save_secret_media(chat_id=1, message_id=5, account="acct"))

    assert result["size_bytes"] == 10
