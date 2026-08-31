"""Profile mutations: rewriting the account's own name, bio and photo.

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which request the tools build, not about a returned
string that could be right for the wrong reason.

The one that matters most is the ordering in set_profile_photo: the path check
runs BEFORE the upload, so a refused path must mean no bytes left the machine.
That test asserts on `upload_file` never having been called — the returned
string is right for the wrong reason if the ordering ever inverts.
"""

from types import SimpleNamespace

import pytest

from tests.helpers_handles import source_gate, uploaded_names

from telegram_mcp.tools import profile as mod
from telegram_mcp.tools.profile import (
    delete_profile_photo,
    set_bot_commands,
    set_profile_photo,
    update_profile,
)


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, photos=(), bot=False):
        self.requests = []
        self.photos = list(photos)
        self.uploads = []
        self.bot = bot

    async def get_me(self):
        return SimpleNamespace(bot=self.bot, username="thisbot", id=7)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetUserPhotosRequest":
            return SimpleNamespace(photos=self.photos)
        if name == "SetBotCommandsRequest":
            return True
        if name in ("UpdateProfileRequest", "UploadProfilePhotoRequest", "DeletePhotosRequest"):
            return SimpleNamespace(updates=[])
        raise AssertionError(f"unexpected request {name}")

    async def upload_file(self, path):
        self.uploads.append(path)
        return SimpleNamespace(handle=path)

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch, tmp_path):
    def wire(client, path_error=None, resolved=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        if resolved is None:
            resolved = tmp_path / "avatar.jpg"
            if not resolved.exists():
                resolved.write_bytes(b"jpeg-bytes")

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(
            mod,
            "_open_verified_source",
            source_gate(lambda raw: (None, path_error) if path_error else (resolved, None)),
        )
        return client

    return wire


@pytest.mark.asyncio
async def test_updating_a_profile_passes_through_exactly_what_it_was_given(_wire):
    """Telegram treats an absent field as "leave this alone", so a None must stay a
    None rather than becoming an empty string that wipes the bio."""
    client = _wire(_Client())

    await update_profile(first_name="Ada", about=None, account="a")

    assert client.names == ["UpdateProfileRequest"]
    sent = client.sent("UpdateProfileRequest")
    assert sent.first_name == "Ada"
    assert sent.last_name is None, "an unsupplied name was sent as something"
    assert sent.about is None, "an unsupplied bio was sent as something"


@pytest.mark.asyncio
async def test_a_refused_photo_path_uploads_nothing(_wire):
    """The gate is only a gate if it runs before the bytes leave the machine."""
    client = _wire(_Client(), path_error="Path is outside allowed roots.")

    result = await set_profile_photo("../../secrets.png", account="a")

    assert result == "Path is outside allowed roots."
    assert client.uploads == [], "the refused file was uploaded anyway"
    assert client.requests == [], "a profile photo request followed a refused path"


@pytest.mark.asyncio
async def test_an_accepted_photo_path_uploads_then_sets(_wire):
    """The upload must use the path the gate resolved, not the raw argument."""
    client = _wire(_Client())

    await set_profile_photo("avatar.jpg", account="a")

    # An open handle, not the raw argument and not a name Telethon would
    # have had to resolve for a second time.
    assert uploaded_names(client.uploads) == ["avatar.jpg"]
    assert client.names == ["UploadProfilePhotoRequest"]


@pytest.mark.asyncio
async def test_deleting_a_photo_when_there_is_none_sends_no_delete(_wire):
    client = _wire(_Client(photos=[]))

    result = await delete_profile_photo(account="a")

    assert result == "No profile photo to delete."
    assert "DeletePhotosRequest" not in client.names, "a delete was sent for nothing"


@pytest.mark.asyncio
async def test_deleting_a_photo_deletes_the_most_recent_one(_wire):
    """GetUserPhotosRequest returns newest first; deleting the wrong element would
    remove a photo the caller did not ask about."""
    newest, older = SimpleNamespace(id=2), SimpleNamespace(id=1)
    client = _wire(_Client(photos=[newest, older]))

    await delete_profile_photo(account="a")

    assert client.names == ["GetUserPhotosRequest", "DeletePhotosRequest"]
    assert client.sent("DeletePhotosRequest").id == [newest]


# --- bot commands ------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_bot_commands_does_not_promise_to_target_another_bot(_wire):
    """Telegram's bots.setBotCommands has no field naming a bot: it always applies to
    the calling account. bots.setBotInfo DOES take `bot=`, so the omission is the
    protocol's decision, not an oversight. A `bot_username` parameter here could only
    ever be decoration - it was resolved and then dropped, while the reply claimed the
    commands had been set for it.
    """
    import inspect

    params = inspect.signature(set_bot_commands).parameters
    assert "bot_username" not in params, (
        "the signature still accepts a target bot it cannot act on: " f"{list(params)}"
    )


@pytest.mark.asyncio
async def test_setting_commands_sends_one_bot_command_per_row(_wire):
    client = _wire(_Client(bot=True))

    result = await set_bot_commands(
        [
            {"command": "start", "description": "Begin"},
            {"command": "help", "description": "Show help"},
        ],
        account="a",
    )

    request = client.sent("SetBotCommandsRequest")
    assert request is not None, f"sent {client.names}"
    assert [(c.command, c.description) for c in request.commands] == [
        ("start", "Begin"),
        ("help", "Show help"),
    ]
    assert type(request.scope).__name__ == "BotCommandScopeDefault"
    assert request.lang_code == "en"
    assert "thisbot" in result


@pytest.mark.asyncio
async def test_a_user_session_is_refused_before_anything_is_sent(_wire):
    """The server's own session generator only does phone/QR login, so the account is
    a user unless someone supplies a bot session string by hand. Refusing early keeps
    that a sentence rather than a raw BOT_METHOD_INVALID."""
    client = _wire(_Client(bot=False))

    result = await set_bot_commands([{"command": "start", "description": "Begin"}], account="a")

    assert client.requests == [], "a user session must not reach Telegram"
    assert "bot" in result.lower()


@pytest.mark.asyncio
async def test_an_unauthorised_login_is_explained_rather_than_crashing(monkeypatch):
    """Telethon answers `None` from `get_me()` for a client that is connected but
    no longer authorised. That None used to reach `format_entity` and die on
    `None.id`, so the owner saw "AttributeError" and an error code - nothing
    about the session having been replaced, which is what had happened.

    The cause is worth naming precisely: removing and re-adding an account writes
    a NEW session string to `.env`, and a server started earlier still holds the
    old one. Only a restart picks it up.
    """
    from telegram_mcp.tools import profile as profile_mod

    class _Unauthorised:
        async def get_me(self):
            return None

        def is_connected(self):
            return True

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: _Unauthorised())

    async def _connected(client):
        return None

    monkeypatch.setattr(profile_mod, "ensure_connected", _connected)

    answer = await profile_mod.get_me(account="acct")

    assert "AttributeError" not in answer
    assert "Restart the MCP server" in answer, "the remedy was not named"
    assert "no longer valid" in answer


def test_formatting_nothing_says_what_came_back_empty():
    """The shared guard, so no other path can produce the same opaque crash."""
    from telegram_mcp.runtime import get_marked_id

    with pytest.raises(ValueError) as raised:
        get_marked_id(None)

    assert "no longer authorised" in str(raised.value)
