"""Attaching media to a rich message, where the markup names what it cannot carry.

`InputRichMessageHTML`/`Markdown` have carried a `files` list from the start and
this server never passed it, so the composer's Photo/Video, Audio and File
attachments could not be sent at all. Nothing about that failed loudly: the tag
was simply dropped and the message arrived as text.

The pairing is what these pin. Telegram matches the SCHEME the markup used
against the wrapper in the list, and its two refusals are worth telling apart:
`RICH_MESSAGE_PHOTO_URL_INVALID` means the URL form was not understood at all,
`RICH_MESSAGE_PHOTO_INVALID` means it was, and the media behind it was the wrong
kind. Both were measured against the live API.
"""

import pytest
from telethon.tl import functions, types

from telegram_mcp import file_roots, runtime
from telegram_mcp.tools import messages as messages_mod

ME = types.InputPeerUser(user_id=1, access_hash=0)


@pytest.fixture
def a_picture(tmp_path, monkeypatch):
    """A file the allowed-roots guard will accept; a bare tmp_path is refused."""
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")
    picture = tmp_path / "logo.png"
    picture.write_bytes(b"png")
    return str(picture)


class Client:
    """Records what was uploaded and how, which is the only place the pairing shows."""

    def __init__(self, as_photo=True):
        self.sent = []
        self.forced = []
        self._as_photo = as_photo

    async def _file_to_media(self, handle, force_document=False, **_kw):
        self.forced.append(force_document)
        return None, types.InputMediaUploadedPhoto(file=types.InputFile(1, 1, "l.png", "")), True

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            if self._as_photo:
                photo = types.Photo(
                    id=7,
                    access_hash=1,
                    file_reference=b"",
                    date=None,
                    sizes=[],
                    dc_id=1,
                    has_stickers=False,
                )
                return types.MessageMediaPhoto(photo=photo)
            document = types.Document(
                id=8,
                access_hash=2,
                file_reference=b"",
                date=None,
                mime_type="video/mp4",
                size=1,
                dc_id=1,
                attributes=[],
            )
            return types.MessageMediaDocument(document=document)
        return True


# ------------------------------------------------------------ the payload


def test_the_files_list_reaches_both_rich_parse_modes():
    """The field existed on the TL type all along and was never passed."""
    marker = [object()]

    assert runtime.make_rich_input("rich_html", "<p>x</p>", None, marker).files is marker
    assert runtime.make_rich_input("rich_markdown", "x", None, marker).files is marker


def test_no_attachments_still_sends_the_field_empty():
    """Rebuilding the payload must not change what an ordinary rich send looks
    like; `files` stays absent rather than becoming an empty list."""
    assert runtime.make_rich_input("rich_html", "<p>x</p>").files is None


# ------------------------------------------------ scheme decides the wrapper


@pytest.mark.asyncio
async def test_a_photo_reference_is_wrapped_as_a_photo(a_picture):
    client = Client(as_photo=True)

    files, error = await runtime.rich_message_files(
        client, ME, {"logo": a_picture}, '<img src="tg://photo?id=logo">'
    )

    assert error is None
    assert isinstance(files[0], types.InputRichFilePhoto) and files[0].id == "logo"


@pytest.mark.asyncio
async def test_a_file_reference_is_uploaded_as_a_plain_document(a_picture):
    """`tg://document?id=` is the composer's File button: the same picture has to
    arrive as something to download, not as a photo."""
    client = Client(as_photo=False)

    files, error = await runtime.rich_message_files(
        client, ME, {"notes": a_picture}, '<a href="tg://document?id=notes">notes</a>'
    )

    assert error is None
    assert isinstance(files[0], types.InputRichFileDocument)
    assert client.forced == [True]


@pytest.mark.asyncio
async def test_video_and_audio_references_keep_their_attributes(a_picture):
    """`force_document` STRIPS duration and dimensions, and Telegram refuses a
    video or a track without them. Forcing it for every non-photo scheme is
    exactly what turned two working attachments into RICH_MESSAGE_VIDEO_INVALID
    and RICH_MESSAGE_AUDIO_INVALID."""
    client = Client(as_photo=False)

    await runtime.rich_message_files(
        client,
        ME,
        {"clip": a_picture, "track": a_picture},
        '<video src="tg://video?id=clip"></video><audio src="tg://audio?id=track"></audio>',
    )

    assert client.forced == [False, False]


@pytest.mark.asyncio
async def test_an_unreferenced_attachment_keeps_its_own_type(a_picture):
    """Only the `document` scheme asks for a plain file. A name the markup never
    mentions has asked for nothing, so it is uploaded as whatever it is rather
    than flattened on a guess."""
    client = Client(as_photo=False)

    await runtime.rich_message_files(client, ME, {"stray": a_picture}, "<p>no reference</p>")

    assert client.forced == [False]


# --------------------------------------------------------- through the tool


@pytest.mark.asyncio
async def test_a_refused_path_stops_the_send_instead_of_sending_text(
    wire_client, tmp_path, a_picture
):
    """The attachment IS the message. Sending the markup anyway would deliver a
    message whose picture is silently missing and report it as sent."""
    client = wire_client(messages_mod, Client(), entity=ME)

    answer = await messages_mod.send_message(
        chat_id="me",
        message='<img src="tg://photo?id=logo">',
        parse_mode="rich_html",
        rich_files={"logo": str(tmp_path / "never-written.png")},
    )

    assert '"sent": true' not in answer.lower()
    assert not [r for r in client.sent if isinstance(r, functions.messages.SendMessageRequest)]


# ------------------------------------------------------- the autolink flag


def test_the_autolink_flag_reaches_both_rich_parse_modes():
    """The third field that was on the TL type from the start and never passed.
    Telegram links a bare URL by itself, so a message that writes one as an
    example rather than a destination has no other way to say so."""
    assert runtime.make_rich_input("rich_html", "<p>x</p>", None, None, True).noautolink is True
    assert runtime.make_rich_input("rich_markdown", "x", None, None, True).noautolink is True


def test_autolinking_stays_on_unless_it_is_turned_off():
    """Absent, not False: an unasked-for keyword changes the payload for every
    existing caller, which is how the send_as work broke five unrelated tests."""
    assert runtime.make_rich_input("rich_html", "<p>x</p>").noautolink is None
