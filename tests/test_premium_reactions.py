"""Reacting with a premium emoji — the other half of a read that already worked.

`get_message_reactions` has always reported custom-emoji reactions as
`custom:<document_id>`, and `inspect_message` reports `custom_emoji_id` on
entities. Nothing could SEND one: `send_reaction` built a `ReactionEmoji` from a
string and had no route to `ReactionCustomEmoji` at all — the same read/write
asymmetry the forum-topic work turned up, in a different subsystem.

Telegram gates two things behind Premium here: a custom-emoji reaction, and more
than one reaction at once. Both come back as RPC errors that name nothing a
caller can act on, so both are translated.
"""

from types import SimpleNamespace

import pytest
import telethon
from telethon.tl import functions
from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji

from telegram_mcp.tools import messages_state as state_mod

PEER = SimpleNamespace(channel_id=42)


class Recorder:
    def __init__(self, raises=None):
        self.sent = []
        self.raises = raises

    async def __call__(self, request):
        self.sent.append(request)
        if self.raises is not None:
            raise self.raises
        return None


@pytest.fixture
def wired(monkeypatch):
    client = Recorder()

    async def _resolve_input(chat_id, cl=None, account=None):
        return PEER

    monkeypatch.setattr(state_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(state_mod, "resolve_input_entity", _resolve_input)
    return client


def _reactions(client):
    return client.sent[-1].reaction


@pytest.mark.asyncio
async def test_a_standard_emoji_still_works(wired):
    """Guard the guard: the common case must not have been broken by widening."""
    await state_mod.send_reaction(-100042, 7, "👍")

    request = wired.sent[-1]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.msg_id == 7
    assert _reactions(wired) == [ReactionEmoji(emoticon="👍")]


@pytest.mark.asyncio
async def test_a_premium_emoji_is_sent_as_a_custom_reaction(wired):
    await state_mod.send_reaction(-100042, 7, custom_emoji_id=5361234567890123456)

    (reaction,) = _reactions(wired)
    assert isinstance(reaction, ReactionCustomEmoji)
    assert reaction.document_id == 5361234567890123456


@pytest.mark.asyncio
async def test_several_custom_emoji_can_go_at_once(wired):
    await state_mod.send_reaction(-100042, 7, custom_emoji_id=[111, 222])

    sent = _reactions(wired)
    assert [r.document_id for r in sent] == [111, 222]


@pytest.mark.asyncio
async def test_a_standard_and_a_premium_emoji_together(wired):
    await state_mod.send_reaction(-100042, 7, emoji="🔥", custom_emoji_id=333)

    sent = _reactions(wired)
    assert isinstance(sent[0], ReactionEmoji) and sent[0].emoticon == "🔥"
    assert isinstance(sent[1], ReactionCustomEmoji) and sent[1].document_id == 333


@pytest.mark.asyncio
async def test_reacting_with_nothing_is_refused_before_a_request(wired):
    """An empty reaction list is how `remove_reaction` CLEARS reactions. Sending
    one from here would silently delete the caller's existing reaction instead of
    adding anything."""
    result = await state_mod.send_reaction(-100042, 7)

    assert wired.sent == []
    assert "Nothing to react with" in result


@pytest.mark.asyncio
async def test_the_big_flag_reaches_the_request(wired):
    await state_mod.send_reaction(-100042, 7, "👍", big=True)

    assert wired.sent[-1].big is True


@pytest.mark.asyncio
async def test_a_premium_refusal_is_a_sentence_not_an_rpc_name(monkeypatch):
    """Telegram answers this with a name nobody can act on. The tool says which
    gate was hit and what still works without Premium."""

    class Premium(telethon.errors.RPCError):
        def __init__(self):
            self.message = "PREMIUM_ACCOUNT_REQUIRED"
            self.code = 403

        def __str__(self):
            return self.message

    client = Recorder(raises=Premium())

    async def _resolve_input(chat_id, cl=None, account=None):
        return PEER

    monkeypatch.setattr(state_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(state_mod, "resolve_input_entity", _resolve_input)

    result = await state_mod.send_reaction(-100042, 7, custom_emoji_id=999)

    assert "Premium" in result
    assert "custom (premium) emoji" in result
    assert "PREMIUM_ACCOUNT_REQUIRED" not in result, "the raw RPC name leaked out"


@pytest.mark.asyncio
async def test_the_multiple_reaction_refusal_names_that_gate_instead(monkeypatch):
    class Premium(telethon.errors.RPCError):
        def __init__(self):
            self.message = "REACTIONS_TOO_MANY"
            self.code = 400

        def __str__(self):
            return self.message

    client = Recorder(raises=Premium())

    async def _resolve_input(chat_id, cl=None, account=None):
        return PEER

    monkeypatch.setattr(state_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(state_mod, "resolve_input_entity", _resolve_input)
    monkeypatch.setattr(state_mod, "is_premium_rpc_error", lambda e: True)

    result = await state_mod.send_reaction(-100042, 7, emoji="🔥", custom_emoji_id=333)

    assert "more than one reaction at once" in result


def test_the_read_side_and_the_write_side_speak_the_same_language():
    """`get_message_reactions` reports `custom:<document_id>`; that id is exactly
    what `send_reaction` now accepts back. A round trip an agent can actually
    make."""
    import inspect

    source = inspect.getsource(state_mod)

    assert 'f"custom:{reaction.reaction.document_id}"' in source
    assert "ReactionCustomEmoji(document_id=" in source


# --- the same gap, one door over --------------------------------------------


@pytest.mark.asyncio
async def test_a_story_can_be_reacted_to_with_a_premium_emoji(monkeypatch):
    from telegram_mcp.tools import stories as stories_mod

    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return PEER

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(stories_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(stories_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(stories_mod, "ensure_connected", _connected)

    await stories_mod.react_to_story(-100042, 5, custom_emoji_id=777)

    reaction = client.sent[-1].reaction
    assert isinstance(reaction, ReactionCustomEmoji)
    assert reaction.document_id == 777


@pytest.mark.asyncio
async def test_a_story_takes_one_reaction_so_both_at_once_is_refused(monkeypatch):
    """Unlike a message, a story carries a single reaction. Sending the pair
    would be rejected by Telegram; it is refused here with a reason instead."""
    from telegram_mcp.tools import stories as stories_mod

    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return PEER

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(stories_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(stories_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(stories_mod, "ensure_connected", _connected)

    result = await stories_mod.react_to_story(-100042, 5, emoji="🔥", custom_emoji_id=777)

    assert client.sent == []
    assert "one reaction" in result


@pytest.mark.asyncio
async def test_omitting_both_still_removes_a_story_reaction(monkeypatch):
    """Guard the guard: the widening must not have broken the documented way to
    take a reaction back."""
    from telethon.tl.types import ReactionEmpty

    from telegram_mcp.tools import stories as stories_mod

    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return PEER

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(stories_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(stories_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(stories_mod, "ensure_connected", _connected)

    await stories_mod.react_to_story(-100042, 5)

    assert isinstance(client.sent[-1].reaction, ReactionEmpty)
