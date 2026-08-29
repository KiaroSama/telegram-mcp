"""A topic could be created and then only ever lived with.

`create_forum_topic` and `list_topics` existed with nothing that could change a
topic afterwards — no rename, no close, no reopen. It was reported as
unbuildable because `channels.EditForumTopic` is absent from Telethon 1.44. That
was the wrong conclusion: `GetForumTopics` and `CreateForumTopic` are absent too,
and this module has hand-written wire encoders for both. The request was
writable; only the constructor id had to be established rather than guessed.

These tests pin the two things a hand-rolled TL request can get silently wrong:
the constructor id, and the flag layout. Both are checked against the schema
itself rather than against a copied constant — CRC32 covers the WHOLE definition
line, so an id that matches also proves the field order and flag numbers.
"""

import struct
import zlib

import pytest
from telethon.tl.types import InputChannel

from telegram_mcp.tools import topics as topics_mod
from telegram_mcp.tools.topics import EditForumTopicRequest

# The layer-227 schema line, normalised the way Telegram computes ids.
SCHEMA = (
    "channels.editForumTopic flags:# channel:InputChannel topic_id:int "
    "title:flags.0?string icon_emoji_id:flags.1?long closed:flags.2?Bool "
    "hidden:flags.3?Bool = Updates"
)

CHANNEL = InputChannel(channel_id=555, access_hash=7)


def _flags(request) -> int:
    """The flags word: right after the constructor id in the encoded request."""
    return struct.unpack_from("<I", request._bytes(), 4)[0]


def test_the_constructor_id_is_derived_from_the_schema_not_copied():
    """A wrong id is not a soft failure: Telegram answers a request it does not
    recognise with an error that names nothing useful."""
    assert EditForumTopicRequest.CONSTRUCTOR_ID == zlib.crc32(SCHEMA.encode()) & 0xFFFFFFFF


def test_the_derivation_reproduces_a_request_this_project_already_sends():
    """Guard the guard. If the normalisation above were wrong, the test before
    this one would pass against an equally wrong id. `GetForumTopics` is a
    request whose id was established independently and is in daily use."""
    known = (
        "channels.getForumTopics flags:# channel:InputChannel q:flags.0?string "
        "offset_date:int offset_id:int offset_topic:int limit:int = messages.ForumTopics"
    )

    assert (
        zlib.crc32(known.encode()) & 0xFFFFFFFF == topics_mod.GetForumTopicsRequest.CONSTRUCTOR_ID
    )


def test_every_field_sets_its_own_flag():
    assert _flags(EditForumTopicRequest(CHANNEL, 4, title="Renamed")) == 1 << 0
    assert _flags(EditForumTopicRequest(CHANNEL, 4, icon_emoji_id=99)) == 1 << 1
    assert _flags(EditForumTopicRequest(CHANNEL, 4, closed=True)) == 1 << 2
    assert _flags(EditForumTopicRequest(CHANNEL, 4, hidden=True)) == 1 << 3


def test_an_omitted_field_sets_no_flag_and_writes_no_bytes():
    """This is what makes it an edit and not a replace: renaming a topic must not
    clear its icon."""
    only_title = EditForumTopicRequest(CHANNEL, 4, title="Renamed")
    everything = EditForumTopicRequest(
        CHANNEL, 4, title="Renamed", icon_emoji_id=99, closed=False, hidden=False
    )

    assert _flags(only_title) == 1 << 0
    assert _flags(everything) == (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)
    assert len(only_title._bytes()) < len(everything._bytes())


def test_false_is_sent_and_is_not_the_same_as_omitted():
    """`closed=False` REOPENS a topic. Treating it as "not given" would make the
    tool unable to undo its own close - the bug a falsy check would introduce."""
    reopen = EditForumTopicRequest(CHANNEL, 4, closed=False)

    assert _flags(reopen) == 1 << 2
    # boolFalse on the wire, not an absent field.
    assert struct.pack("<I", 0xBC799737) in reopen._bytes()


def test_true_and_false_serialise_to_different_constructors():
    closed = EditForumTopicRequest(CHANNEL, 4, closed=True)._bytes()
    opened = EditForumTopicRequest(CHANNEL, 4, closed=False)._bytes()

    assert struct.pack("<I", 0x997275B5) in closed
    assert struct.pack("<I", 0xBC799737) in opened
    assert closed != opened


def test_the_encoding_round_trips():
    """`from_reader` is the inverse of `_bytes`; if the flag numbering disagreed
    between them this would not survive."""
    from telethon.extensions import BinaryReader

    original = EditForumTopicRequest(
        CHANNEL, 42, title="Planning", icon_emoji_id=7, closed=True, hidden=False
    )
    reader = BinaryReader(original._bytes()[4:])
    restored = EditForumTopicRequest.from_reader(reader)

    assert restored.topic_id == 42
    assert restored.title == "Planning"
    assert restored.icon_emoji_id == 7
    assert restored.closed is True
    assert restored.hidden is False


# --- the tool around it -----------------------------------------------------


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, request):
        self.sent.append(request)
        return None


def _wire(monkeypatch, entity):
    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return entity

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(topics_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(topics_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(topics_mod, "ensure_connected", _connected)
    return client


def _forum(forum=True):
    """A real Channel: the tool selects its branch with isinstance, so a stand-in
    class is refused before the forum check is ever reached."""
    from telethon.tl.types import Channel

    return Channel(
        id=555,
        title="A Forum",
        photo=None,
        date=None,
        creator=True,
        left=False,
        broadcast=False,
        verified=False,
        megagroup=True,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        forum=forum,
    )


@pytest.mark.asyncio
async def test_changing_nothing_is_refused_before_anything_is_sent(monkeypatch):
    client = _wire(monkeypatch, _forum())

    result = await topics_mod.edit_forum_topic(-100555, 4)

    assert client.sent == []
    assert "Nothing to change" in result


@pytest.mark.asyncio
async def test_a_rename_reaches_the_wire(monkeypatch):
    client = _wire(monkeypatch, _forum())

    await topics_mod.edit_forum_topic(-100555, 4, title="Planning")

    request = client.sent[-1]
    assert isinstance(request, EditForumTopicRequest)
    assert request.topic_id == 4
    assert request.title == "Planning"
    assert request.closed is None, "an untouched field must stay untouched"


@pytest.mark.asyncio
async def test_a_non_forum_supergroup_is_refused(monkeypatch):
    client = _wire(monkeypatch, _forum(forum=False))

    result = await topics_mod.edit_forum_topic(-100555, 4, title="x")

    assert client.sent == []
    assert "forum topics enabled" in result
