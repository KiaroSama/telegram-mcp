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
"""

import inspect

import pytest
from telethon.tl.types import ChatAdminRights

from telegram_mcp.tools import moderation as moderation_mod


def _telethon_fields():
    return {
        name for name in inspect.signature(ChatAdminRights.__init__).parameters if name != "self"
    }


def test_the_builder_knows_every_field_the_installed_telethon_has():
    """The guard that makes the rest of this file self-maintaining: if a future
    Telethon adds a right, this is what notices."""
    assert set(moderation_mod._admin_rights_fields()) == _telethon_fields()


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

    for name in _telethon_fields():
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

    missing = _telethon_fields() - params
    assert not missing, f"edit_admin_rights cannot set: {sorted(missing)}"
