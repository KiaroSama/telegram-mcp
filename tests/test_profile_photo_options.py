"""Photo history of the account or an owned bot: list with dates, delete one, replace.

Asserted on the requests and their ORDER, because the one promise that matters in
a replace is that nothing is deleted before the new picture is in place.
"""

from datetime import datetime, timezone

import pytest
from telethon.tl import functions, types

from telegram_mcp import file_roots
from telegram_mcp.tools import profile as profile_mod

ME = types.InputPeerUser(user_id=1, access_hash=0)
BOT = types.InputPeerUser(user_id=777, access_hash=9)
WHEN = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _photo(photo_id):
    return types.Photo(
        id=photo_id,
        access_hash=1,
        file_reference=b"",
        date=WHEN,
        sizes=[],
        dc_id=1,
        has_stickers=False,
    )


class Client:
    def __init__(self, photos=(), deleted=None, upload_fails=False):
        self.sent = []
        self._photos = list(photos)
        self._deleted = deleted  # None: Telegram deletes whatever it is asked to
        self._upload_fails = upload_fails

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.photos.GetUserPhotosRequest):
            return types.photos.Photos(photos=self._photos[: request.limit], users=[])
        if isinstance(request, functions.photos.DeletePhotosRequest):
            return [p.id for p in request.id] if self._deleted is None else self._deleted
        return True

    async def upload_file(self, handle):
        self.sent.append("upload")
        if self._upload_fails:
            raise ConnectionError("upload dropped")
        return types.InputFile(id=1, parts=1, name="p.jpg", md5_checksum="")


@pytest.fixture
def a_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    picture = tmp_path / "photo.jpg"
    picture.write_bytes(b"jpeg")
    return str(picture)


def _resolve(chat_id, _cl=None, _account=None):
    async def _r():
        return BOT if chat_id == "@mybot" else ME

    return _r()


def _deletes(client):
    return [r for r in client.sent if isinstance(r, functions.photos.DeletePhotosRequest)]


# ------------------------------------------------------------------- listing


@pytest.mark.asyncio
async def test_listed_photos_carry_their_date(wire_client):
    wire_client(profile_mod, Client(photos=[_photo(5), _photo(6)]), resolve=_resolve)

    answer = await profile_mod.get_user_photos("me")

    assert '"photo_id": 5' in answer
    assert "2026-09-26T12:00:00+00:00" in answer


# ------------------------------------------------------------ delete by id


@pytest.mark.asyncio
async def test_deleting_by_id_removes_exactly_that_photo(wire_client):
    client = wire_client(profile_mod, Client(photos=[_photo(5), _photo(6)]), resolve=_resolve)

    answer = await profile_mod.delete_profile_photo(photo_id=6)

    (request,) = _deletes(client)
    assert [p.id for p in request.id] == [6]
    assert "6" in answer and "deleted" in answer


@pytest.mark.asyncio
async def test_an_id_not_in_the_list_deletes_nothing(wire_client):
    client = wire_client(profile_mod, Client(photos=[_photo(5)]), resolve=_resolve)

    answer = await profile_mod.delete_profile_photo(photo_id=99)

    assert _deletes(client) == []
    assert "99" in answer


@pytest.mark.asyncio
async def test_telegram_deleting_nothing_is_not_reported_as_deleted(wire_client):
    """`deletePhotos` answers with the ids it removed; an empty answer is a no."""
    client = wire_client(profile_mod, Client(photos=[_photo(5)], deleted=[]), resolve=_resolve)

    answer = await profile_mod.delete_profile_photo(bot="@mybot", photo_id=5)

    assert _deletes(client)
    assert "did not delete" in answer


# ------------------------------------------------------------------- replace


@pytest.mark.asyncio
async def test_replace_uploads_first_then_deletes_exactly_the_previous_photo(
    wire_client, a_picture
):
    client = wire_client(profile_mod, Client(photos=[_photo(5), _photo(4)]), resolve=_resolve)

    answer = await profile_mod.set_profile_photo(file_path=a_picture, replace=True)

    upload = next(
        i
        for i, r in enumerate(client.sent)
        if isinstance(r, functions.photos.UploadProfilePhotoRequest)
    )
    delete = next(
        i for i, r in enumerate(client.sent) if isinstance(r, functions.photos.DeletePhotosRequest)
    )
    assert upload < delete
    assert [p.id for p in client.sent[delete].id] == [5]
    assert "replaced" in answer


@pytest.mark.asyncio
async def test_replace_with_no_previous_photo_is_a_plain_add(wire_client, a_picture):
    client = wire_client(profile_mod, Client(photos=[]), resolve=_resolve)

    await profile_mod.set_profile_photo(file_path=a_picture, replace=True)

    assert _deletes(client) == []
    assert any(isinstance(r, functions.photos.UploadProfilePhotoRequest) for r in client.sent)


@pytest.mark.asyncio
async def test_a_failed_upload_leaves_the_old_photo_alone(wire_client, a_picture):
    client = wire_client(
        profile_mod, Client(photos=[_photo(5)], upload_fails=True), resolve=_resolve
    )

    await profile_mod.set_profile_photo(file_path=a_picture, replace=True)

    assert _deletes(client) == []


@pytest.mark.asyncio
async def test_a_previous_photo_that_stays_is_said_plainly(wire_client, a_picture):
    wire_client(profile_mod, Client(photos=[_photo(5)], deleted=[]), resolve=_resolve)

    answer = await profile_mod.set_profile_photo(file_path=a_picture, bot="@mybot", replace=True)

    assert "not removed" in answer
    assert "5" in answer


@pytest.mark.asyncio
async def test_without_replace_nothing_is_deleted(wire_client, a_picture):
    client = wire_client(profile_mod, Client(photos=[_photo(5)]), resolve=_resolve)

    await profile_mod.set_profile_photo(file_path=a_picture)

    assert _deletes(client) == []
    assert not any(isinstance(r, functions.photos.GetUserPhotosRequest) for r in client.sent)
