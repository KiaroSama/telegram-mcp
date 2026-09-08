"""Two identity questions Telegram answers with a flag, not with an error.

**Whose profile photo is this?** `photos.uploadProfilePhoto` carries an optional
`bot` field. Omit it and the picture lands on the CALLER's own profile - so a
request meant for a bot that quietly loses the flag replaces the owner's face
with the bot's logo and reports success. Asserted on the request.

**Where does a forward land, and who sent it?** Telethon's `forward_messages`
helper has neither `top_msg_id` nor `send_as`, so both need the raw request. A
forward without `top_msg_id` arrives in the destination's General topic rather
than the topic asked for, and one without `send_as` arrives under the caller's
own name - neither is an error, both are the wrong place or the wrong person.
"""

import pytest
from telethon.tl import functions, types

from telegram_mcp import file_roots
from telegram_mcp.tools import messages as messages_mod
from telegram_mcp.tools import profile as profile_mod


@pytest.fixture
def a_picture(tmp_path, monkeypatch):
    """An image the file-root guard will actually accept.

    Uploading is guarded by the allowed-roots check, so a bare `tmp_path` is
    refused before the request is ever built - and a test that never reaches
    the request cannot say anything about the flag on it.
    """
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    picture = tmp_path / "photo.jpg"
    picture.write_bytes(b"jpeg")
    return str(picture)


ME = types.InputPeerUser(user_id=1, access_hash=0)
BOT = types.InputPeerUser(user_id=777, access_hash=9)
SOURCE = types.InputPeerChannel(channel_id=11, access_hash=1)
DEST = types.InputPeerChannel(channel_id=22, access_hash=2)
AS_CHANNEL = types.InputPeerChannel(channel_id=33, access_hash=3)


class Client:
    """Records the requests, which is the only place whose/where shows."""

    def __init__(self, photos=None):
        self.sent = []
        self.forwarded = []
        self.uploaded = 0
        self._photos = photos or []

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.photos.GetUserPhotosRequest):
            return types.photos.Photos(photos=self._photos, users=[])
        return True

    async def upload_file(self, handle):
        self.uploaded += 1
        return types.InputFile(id=1, parts=1, name="p.jpg", md5_checksum="")

    async def forward_messages(self, entity, messages, from_peer, **kwargs):
        self.forwarded.append((entity, messages, from_peer, kwargs))
        return []


def _resolver(mapping):
    async def resolve(chat_id, _cl=None, _account=None):
        return mapping[chat_id]

    return resolve


def _last(client, kind):
    matches = [r for r in client.sent if isinstance(r, kind)]
    assert matches, f"no {kind.__name__} was sent"
    return matches[-1]


# ------------------------------------------------------------ profile photo


@pytest.mark.asyncio
async def test_a_bot_photo_carries_the_bot_flag(wire_client, a_picture):
    """Without the flag this replaces the OWNER's photo and says it worked."""
    client = wire_client(profile_mod, Client(), resolve=_resolver({"@mybot": BOT}))

    answer = await profile_mod.set_profile_photo(file_path=a_picture, bot="@mybot")

    request = _last(client, functions.photos.UploadProfilePhotoRequest)
    assert request.bot is BOT, "the request would have hit the caller's own profile"
    assert "@mybot" in answer


@pytest.mark.asyncio
async def test_the_owners_own_photo_sends_no_bot_flag_at_all(wire_client, a_picture):
    """Present-but-None optionals have bitten this codebase before (`send_as`),
    so the argument is omitted rather than passed empty."""
    client = wire_client(profile_mod, Client(), entity=ME)

    await profile_mod.set_profile_photo(file_path=a_picture)

    assert _last(client, functions.photos.UploadProfilePhotoRequest).bot is None


@pytest.mark.asyncio
async def test_removing_a_bot_photo_sets_an_empty_one_rather_than_deleting_by_id(
    wire_client,
):
    """A bot's photo is not in the caller's own photo list, so there is nothing
    to delete by id - `photos.deletePhotos` would silently remove the WRONG
    picture or none at all."""
    client = wire_client(profile_mod, Client(), resolve=_resolver({"@mybot": BOT}))

    await profile_mod.delete_profile_photo(bot="@mybot")

    request = _last(client, functions.photos.UpdateProfilePhotoRequest)
    assert isinstance(request.id, types.InputPhotoEmpty)
    assert request.bot is BOT
    assert not [r for r in client.sent if isinstance(r, functions.photos.DeletePhotosRequest)]


@pytest.mark.asyncio
async def test_removing_your_own_photo_still_deletes_by_id(wire_client):
    photo = types.Photo(
        id=5, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1, has_stickers=False
    )
    client = wire_client(profile_mod, Client(photos=[photo]), entity=ME)

    answer = await profile_mod.delete_profile_photo()

    assert "deleted" in answer
    assert _last(client, functions.photos.DeletePhotosRequest).id == [photo]


@pytest.mark.asyncio
async def test_no_photo_to_remove_is_said_plainly(wire_client):
    client = wire_client(profile_mod, Client(photos=[]), entity=ME)

    assert "No profile photo" in await profile_mod.delete_profile_photo()
    assert not [r for r in client.sent if isinstance(r, functions.photos.DeletePhotosRequest)]


# --------------------------------------------------------- forward routing


@pytest.fixture
def forwarding(wire_client, monkeypatch):
    async def _album(_cl, _entity, message_id, _expand):
        return ([message_id] if isinstance(message_id, int) else message_id), False

    monkeypatch.setattr(messages_mod, "_album_batch", _album)
    return wire_client(
        messages_mod,
        Client(),
        resolve=_resolver({"@sourcechat": SOURCE, "@destchat": DEST, "@aschannel": AS_CHANNEL}),
    )


@pytest.mark.asyncio
async def test_a_forward_into_a_topic_carries_the_topic_id(forwarding):
    """Without it the forward lands in General, which is a different place and
    reports success either way."""
    await messages_mod.forward_message(
        from_chat_id="@sourcechat", message_id=5, to_chat_id="@destchat", topic_id=42
    )

    assert _last(forwarding, functions.messages.ForwardMessagesRequest).top_msg_id == 42


@pytest.mark.asyncio
async def test_a_forward_can_be_posted_under_a_channels_identity(forwarding):
    await messages_mod.forward_message(
        from_chat_id="@sourcechat", message_id=5, to_chat_id="@destchat", send_as="@aschannel"
    )

    assert _last(forwarding, functions.messages.ForwardMessagesRequest).send_as is AS_CHANNEL


@pytest.mark.asyncio
async def test_every_forwarded_message_gets_its_own_random_id(forwarding):
    """Telegram deduplicates on random_id: one repeated value silently drops
    every copy after the first, and the answer still says three were sent."""
    await messages_mod.forward_message(
        from_chat_id="@sourcechat", message_id=[1, 2, 3], to_chat_id="@destchat", topic_id=7
    )

    ids = _last(forwarding, functions.messages.ForwardMessagesRequest).random_id
    assert len(ids) == 3 and len(set(ids)) == 3


@pytest.mark.asyncio
async def test_an_ordinary_forward_still_goes_through_telethons_helper(forwarding):
    """The helper resolves peers, groups albums and picks random ids. Routing is
    the only reason to bypass it, so an unrouted forward must not."""
    await messages_mod.forward_message(
        from_chat_id="@sourcechat", message_id=5, to_chat_id="@destchat"
    )

    assert forwarding.forwarded, "the helper path was abandoned for the raw request"
    assert not [
        r for r in forwarding.sent if isinstance(r, functions.messages.ForwardMessagesRequest)
    ]


@pytest.mark.asyncio
async def test_drop_author_and_silent_reach_the_helper_too(forwarding):
    """Both are supported by the helper, so they must not force the raw path."""
    await messages_mod.forward_message(
        from_chat_id="@sourcechat",
        message_id=5,
        to_chat_id="@destchat",
        drop_author=True,
        silent=True,
    )

    _entity, _ids, _from, kwargs = forwarding.forwarded[-1]
    assert kwargs["drop_author"] is True and kwargs["silent"] is True


@pytest.mark.asyncio
async def test_defaults_are_omitted_rather_than_sent_as_false(forwarding):
    """Not merely False - ABSENT. An unasked-for keyword changes this call's
    signature for every existing caller, which is how the `send_as` work broke
    five unrelated tests last time; `test_writing_tools.py`'s recorder pins the
    old shape on purpose and is right to."""
    await messages_mod.forward_message(
        from_chat_id="@sourcechat", message_id=5, to_chat_id="@destchat"
    )

    _entity, _ids, _from, kwargs = forwarding.forwarded[-1]
    assert kwargs == {}, f"an unasked-for keyword reached the helper: {kwargs}"
