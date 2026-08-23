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

from telegram_mcp.tools import profile as mod
from telegram_mcp.tools.profile import delete_profile_photo, set_profile_photo, update_profile


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, photos=()):
        self.requests = []
        self.photos = list(photos)
        self.uploads = []

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetUserPhotosRequest":
            return SimpleNamespace(photos=self.photos)
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
def _wire(monkeypatch):
    def wire(client, path_error=None, resolved="C:/roots/avatar.jpg"):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve_path(*, raw_path, ctx, tool_name):
            return (None, path_error) if path_error else (resolved, None)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "_resolve_readable_file_path", _resolve_path)
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
    client = _wire(_Client(), resolved="C:/roots/avatar.jpg")

    await set_profile_photo("avatar.jpg", account="a")

    assert client.uploads == ["C:/roots/avatar.jpg"], "the unresolved argument was uploaded"
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
