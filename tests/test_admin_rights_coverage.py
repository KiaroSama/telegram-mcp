"""Every admin right Telegram has, reachable through the tools that grant them.

`promote_admin` hand-wrote twelve fields into `ChatAdminRights`. Telethon 1.44
carries **seventeen**. The five it never constructed - `post_stories`,
`edit_stories`, `delete_stories`, `manage_direct_messages` and `manage_ranks` -
could not be granted by any caller, however complete a `rights` dict it passed:
the keys were simply read past. The failure was invisible from the server side.
It showed up only in Telegram's own admin panel, as "Manage stories 0/3" and two
switches sitting off after a promotion that had reported success.

`demote_admin` had the mirror image: it set twelve fields to False and left the
other five untouched, so a "demotion" could leave story and direct-message
rights standing.

The fix is not a longer list - a longer list falls behind the next time Telegram
adds a right. It is to build the rights object FROM the installed type, which is
what `_build_admin_rights` does and what these tests pin.

That held until Telethon stopped moving. The project was archived in February
2026 at 1.44, which stops at `manage_ranks` (flags.18); layer 229 has since
added `manage_linked_peers` (flags.19) and `manage_welcome_messages`
(flags.20). So the installed type is now a FLOOR, not the whole truth, and the
two later rights are named by hand - the one thing this file was written to
avoid, now unavoidable, and therefore pinned at the only level that proves it:
the bytes on the wire.
"""

import inspect

import pytest
from telethon.tl.types import ChatAdminRights

from telegram_mcp.tools import moderation as moderation_mod

# The two Telegram added after Telethon's final release.
_LATER_THAN_TELETHON = {"manage_linked_peers", "manage_welcome_messages"}


def _telethon_fields():
    return {
        name for name in inspect.signature(ChatAdminRights.__init__).parameters if name != "self"
    }


def _all_fields():
    """Every right this server can grant, whether or not Telethon knows it."""
    return set(moderation_mod._admin_rights_fields())


def _flags_on_the_wire(rights):
    """The flags int Telegram will actually receive.

    `ChatAdminRights` is a payload-free flags object, so its whole serialised
    form is the constructor id followed by this one integer. Reading it back is
    the only check that distinguishes a right that was set from a right that was
    merely stored on a Python object and then dropped.
    """
    raw = bytes(rights)
    assert len(raw) == 8, f"expected id+flags, got {len(raw)} bytes"
    assert int.from_bytes(raw[:4], "little") == ChatAdminRights.CONSTRUCTOR_ID, (
        "constructor id changed - the hand-added flag bits below are only valid "
        "for the layout they were read from"
    )
    return int.from_bytes(raw[4:], "little")


def test_the_builder_knows_every_field_the_installed_telethon_has():
    """The guard that makes the rest of this file self-maintaining: if a future
    Telethon adds a right, this is what notices.

    A superset, not an equality: the builder now also carries rights Telethon
    never shipped. Telethon's own fields remain the floor it may not fall below.
    """
    assert _all_fields() >= _telethon_fields()


def test_the_rights_telethon_never_shipped_are_reachable_too():
    """Named individually, because these are the ones no introspection can find:
    the library that would have to declare them is archived."""
    assert _LATER_THAN_TELETHON <= _all_fields()


def test_a_right_telethon_lacks_still_reaches_telegram_as_the_right_bit():
    """The load-bearing test of the whole hand-added-flags approach.

    Setting an attribute on a Python object proves nothing: the earlier bug was
    exactly a right that looked set and never left the process. These bit
    positions come from layer 229's `chatAdminRights`, and this asserts the
    server puts them where Telegram reads them.
    """
    for name, bit in (("manage_linked_peers", 19), ("manage_welcome_messages", 20)):
        granted = _flags_on_the_wire(moderation_mod._build_admin_rights({name: True}))
        assert granted >> bit & 1, f"{name} never reached flags.{bit}"

        withheld = _flags_on_the_wire(
            moderation_mod._build_admin_rights({name: False}, defaults={})
        )
        assert not (withheld >> bit & 1), f"{name} set flags.{bit} when it was declined"


def test_the_hand_added_bits_do_not_disturb_the_rights_telethon_serialises():
    """The bits are OR'd onto Telethon's own output. If that arithmetic were
    wrong it would corrupt a neighbouring right rather than fail loudly."""
    without = _flags_on_the_wire(moderation_mod._build_admin_rights({}, defaults={}))
    with_later = _flags_on_the_wire(
        moderation_mod._build_admin_rights(
            {"manage_linked_peers": True, "manage_welcome_messages": True}, defaults={}
        )
    )

    assert without == 0
    assert with_later == (1 << 19) | (1 << 20)


def test_the_five_that_were_unreachable_are_named_explicitly():
    """Pinned by name, not by count, so the specific regression cannot come back
    while the total happens to match."""
    fields = set(moderation_mod._admin_rights_fields())

    for right in (
        "post_stories",
        "edit_stories",
        "delete_stories",
        "manage_direct_messages",
        "manage_ranks",
    ):
        assert right in fields, right


def test_a_declined_right_is_declined_and_the_rest_keep_their_default():
    """The long-standing contract, which the story fix must not have changed:
    asking for less gets you less, but declining ONE right does not silently
    decline the others."""
    rights = moderation_mod._build_admin_rights({"ban_users": False})

    assert rights.ban_users is False
    assert rights.change_info is True, "an unmentioned right lost its default"


def test_the_five_formerly_unreachable_rights_are_granted_by_default_too():
    """The user-visible bug: a promotion reported success while Telegram's own
    panel showed "Manage stories 0/3" and two switches sitting off."""
    rights = moderation_mod._build_admin_rights()

    assert rights.post_stories is True
    assert rights.edit_stories is True
    assert rights.delete_stories is True
    assert rights.manage_direct_messages is True
    assert rights.manage_ranks is True


def test_the_two_held_back_stay_off_unless_asked_for():
    """One lets an admin mint more admins, the other changes who they appear to
    be. Neither should arrive by default."""
    rights = moderation_mod._build_admin_rights()

    assert rights.add_admins is False
    assert rights.anonymous is False
    assert moderation_mod._build_admin_rights({"add_admins": True}).add_admins is True


def test_a_demotion_clears_every_field_including_the_new_ones():
    """`ChatAdminRights` fields left unset serialise as absent, which is how the
    old demotion could leave story and direct-message rights standing."""
    rights = moderation_mod._build_admin_rights({}, defaults={})

    for name in _all_fields():
        assert getattr(rights, name) is False, f"{name} was not explicitly cleared"


def test_an_unknown_key_is_ignored_rather_than_raising():
    """Telegram adds rights over time. A caller copying a newer example should
    lose that one right, not have the whole promotion refused."""
    rights = moderation_mod._build_admin_rights({"some_right_from_the_future": True})

    assert rights.post_stories is True


@pytest.mark.parametrize("tool_name", ["promote_admin", "demote_admin", "edit_admin_rights"])
def test_no_tool_hand_rolls_the_rights_object_any_more(tool_name):
    """All three built their own `ChatAdminRights(...)` and all three fell behind
    together. One builder is the reason they cannot drift apart again."""
    source = inspect.getsource(getattr(moderation_mod, tool_name))

    assert "ChatAdminRights(" not in source, (
        f"{tool_name} constructs ChatAdminRights directly again; use "
        "_build_admin_rights so a new Telethon field cannot go missing"
    )


def test_edit_admin_rights_exposes_every_field_as_a_parameter():
    """This tool's whole purpose is per-right control, so a right it cannot name
    is a right it cannot set."""
    params = set(inspect.signature(moderation_mod.edit_admin_rights).parameters)

    missing = _all_fields() - params
    assert not missing, f"edit_admin_rights cannot set: {sorted(missing)}"


def test_the_later_flags_survive_a_round_trip_through_telethons_reader():
    """Granting a right this server cannot then report would be the same
    asymmetry in a new place.

    `ChatAdminRights.from_reader` reads the flags integer, sets the seventeen
    fields it knows and discards the rest, so bits 19 and 20 arrive from
    Telegram and vanish before any caller sees them. The reader is wrapped at
    import; this is what proves the wrap is installed and correct.
    """
    from telethon.extensions.binaryreader import BinaryReader

    granted = moderation_mod._build_admin_rights(
        {"manage_welcome_messages": True, "manage_linked_peers": True}, defaults={}
    )

    read_back = BinaryReader(bytes(granted)).tgread_object()
    rights = moderation_mod.admin_rights_to_dict(read_back)

    assert rights["manage_welcome_messages"] is True
    assert rights["manage_linked_peers"] is True
    assert rights["ban_users"] is False, "a right never granted came back set"


def test_the_reader_wrap_is_installed_only_once():
    """A second wrap would read the flags twice and rewind twice, which
    silently corrupts every rights object after it."""
    moderation_mod._install_extended_rights_reader()
    moderation_mod._install_extended_rights_reader()

    from telethon.extensions.binaryreader import BinaryReader

    granted = moderation_mod._build_admin_rights({"manage_welcome_messages": True}, defaults={})
    rights = moderation_mod.admin_rights_to_dict(BinaryReader(bytes(granted)).tgread_object())

    assert rights["manage_welcome_messages"] is True


def test_a_right_the_layer_cannot_carry_is_reported_not_swallowed():
    """Measured on a live channel: one request from its creator carrying
    flags.18, flags.19 and flags.20 was ACCEPTED, and only flags.18 was there
    afterwards. Telegram masks flags newer than the layer the client announced.

    Serialising the bits correctly is necessary and not sufficient, so the
    tool has to say what it could not deliver. "Admin rights updated" while
    the requested right is quietly absent is the worst available outcome:
    nothing downstream can tell.
    """
    from telethon.tl.alltlobjects import LAYER

    dropped = moderation_mod.undeliverable_rights(
        {"manage_welcome_messages": True, "ban_users": True, "manage_linked_peers": False}
    )

    assert dropped == ["manage_welcome_messages"], "the wrong rights were called undeliverable"

    note = moderation_mod._undeliverable_note(dropped)
    assert "manage_welcome_messages" in note
    assert str(LAYER) in note, "the note must name the layer it measured, not a remembered number"


def test_every_right_beyond_the_announced_layer_is_known_to_be_undeliverable():
    """The two lists must not drift: a flag added to the builder without being
    added here would go back to being silently dropped."""
    assert set(moderation_mod._EXTRA_ADMIN_RIGHT_BITS) == set(
        moderation_mod.undeliverable_rights(
            {name: True for name in moderation_mod._EXTRA_ADMIN_RIGHT_BITS}
        )
    )


def test_the_reported_rights_cover_exactly_what_can_be_set():
    """`get_admins` reads back through `admin_rights_to_dict`. A right the
    reader cannot name is a right nobody can see the absence of - which is how
    "one or two admins are missing this permission" became unanswerable
    without opening Telegram itself."""
    granted = moderation_mod._build_admin_rights()

    assert set(moderation_mod.admin_rights_to_dict(granted)) == set(
        moderation_mod._admin_rights_fields()
    )


def test_the_note_accounts_for_every_right_however_it_ended_up():
    """`edit_admin_rights` now finishes the dropped rights over TDLib, so the
    note has three outcomes to report instead of one. The property that matters
    is not the wording: it is that a requested right cannot fall between the
    buckets. A name mentioned nowhere reads as success, which is exactly the
    silent drop this whole path exists to end.
    """
    outcome = {
        "delivered": ["manage_welcome_messages"],
        "failed": {"a_right_telegram_refused": "Telegram declined it."},
        "unmappable": ["manage_linked_peers"],
    }

    note = moderation_mod._later_rights_note(outcome)

    for name in ("manage_welcome_messages", "a_right_telegram_refused", "manage_linked_peers"):
        assert name in note, f"{name} vanished from the report"
    assert "Delivered over TDLib" in note
    assert (
        "NOT set: a_right_telegram_refused, manage_linked_peers" in note
    ), "a right that was delivered must not also be listed as not set"


def test_a_right_that_was_delivered_is_not_also_reported_as_dropped():
    """The regression worth pinning: the note is built from what HAPPENED, not
    from what was requested, so a right TDLib delivered reads as delivered."""
    note = moderation_mod._later_rights_note(
        {"delivered": ["manage_welcome_messages"], "failed": {}, "unmappable": []}
    )

    assert "manage_welcome_messages" in note
    assert "NOT set" not in note


def test_nothing_dropped_and_nothing_delivered_says_nothing():
    """The ordinary call. A note appended to every successful result would train
    readers to skip it."""
    assert (
        moderation_mod._later_rights_note({"delivered": [], "failed": {}, "unmappable": []}) == ""
    )


@pytest.mark.asyncio
async def test_a_session_too_new_to_promote_says_so_instead_of_looking_like_a_permission_gap():
    """Telegram refuses admin changes from a login younger than about 24 hours,
    however complete its rights are. Measured live: the channel's own CREATOR
    was refused, minutes after that account was added.

    Worth its own message because the account that hits this is nearly always
    one just configured - the rights read correctly, the call fails, and the
    generic "you need admin rights" answer sends the reader to check a
    permission that was never the problem.
    """
    import telethon

    from telegram_mcp.tools import moderation as mod

    class _Refuses:
        def is_connected(self):
            return True

        async def __call__(self, request):
            raise telethon.errors.rpcerrorlist.FreshChangeAdminsForbiddenError(request=None)

    client = _Refuses()

    async def _connected(_client):
        return None

    async def _resolve(reference, _client):
        class _Peer:
            id = 5876481644

        return _Peer()

    original = (mod.get_client, mod.ensure_connected, mod.resolve_entity)
    mod.get_client = lambda account=None: client
    mod.ensure_connected = _connected
    mod.resolve_entity = _resolve
    try:
        answer = await mod.edit_admin_rights(
            chat_id=-1002046407246, user_id=5876481644, account="acct", change_info=True
        )
    finally:
        mod.get_client, mod.ensure_connected, mod.resolve_entity = original

    assert "24 hours" in answer, "the age rule was not named"
    assert "anti-hijack" in answer
    assert "need admin rights" not in answer, "it still reads as a missing permission"
