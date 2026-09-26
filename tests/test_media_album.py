import pytest

from telegram_mcp import file_roots
from telegram_mcp.tools import media


class _DummyClient:
    """Reads what it was handed while it still holds it.

    The gate hands over an OPEN file and closes it when the tool returns, so
    a handle inspected after the call is a closed one. Telethon reads during
    the call; so does this.
    """

    def __init__(self):
        self.sent = None
        # Every call, not just the last. One request can now become several
        # messages, and a recorder that keeps only the newest cannot tell a
        # split that worked from one that silently dropped a message.
        self.calls = []

    async def send_file(self, entity, file_paths, caption=None, reply_to=None, **flags):
        # `**flags` because production now names the media kind: `send_file`
        # passes `force_document` and friends on every call. A double that
        # accepts only what one caller used to send fails the moment the
        # caller learns a new argument, and says TypeError rather than why.
        given = file_paths if isinstance(file_paths, list) else [file_paths]
        self.sent = {
            "entity": entity,
            "file_paths": file_paths,
            "names": [handle.name for handle in given],
            "bytes": [handle.read() for handle in given],
            "caption": caption,
            "reply_to": reply_to,
            "flags": flags,
        }
        self.calls.append(self.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["send_album", "send_file"])
async def test_album_mode_sends_multiple_files_as_one_media_group(
    tmp_path, monkeypatch, tool_name
):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    first = root / "one.png"
    second = root / "two.png"
    first.write_bytes(b"png-one")
    second.write_bytes(b"png-two")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        assert chat_id == "AgenticAIChat"
        assert cl is client
        return "entity:AgenticAIChat"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)

    tool = getattr(media, tool_name)
    result = await tool(
        "AgenticAIChat",
        ["one.png", str(second)],
        caption="pick one",
    )

    assert result == ("Sent 1 message to chat AgenticAIChat: one.png as photo, two.png as photo.")
    assert client.sent["entity"] == "entity:AgenticAIChat"
    assert client.sent["caption"] == "pick one"
    assert client.sent["reply_to"] is None
    # Open handles, not names. Telethon reopening a path is exactly the
    # second lookup the gate exists to remove.
    assert client.sent["names"] == [first.name, second.name]
    assert client.sent["bytes"] == [b"png-one", b"png-two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["send_album", "send_file"])
async def test_album_mode_passes_topic_id_as_reply_to(tmp_path, monkeypatch, tool_name):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    first = root / "one.png"
    second = root / "two.png"
    first.write_bytes(b"png-one")
    second.write_bytes(b"png-two")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity:forum"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)

    tool = getattr(media, tool_name)
    result = await tool(
        "ForumChat",
        ["one.png", str(second)],
        caption="topic post",
        topic_id=777,
    )

    assert result == ("Sent 1 message to chat ForumChat: one.png as photo, two.png as photo.")
    assert client.sent["reply_to"] == 777


@pytest.mark.asyncio
async def test_send_file_passes_topic_id_as_reply_to(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    path = root / "doc.pdf"
    path.write_bytes(b"%PDF")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity:forum"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)

    result = await media.send_file("ForumChat", "doc.pdf", caption="hello", topic_id=42)

    # The kind is named in the reply now: a send that inferred one has to say
    # which, or the caller cannot tell a document from a photo it did not choose.
    assert result == f"File sent to chat ForumChat from {path} as document."
    assert client.sent["flags"] == {"force_document": True}
    assert client.sent["entity"] == "entity:forum"
    assert client.sent["caption"] == "hello"
    assert client.sent["reply_to"] == 42
    assert client.sent["names"] == ["doc.pdf"]
    assert client.sent["bytes"] == [b"%PDF"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_paths", "expected"),
    [
        ("not-a-list", "file_paths must be a list of file paths."),
        (["one.png"], "Albums must contain between 2 and 10 files."),
        ([f"{index}.png" for index in range(11)], "Albums must contain between 2 and 10 files."),
    ],
)
async def test_send_album_validates_album_file_count(file_paths, expected, monkeypatch):
    monkeypatch.setattr(media, "clients", {"default": _DummyClient()})

    result = await media.send_album("AgenticAIChat", file_paths)

    assert result == expected


@pytest.mark.asyncio
async def test_send_album_reuses_readable_path_security(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    outside = (tmp_path / "outside").resolve()
    root.mkdir()
    outside.mkdir()
    (root / "one.png").write_bytes(b"png-one")
    outside_file = outside / "two.png"
    outside_file.write_bytes(b"png-two")

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": _DummyClient()})

    result = await media.send_album("AgenticAIChat", ["one.png", str(outside_file)])

    assert result == "Path is outside allowed roots."


@pytest.mark.asyncio
async def test_a_file_and_a_photo_are_sent_as_two_messages(tmp_path, monkeypatch):
    """`force_document` belongs to the media group, not to a file inside it, so
    "this one as a file, that one compressed" cannot be one message. Telegram's
    own clients split it; refusing the request or silently re-typing one of the
    two were the alternatives, and both lose what the caller asked for."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "one.png").write_bytes(b"png-one")
    (root / "two.png").write_bytes(b"png-two")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity:split"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)

    result = await media.send_file("SplitChat", ["one.png", "two.png"], kind=["photo", "document"])

    assert len(client.calls) == 2
    assert client.calls[0]["names"] == ["one.png"]
    assert client.calls[0]["flags"] == {"force_document": False}
    assert client.calls[1]["names"] == ["two.png"]
    assert client.calls[1]["flags"] == {"force_document": True}
    # Both messages named, in the order they were asked for.
    assert result == ("Sent 2 messages to chat SplitChat: one.png as photo; two.png as document.")


@pytest.mark.asyncio
async def test_the_caption_rides_the_first_message_only(tmp_path, monkeypatch):
    """Telegram shows an album caption on its first item. Repeating it on each
    split message would put text in the chat the caller never wrote."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "one.png").write_bytes(b"png-one")
    (root / "two.pdf").write_bytes(b"%PDF")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, cl):
        return "entity:split"

    monkeypatch.setattr(media, "resolve_entity", _resolve_entity)

    await media.send_file("SplitChat", ["one.png", "two.pdf"], caption="look")

    assert [call["caption"] for call in client.calls] == ["look", None]


@pytest.mark.asyncio
async def test_a_kind_list_of_the_wrong_length_sends_nothing(tmp_path, monkeypatch):
    """Zipping it short would send the tail as something nobody asked for, and
    the caller would have no way to find out which files those were."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "one.png").write_bytes(b"png-one")
    (root / "two.png").write_bytes(b"png-two")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    result = await media.send_file("SplitChat", ["one.png", "two.png"], kind=["photo"])

    assert client.calls == []
    assert "1 entries for 2 files" in result and "Nothing was sent" in result


@pytest.mark.asyncio
async def test_one_impossible_kind_leaves_the_others_unsent(tmp_path, monkeypatch):
    """The refusal happens while the sources are being opened, before the entity
    is resolved - so a bad third entry does not leave the first two in the chat."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "one.png").write_bytes(b"png-one")
    (root / "song.mp3").write_bytes(b"ID3")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    result = await media.send_file(
        "SplitChat", ["one.png", "song.mp3"], kind=["photo", "video_note"]
    )

    assert client.calls == []
    assert "song.mp3" in result and "video_note" in result


@pytest.mark.asyncio
async def test_a_single_file_refusal_reaches_the_caller_in_words(tmp_path, monkeypatch):
    """Found by the album test above, and it was broken on the single-file path
    too: `resolve_kind` raised, the tool's generic handler caught it, and the
    caller got "An error occurred (code: GEN-ERR-###)" instead of the sentence
    naming the file and the kind. A refusal nobody can read is a refusal that
    costs the caller a round trip to a log file they cannot see."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "song.mp3").write_bytes(b"ID3")

    client = _DummyClient()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(file_roots, "_relative_base", lambda: root)
    monkeypatch.setattr(media, "clients", {"default": client})
    monkeypatch.setattr(media, "get_client", lambda account=None: client)

    result = await media.send_file("AnyChat", "song.mp3", kind="video_note")

    assert client.calls == []
    assert "song.mp3" in result and "video_note" in result
    assert "GEN-ERR" not in result
