"""Every tool describes itself with all four annotation hints.

Two readers depend on it. Public MCP directories reject a tool whose read-only,
destructive, idempotent or open-world hint is missing or not a boolean; an external
scan found 217 of 226 tools failing that. And the safeguard classifies each call from
these same hints, so a missing one is not a cosmetic gap - it is a tool the safeguard
would have to guess about.
"""

import pytest

from telegram_mcp.tools import mcp  # noqa: F401  (registers every tool)

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _tools():
    return list(mcp._tool_manager.list_tools())


def _wire(tool) -> dict:
    """The hints as a client receives them: camelCase keys, as serialised on the wire.

    mcp 2.x exposes them as snake_case attributes and serialises them under these
    aliases; the directories read the serialised form, so that is what is checked.
    """
    if tool.annotations is None:
        return {}
    return tool.annotations.model_dump(by_alias=True, exclude_none=True)


def test_the_registry_is_not_empty():
    """A test over an empty list passes vacuously; this pins that it saw real tools."""
    assert len(_tools()) > 200


@pytest.mark.parametrize("hint", HINTS)
def test_every_tool_declares_the_hint_as_a_boolean(hint):
    missing = []
    for tool in _tools():
        if not isinstance(_wire(tool).get(hint), bool):
            missing.append(tool.name)
    assert not missing, f"{len(missing)} tools lack a boolean {hint}: {', '.join(sorted(missing))}"


def test_no_tool_is_both_read_only_and_destructive():
    contradictions = [
        tool.name
        for tool in _tools()
        if _wire(tool).get("readOnlyHint") is True and _wire(tool).get("destructiveHint") is True
    ]
    assert not contradictions, f"read-only yet destructive: {', '.join(contradictions)}"


def test_copying_into_a_secret_chat_is_not_destructive_to_the_owner():
    """A scanner flagged it for deleting its own scratch file. That file is local and
    temporary; nothing of the owner's is destroyed."""
    tool = next(t for t in _tools() if t.name == "copy_into_secret_chat")
    assert _wire(tool).get("destructiveHint") is False
    assert _wire(tool).get("readOnlyHint") is False
