"""Basic ("legacy") groups get the id Telegram will actually accept.

``messages.editChatTitle`` and ``messages.editChatPhoto`` take a bare TL
``long``. Telethon casts nothing there -- the request has no ``resolve()`` and
``_bytes`` does ``struct.pack('<q', self.chat_id)`` -- so whatever the caller
handed the tool goes on the wire unchanged: a marked id like ``-987654`` that
Telegram rejects, or a username that cannot even be packed.

The tools already resolve the caller's argument into an entity, so the right
number is right there. These tests assert on the *request that was sent*, not on
the returned sentence: the sentence is identical before and after the fix.

No network: the fake client records the TL request and then serializes it, which
is the step that rejects a non-integer id in production.
"""

import inspect
import re
from datetime import datetime, timezone

import pytest
from telethon.tl.types import Chat, ChatPhotoEmpty

from telegram_mcp.tools import groups as mod

# The entity `resolve_entity` hands back, and the id every request below must
# carry. A real `Chat`, not a mock: production picks the branch under test with
# `isinstance(entity, Chat)`, so a mock would silently take a different one.
BARE_ID = 987654
MARKED_ID = -BARE_ID


def _chat():
    return Chat(
        id=BARE_ID,
        title="Weekend Plans",
        photo=ChatPhotoEmpty(),
        participants_count=3,
        date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        version=1,
    )


class _Client:
    """Records each request, then packs it the way the transport would."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        request._bytes()
        return None

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def client(monkeypatch):
    fake = _Client()
    monkeypatch.setattr(mod, "get_client", lambda account=None: fake)

    async def _resolve(identifier, client=None, account=None):
        return _chat()

    monkeypatch.setattr(mod, "resolve_entity", _resolve)
    return fake


@pytest.mark.asyncio
async def test_edit_chat_title_sends_the_resolved_id_not_the_marked_one(client):
    """A marked id goes on the wire as-is, and Telegram rejects it."""
    await mod.edit_chat_title(MARKED_ID, "Weekday Plans", account="a")

    assert client.sent("EditChatTitleRequest").chat_id == BARE_ID


@pytest.mark.asyncio
async def test_delete_chat_photo_sends_the_resolved_id_not_the_marked_one(client):
    await mod.delete_chat_photo(MARKED_ID, account="a")

    assert client.sent("EditChatPhotoRequest").chat_id == BARE_ID


@pytest.mark.asyncio
async def test_a_username_still_reaches_telegram_as_the_resolved_id(client):
    """A username cannot be packed into a TL long at all, so pre-fix this is a
    `struct.error` swallowed into a generic GROUP-ERR with no hint about the
    argument shape."""
    await mod.edit_chat_title("@weekendplans", "Weekday Plans", account="a")

    assert client.sent("EditChatTitleRequest").chat_id == BARE_ID


def test_no_basic_group_request_is_built_from_the_callers_argument():
    """`edit_chat_photo` needs an upload and a configured file root to drive, so
    it is covered by shape instead -- and this catches the next request added to
    the module with the same trap.

    Scoped to request constructors: `log_and_format_error(..., chat_id=chat_id)`
    is error context, not a wire field, and is correct as written.
    """
    source = inspect.getsource(mod)

    assert not re.search(r"Request\(\s*chat_id=chat_id\b", source)
