"""Guards for the groups.py -> groups/moderation/invites split.

Two things can silently break when tools move between these modules: a tool can
be dropped from a module's ``__all__`` (the package re-exports via
``from ... import *``, so it would just vanish from the MCP surface), and a new
module can be given the name of an existing tool (that binding shadows the
submodule -- a mistake this package has made before, see the note on
``translation.py`` in ``tools/__init__.py``).
"""

import ast
import pathlib

import pytest

from telegram_mcp.tools import groups, invites, moderation

MODULES = (groups, moderation, invites)

# The full tool set that lived in groups.py before the split. Moving a tool
# between these three modules is fine; losing one is not.
EXPECTED_TOOLS = {
    "ban_user",
    "create_channel",
    "create_group",
    "delete_chat_photo",
    "demote_admin",
    "edit_admin_rights",
    "edit_chat_about",
    "edit_chat_photo",
    "edit_chat_title",
    "export_chat_invite",
    "get_admins",
    "get_banned_users",
    "get_invite_link",
    "get_participants",
    "get_recent_actions",
    "import_chat_invite",
    "invite_to_group",
    "join_chat_by_link",
    "leave_chat",
    "promote_admin",
    "set_default_chat_permissions",
    "toggle_slow_mode",
    "unban_user",
}


def test_no_tool_was_lost_or_duplicated():
    exported = [name for m in MODULES for name in m.__all__]
    assert sorted(exported) == sorted(set(exported)), "a tool is exported by two modules"
    assert set(exported) == EXPECTED_TOOLS


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_all_matches_the_decorated_tools_defined_in_the_file(module):
    """__all__ must list exactly the module's own tools, and each must exist."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any("mcp.tool" in ast.unparse(d) for d in node.decorator_list)
    ]
    assert sorted(module.__all__) == sorted(defined)
    for name in module.__all__:
        assert callable(getattr(module, name))


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_module_name_is_not_shadowed_by_a_tool(module):
    """`from x import *` binds tool names into the package; one matching a
    submodule name would replace that submodule."""
    short_name = module.__name__.rsplit(".", 1)[-1]
    assert short_name not in EXPECTED_TOOLS
