"""The command list in docs/COMMANDS.md is generated from the live tool registry.

A hand-kept list of 230 entries drifts the first week. This regenerates it in memory
and fails on any difference, so a new tool, a renamed one, or a changed safeguard
category cannot ship without the list saying so (FR-022, SC-005).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_command_list  # noqa: E402


def test_the_command_list_matches_the_registry():
    expected = generate_command_list.render()
    actual = (ROOT / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/COMMANDS.md is stale; regenerate it with "
        "`python scripts/generate_command_list.py`"
    )


def test_every_tool_has_exactly_one_entry():
    from telegram_mcp.tools import mcp

    text = generate_command_list.render()
    for tool in mcp._tool_manager.list_tools():
        assert text.count(f"| `{tool.name}` |") == 1, tool.name
