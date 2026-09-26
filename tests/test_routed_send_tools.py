"""`send_voice` and `send_sticker` as routes into the media-kind path.

They were separate implementations that each hand-wrote their own Telethon flags.
That is three places for one decision, and the kind vocabulary exists so there is
one: a fix to what `voice_note` means on the wire has to reach every tool that
sends one, without being applied three times.

What must NOT change is either tool's own refusal. `send_voice` promises `.ogg`
or `.opus` in its docstring and callers rely on it; that is narrower than the voice
family allows and it stays narrower. The tests below are the guard against the
routing quietly loosening it.
"""

import pytest

from telegram_mcp import file_roots, media_send
from telegram_mcp.tools import media


class _Recorder:
    """Records every send, so a tool that sends twice cannot look like one."""

    def __init__(self):
        self.calls = []

    async def send_file(self, entity, handle, caption=None, reply_to=None, **flags):
        self.calls.append(
            {
                "entity": entity,
                "name": getattr(handle, "name", None),
                # Read HERE, not later. The gate closes the handle when the tool
                # returns, and Telethon reads from wherever the pointer was left,
                # so what this reads now is exactly what would have been uploaded.
                "uploaded": handle.read() if hasattr(handle, "read") else None,
                "caption": caption,
                "reply_to": reply_to,
                "flags": flags,
            }
        )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    client = _Recorder()
    # CONTENTS, not the name. `file_roots`'s own module docstring says so:
    # `runtime` and `main` star-import this list and hold second references to
    # the same object, and `refresh_server_roots()` slice-assigns into whatever
    # the module currently names. Rebinding it here emptied the REAL list for
    # every test that ran afterwards - `test_file_path_security.py` failed with
    # "disabled until allowed roots are configured" only when this file ran first.
    original = list(file_roots.SERVER_ALLOWED_ROOTS)
    file_roots.SERVER_ALLOWED_ROOTS[:] = [root]
    monkeypatch.setattr(file_roots, "refresh_server_roots", lambda: None)
    # Bare names below resolve against this root, as they would against files/.
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return f"entity:{chat_id}"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)
    yield root, client
    file_roots.SERVER_ALLOWED_ROOTS[:] = original


# --- the flags come from one place now ---------------------------------------


@pytest.mark.asyncio
async def test_send_voice_sends_exactly_what_the_vocabulary_says_a_voice_note_is(wired):
    root, client = wired
    (root / "note.ogg").write_bytes(b"OggS")

    await media.send_voice("AnyChat", "note.ogg")

    assert len(client.calls) == 1
    assert client.calls[0]["flags"] == media_send.flags_for("voice_note")


@pytest.mark.asyncio
async def test_send_sticker_sends_exactly_what_the_vocabulary_says_a_sticker_is(wired):
    root, client = wired
    (root / "face.webp").write_bytes(b"RIFF")

    await media.send_sticker("AnyChat", "face.webp")

    assert len(client.calls) == 1
    assert client.calls[0]["flags"] == media_send.flags_for("sticker")


# --- and the refusals each tool already promised still hold ------------------


@pytest.mark.parametrize("name", ["song.mp3", "clip.mp4", "notes.pdf", "photo.jpg"])
@pytest.mark.asyncio
async def test_send_voice_still_refuses_anything_that_is_not_ogg_or_opus(wired, name):
    """Narrower than the voice family allows, deliberately: the tool's contract
    already promises it and the routing must not widen it to `.mp3`."""
    root, client = wired
    (root / name).write_bytes(b"data")

    result = await media.send_voice("AnyChat", name)

    assert client.calls == []
    assert "ogg" in result.lower() and "opus" in result.lower()


@pytest.mark.parametrize("name", ["song.mp3", "face.png", "face.tgs", "notes.pdf"])
@pytest.mark.asyncio
async def test_send_sticker_still_refuses_anything_that_is_not_webp(wired, name):
    """`.webp` ONLY, and narrower than the sticker family allows - `.tgs` is a
    sticker to the vocabulary and is still refused here.

    This was written expecting the tool to pass anything through, because its
    body has no extension check at all. It does not: `EXTENSION_ALLOWLISTS` in
    `file_roots.py` refuses at the gate, before the handle is ever opened, and
    the refusal names the tool and the allowed suffix. Worth knowing before
    "adding" a check that already exists one layer down.
    """
    root, client = wired
    (root / name).write_bytes(b"data")

    result = await media.send_sticker("AnyChat", name)

    assert client.calls == []
    assert "send_sticker" in result and ".webp" in result


# --- an .ogg is read, and reading it does not damage the upload ---------------


def _ogg_bytes(*comments: str) -> bytes:
    """A whole small Ogg-shaped file: header, comment block, then payload."""
    head = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x01\x01" + b"\x00" * 16 + b"OpusTags"
    block = (4).to_bytes(4, "little") + b"test" + len(comments).to_bytes(4, "little")
    for comment in comments:
        raw = comment.encode("utf-8")
        block += len(raw).to_bytes(4, "little") + raw
    return head + block + b"PAYLOAD" * 64


@pytest.mark.asyncio
async def test_an_ogg_with_music_tags_goes_as_audio_not_as_a_voice_note(wired):
    root, client = wired
    (root / "song.ogg").write_bytes(_ogg_bytes("TITLE=A Song", "ARTIST=Someone"))

    result = await media.send_file("AnyChat", "song.ogg")

    # The flags now depend on the NAME too: an `.ogg` asked for as a track has to
    # claim a track MIME, because `audio/ogg` is Telegram's voice type.
    assert client.calls[0]["flags"] == media_send.flags_for("audio", file_name="song.ogg")
    assert client.calls[0]["flags"]["mime_type"] == "audio/vorbis"
    assert "as audio" in result


@pytest.mark.asyncio
async def test_an_ogg_with_no_tags_still_goes_as_a_voice_note(wired):
    root, client = wired
    (root / "note.ogg").write_bytes(_ogg_bytes("ENCODER=Lavf62.3.100"))

    result = await media.send_file("AnyChat", "note.ogg")

    assert client.calls[0]["flags"] == media_send.flags_for("voice_note")
    assert "as voice_note" in result


@pytest.mark.asyncio
async def test_reading_the_header_leaves_the_whole_file_to_upload(wired):
    """The one real hazard in peeking: Telethon uploads from wherever the file
    pointer was left, so a peek that forgets to rewind truncates the file it was
    inspecting - and the send still reports success."""
    root, client = wired
    payload = _ogg_bytes("TITLE=A Song")
    (root / "song.ogg").write_bytes(payload)

    await media.send_file("AnyChat", "song.ogg")

    assert client.calls[0]["uploaded"] == payload, (
        "the peek left the file pointer past the header, so Telethon would have "
        "uploaded a truncated file and still reported success"
    )


# --- a caption can carry its own formatting ----------------------------------


@pytest.mark.asyncio
async def test_a_caption_carries_the_entities_it_was_given(wired):
    """`send_file` was the one sending path with no entity argument, so a photo
    whose caption needed a premium emoji had to be sent and then EDITED - which
    marks the message "edited" in every client, on an ad the operator wanted to
    look untouched. Telegram has no such limit and neither does Telethon, whose
    `send_file` takes `formatting_entities`; only this tool did.
    """
    root, client = wired
    (root / "pic.jpg").write_bytes(b"\xff\xd8\xff")

    await media.send_file(
        "AnyChat",
        "pic.jpg",
        caption="hello",
        caption_entities=[{"type": "bold", "offset": 0, "length": 5}],
        account=None,
    )

    sent = client.calls[0]["flags"].get("formatting_entities")
    assert sent, "the caption went out with no entities at all"
    assert len(sent) == 1
    assert type(sent[0]).__name__ == "MessageEntityBold"


@pytest.mark.asyncio
async def test_a_caption_with_no_entities_sends_none(wired):
    """The argument is optional and its absence must not change the call every
    existing caller already makes."""
    root, client = wired
    (root / "pic.jpg").write_bytes(b"\xff\xd8\xff")

    await media.send_file("AnyChat", "pic.jpg", caption="hello", account=None)

    assert "formatting_entities" not in client.calls[0]["flags"]


@pytest.mark.asyncio
async def test_a_malformed_caption_entity_refuses_before_uploading(wired):
    """The same refusal the other sending paths give, rather than a caption that
    silently arrives unformatted."""
    root, client = wired
    (root / "pic.jpg").write_bytes(b"\xff\xd8\xff")

    result = await media.send_file(
        "AnyChat",
        "pic.jpg",
        caption="hello",
        caption_entities=[{"type": "bold", "offset": 0, "length": 999}],
        account=None,
    )

    assert client.calls == []
    assert "999" in result or "length" in result.lower() or "span" in result.lower()
