"""The chats.py split: nothing lost on the way out, nothing shadowed on the way in.

chats.py held 17 tools across three unrelated jobs. Splitting it is only safe if the
tool set survives the move intact, so that is what this asserts - by responsibility,
not by count, so a tool that drifts into the wrong module fails here rather than
quietly changing which module a future monkeypatch has to target.

The second check is the star-import trap this repo has already been bitten by once
(see test_tool_registry): `from telegram_mcp.tools.X import *` binds every tool NAME
into the package, so a tool named after a module silently replaces the submodule
attribute. `list_topics` in topics.py is fine; a tool named `topics` would not be.
"""

import importlib

CHATS = {
    "get_chats",
    "list_chats",
    "get_chat",
    "search_public_chats",
    "resolve_username",
    "get_full_chat",
    "get_common_chats",
    "get_message_read_by",
    "get_message_link",
}
TOPICS = {"list_topics", "enable_forum_topics", "create_forum_topic"}
CHAT_STATE = {
    "subscribe_public_channel",
    "mute_chat",
    "unmute_chat",
    "archive_chat",
    "unarchive_chat",
}


def _module(name):
    return importlib.import_module(f"telegram_mcp.tools.{name}")


def test_the_split_partitions_the_original_seventeen_tools():
    """Every tool chats.py used to own still exists, in exactly one of the three
    modules. 9 + 3 + 5 == 17, and the three sets are disjoint."""
    homes = {"chats": CHATS, "topics": TOPICS, "chat_state": CHAT_STATE}

    for name, expected in homes.items():
        module = _module(name)
        assert set(module.__all__) == expected, (
            f"telegram_mcp.tools.{name}.__all__ is {sorted(module.__all__)}, "
            f"expected {sorted(expected)}"
        )
        for tool in expected:
            assert callable(getattr(module, tool)), f"{name}.{tool} is not callable"

    assert len(CHATS | TOPICS | CHAT_STATE) == 17
    assert not (CHATS & TOPICS), CHATS & TOPICS
    assert not (CHATS & CHAT_STATE), CHATS & CHAT_STATE
    assert not (TOPICS & CHAT_STATE), TOPICS & CHAT_STATE


def test_no_tool_is_named_after_one_of_the_split_modules():
    """`import *` in tools/__init__.py binds tool names into the package. If any
    module anywhere in the package defines a tool called `chats`, `topics` or
    `chat_state`, that callable overwrites the submodule attribute and
    `from telegram_mcp.tools import topics` silently hands back a function."""
    package = importlib.import_module("telegram_mcp.tools")

    for name in ("chats", "topics", "chat_state"):
        _module(name)  # binds the submodule attribute if nothing shadowed it
        bound = getattr(package, name)
        assert not callable(bound), (
            f"telegram_mcp.tools.{name} resolves to a callable "
            f"({getattr(bound, '__name__', bound)!r}), not the module - some tool in "
            f"the package is named {name!r} and `import *` overwrote the submodule."
        )
