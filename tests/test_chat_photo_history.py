"""replace_chat_photo: one photo left in a group's or channel's photo list (FR-009).

Telegram keeps every earlier chat photo as a "photo changed" service message, and
that is what the chat's photo list shows. Setting a new photo alone leaves them all.
"""

from types import SimpleNamespace

import pytest
from telethon.tl import functions, types

from telegram_mcp import file_roots
from telegram_mcp.tools import chat_photo_history as mod

CHANNEL = types.Channel(
    id=42, title="EVdlc Bot | Relay", photo=types.ChatPhotoEmpty(), date=None, access_hash=7
)


def _photo_message(message_id):
    return SimpleNamespace(
        id=message_id, action=types.MessageActionChatEditPhoto(photo=types.PhotoEmpty(id=0))
    )


class Client:
    def __init__(self, photo_messages=(), upload_fails=False):
        self.sent = []
        self.deleted = []
        self._photo_messages = list(photo_messages)
        self._upload_fails = upload_fails

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.channels.EditPhotoRequest):
            self._photo_messages.insert(0, _photo_message(999))  # Telegram's new one
        return True

    async def upload_file(self, handle):
        self.sent.append("upload")
        if self._upload_fails:
            raise ConnectionError("upload dropped")
        return types.InputFile(id=1, parts=1, name="p.png", md5_checksum="")

    async def get_messages(self, entity, limit=None, filter=None):
        assert filter is types.InputMessagesFilterChatPhotos
        return list(self._photo_messages)

    async def delete_messages(self, entity, ids, revoke=True):
        self.deleted.append(list(ids))
        return []


@pytest.fixture
def a_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    picture = tmp_path / "logo.png"
    picture.write_bytes(b"png")
    return str(picture)


@pytest.mark.asyncio
async def test_the_new_photo_is_set_and_every_older_one_is_deleted(wire_client, a_picture):
    client = wire_client(
        mod, Client([_photo_message(30), _photo_message(20), _photo_message(10)]), entity=CHANNEL
    )

    answer = await mod.replace_chat_photo(chat_id=-10042, file_path=a_picture)

    edit = next(
        i for i, r in enumerate(client.sent) if isinstance(r, functions.channels.EditPhotoRequest)
    )
    assert client.sent.index("upload") < edit
    assert client.deleted == [[30, 20, 10]], "exactly the photos that were there before"
    assert "3" in answer


@pytest.mark.asyncio
async def test_without_a_picture_only_the_older_photos_go(wire_client):
    client = wire_client(
        mod, Client([_photo_message(30), _photo_message(20), _photo_message(10)]), entity=CHANNEL
    )

    await mod.replace_chat_photo(chat_id=-10042)

    assert client.deleted == [[20, 10]], "the newest photo is the current one and stays"
    assert not any(isinstance(r, functions.channels.EditPhotoRequest) for r in client.sent)


@pytest.mark.asyncio
async def test_a_failed_upload_deletes_nothing(wire_client, a_picture):
    client = wire_client(mod, Client([_photo_message(30)], upload_fails=True), entity=CHANNEL)

    await mod.replace_chat_photo(chat_id=-10042, file_path=a_picture)

    assert client.deleted == []


@pytest.mark.asyncio
async def test_nothing_older_means_nothing_deleted(wire_client, a_picture):
    client = wire_client(mod, Client([]), entity=CHANNEL)

    answer = await mod.replace_chat_photo(chat_id=-10042, file_path=a_picture)

    assert client.deleted == []
    assert "0" in answer
