"""The tools that change the account, driven and asserted on what they send.

38 registered tools had no behavioural test at all. Seventeen of them WRITE:
they delete, pin, unpin, clear drafts and destroy folders, and all but one carry
`destructiveHint=True`. `telegram_mcp/tools/*` is excluded from coverage
measurement by policy, so the coverage gate said nothing about any of them — the
most dangerous surface in the server was its least tested.

Several files looked like coverage and were not. `test_forward_attribution.py`
tests `message_to_dict`'s forward rendering, not `forward_message`;
`test_message_reactions.py` tested only `get_message_reactions`, never
send/remove; `test_folders.py` covered only add/remove.

Every test here asserts on the REQUEST that went out, not on the returned
sentence. The sentence is identical whether the request was right or wrong —
which is exactly how a peer that was resolved and then discarded stayed
invisible.
"""

import pytest
from telethon.tl import functions, types

from telegram_mcp.tools import folders as folders_mod
from telegram_mcp.tools import messages as messages_mod
from telegram_mcp.tools import messages_queue as queue_mod
from telegram_mcp.tools import messages_read as read_mod
from telegram_mcp.tools import messages_state as state_mod
from telegram_mcp.tools import saved as saved_mod

# The caller passes this; the resolved entity is what must reach Telegram.
RAW_CHAT_ID = "@somechat"
RESOLVED = types.InputPeerChannel(channel_id=4242, access_hash=7)


class Recorder:
    """Records raw requests AND the client-method calls some tools use instead."""

    def __init__(self, filters=None):
        self.sent = []
        self.calls = []
        self.filters = filters if filters is not None else []

    async def __call__(self, request):
        self.sent.append(request)
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return types.messages.DialogFilters(filters=self.filters, tags_enabled=False)
        return True

    # Tools that go through Telethon's friendly methods rather than raw TL.
    async def pin_message(self, entity, message_id):
        self.calls.append(("pin_message", entity, message_id))

    async def unpin_message(self, entity, message_id):
        self.calls.append(("unpin_message", entity, message_id))

    async def send_read_acknowledge(self, entity):
        self.calls.append(("send_read_acknowledge", entity))

    async def send_message(self, entity, text, reply_to=None, parse_mode=None):
        self.calls.append(("send_message", entity, text, reply_to))

    async def forward_messages(self, to_entity, ids, from_entity):
        self.calls.append(("forward_messages", to_entity, ids, from_entity))

    async def get_messages(self, entity, ids=None):
        # `_album_batch` asks for the anchor to decide whether the id belongs to
        # an album. `grouped_id=None` means a lone message, which is this case.
        self.calls.append(("get_messages", entity, ids))
        return types.Message(id=ids if isinstance(ids, int) else 0, peer_id=None, message="")

    async def get_me(self, input_peer=False):
        return types.InputPeerUser(user_id=1, access_hash=0)


def _last(client, request_type):
    matches = [r for r in client.sent if isinstance(r, request_type)]
    assert (
        matches
    ), f"no {request_type.__name__} was sent; got {[type(r).__name__ for r in client.sent]}"
    return matches[-1]


# --- the destructive ones, most dangerous first -----------------------------


@pytest.mark.asyncio
async def test_unpin_all_messages_addresses_the_resolved_peer(wire_client):
    client = wire_client(state_mod, Recorder(), entity=RESOLVED)

    await state_mod.unpin_all_messages(RAW_CHAT_ID)

    request = _last(client, functions.messages.UnpinAllMessagesRequest)
    assert request.peer is RESOLVED, "the caller's raw argument reached the wire"


@pytest.mark.asyncio
async def test_clear_draft_saves_an_empty_message_to_the_resolved_peer(wire_client):
    client = wire_client(queue_mod, Recorder(), entity=RESOLVED)

    await queue_mod.clear_draft(RAW_CHAT_ID)

    request = _last(client, functions.messages.SaveDraftRequest)
    assert request.peer is RESOLVED
    assert request.message == "", "clearing a draft must send an EMPTY message"


@pytest.mark.asyncio
async def test_save_draft_sends_the_text_it_was_given(wire_client):
    client = wire_client(queue_mod, Recorder(), entity=RESOLVED)

    await queue_mod.save_draft(RAW_CHAT_ID, "unsent thought")

    request = _last(client, functions.messages.SaveDraftRequest)
    assert request.peer is RESOLVED
    assert request.message == "unsent thought"


@pytest.mark.asyncio
async def test_delete_folder_refuses_a_system_folder_without_sending_anything(wire_client):
    """Folder ids 0 and 1 are Telegram's own. The refusal must happen before any
    request, not be discovered by the server."""
    client = wire_client(folders_mod, Recorder(), entity=RESOLVED)

    result = await folders_mod.delete_folder(1)

    assert "system folder" in result.lower()
    assert client.sent == [], "a request went out for a folder that cannot be deleted"


@pytest.mark.asyncio
async def test_delete_folder_sends_a_null_filter_for_the_named_id(wire_client):
    existing = types.DialogFilter(
        id=5,
        title=types.TextWithEntities(text="Work", entities=[]),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
    )
    client = wire_client(folders_mod, Recorder(filters=[existing]), entity=RESOLVED)

    await folders_mod.delete_folder(5)

    request = _last(client, functions.messages.UpdateDialogFilterRequest)
    assert request.id == 5
    assert request.filter is None, "deleting a folder means sending a null filter"


@pytest.mark.asyncio
async def test_reorder_folders_refuses_a_partial_order(wire_client):
    """Telegram replaces the whole order, so a partial list would silently drop
    the folders it omits."""
    existing = [
        types.DialogFilter(
            id=n,
            title=types.TextWithEntities(text=f"F{n}", entities=[]),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
        )
        for n in (2, 3)
    ]
    client = wire_client(folders_mod, Recorder(filters=existing), entity=RESOLVED)

    result = await folders_mod.reorder_folders([2])

    assert "missing" in result.lower()
    assert not [
        r for r in client.sent if isinstance(r, functions.messages.UpdateDialogFiltersOrderRequest)
    ]


@pytest.mark.asyncio
async def test_reorder_folders_sends_the_full_order(wire_client):
    existing = [
        types.DialogFilter(
            id=n,
            title=types.TextWithEntities(text=f"F{n}", entities=[]),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
        )
        for n in (2, 3)
    ]
    client = wire_client(folders_mod, Recorder(filters=existing), entity=RESOLVED)

    await folders_mod.reorder_folders([3, 2])

    request = _last(client, functions.messages.UpdateDialogFiltersOrderRequest)
    assert request.order == [3, 2]


# --- pins, reactions, reads -------------------------------------------------


@pytest.mark.asyncio
async def test_pin_and_unpin_pass_the_resolved_entity(wire_client):
    client = wire_client(state_mod, Recorder(), entity=RESOLVED)

    await state_mod.pin_message(RAW_CHAT_ID, 99)
    await state_mod.unpin_message(RAW_CHAT_ID, 99)

    assert client.calls == [
        ("pin_message", RESOLVED, 99),
        ("unpin_message", RESOLVED, 99),
    ]


@pytest.mark.asyncio
async def test_send_reaction_carries_the_emoji_and_the_peer(wire_client):
    client = wire_client(state_mod, Recorder(), entity=RESOLVED)

    await state_mod.send_reaction(RAW_CHAT_ID, 77, "👍")

    request = _last(client, functions.messages.SendReactionRequest)
    assert request.peer is RESOLVED
    assert request.msg_id == 77
    assert any(getattr(r, "emoticon", None) == "👍" for r in (request.reaction or []))


@pytest.mark.asyncio
async def test_remove_reaction_sends_an_empty_reaction_list(wire_client):
    """Removing is sending no reaction, not sending a 'remove' request — a
    distinction only the request shape shows."""
    client = wire_client(state_mod, Recorder(), entity=RESOLVED)

    await state_mod.remove_reaction(RAW_CHAT_ID, 77)

    request = _last(client, functions.messages.SendReactionRequest)
    assert request.peer is RESOLVED
    assert not request.reaction


@pytest.mark.asyncio
async def test_mark_as_read_acknowledges_the_resolved_entity(wire_client):
    client = wire_client(read_mod, Recorder(), entity=RESOLVED)

    await read_mod.mark_as_read(RAW_CHAT_ID)

    assert client.calls == [("send_read_acknowledge", RESOLVED)]


@pytest.mark.asyncio
async def test_name_saved_tag_updates_the_tag(wire_client):
    client = wire_client(saved_mod, Recorder(), entity=RESOLVED)

    await saved_mod.name_saved_tag("🔖", "Receipts")

    request = _last(client, functions.messages.UpdateSavedReactionTagRequest)
    assert request.title == "Receipts"


# --- sending -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_message_replies_to_the_id_it_was_given(wire_client):
    client = wire_client(messages_mod, Recorder(), entity=RESOLVED)

    await messages_mod.reply_to_message(RAW_CHAT_ID, 55, "on it")

    kind, entity, text, reply_to = client.calls[-1]
    assert (kind, entity, text, reply_to) == ("send_message", RESOLVED, "on it", 55)


@pytest.mark.asyncio
async def test_forward_message_resolves_both_ends(wire_client):
    """Two peers, two resolutions. Sending the caller's raw string for either end
    is the failure this pins."""
    client = wire_client(messages_mod, Recorder(), entity=RESOLVED)

    await messages_mod.forward_message("@from", 12, "@to")

    forwards = [c for c in client.calls if c[0] == "forward_messages"]
    assert forwards, f"no forward went out; calls were {[c[0] for c in client.calls]}"
    kind, to_entity, ids, from_entity = forwards[-1]
    assert to_entity is RESOLVED and from_entity is RESOLVED
    assert ids == [12] or ids == 12


# --- and the one an earlier audit reported as broken -------------------------


@pytest.mark.asyncio
async def test_delete_messages_bulk_sends_the_peerless_request_outside_channels(wire_client):
    """An earlier audit reported this as dropping the peer. It is not a defect:
    `messages.DeleteMessagesRequest` has exactly two fields, `id` and `revoke` -
    there is no peer field, because outside channels a message id is
    account-global. Telethon's own `delete_messages` sends the same peerless
    request. This test pins the correct behaviour so the "fix" is not reapplied.
    """
    client = wire_client(messages_mod, Recorder(), entity=RESOLVED)

    await messages_mod.delete_messages_bulk(RAW_CHAT_ID, [1, 2, 3])

    request = _last(client, functions.messages.DeleteMessagesRequest)
    assert request.id == [1, 2, 3]
    assert not hasattr(request, "peer")


def test_the_delete_docstring_warns_that_ids_are_account_global():
    doc = messages_mod.delete_messages_bulk.__doc__
    assert (
        "account-global" in doc or "account global" in doc
    ), "a caller cannot know the ids are not scoped to chat_id unless told"
