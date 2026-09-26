"""One tool call has a ceiling, and says so rather than going quiet.

Every Telegram call this server makes is bounded somewhere. The CALL was not: a
request that wedged below all of those left the client waiting until its own
idle timeout killed the connection, so the caller saw a transport failure for
what was a stalled operation - and went looking in the wrong place.

The idea is adopted from the upstream project; the shape is this server's, which
runs the MCP SDK's middleware chain rather than FastMCP's handler table.
"""

import asyncio

import pytest

from telegram_mcp import tool_budget


def test_the_default_sits_above_the_event_wait_tools():
    """`wait_for_new_message` answers "nothing arrived" at 50s. A ceiling at or
    below that would cut off a tool that was working correctly."""
    from telegram_mcp.tools import events

    assert tool_budget.tool_timeout_seconds(None) > 50.0
    assert "timeout: float = 50.0" in __import__("inspect").getsource(events.wait_for_new_message)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("30", 30.0),
        ("0", None),
        ("-1", None),
        ("", tool_budget.TOOL_TIMEOUT_SECONDS_DEFAULT),
        ("not a number", tool_budget.TOOL_TIMEOUT_SECONDS_DEFAULT),
        ("nan", tool_budget.TOOL_TIMEOUT_SECONDS_DEFAULT),
        ("inf", tool_budget.TOOL_TIMEOUT_SECONDS_DEFAULT),
    ],
)
def test_the_configured_value_is_read_safely(raw, expected):
    """`nan` is the one that matters: every comparison against it is false, so
    it would have read as "unbounded" while looking like a number."""
    assert tool_budget.tool_timeout_seconds(raw) == expected


@pytest.mark.asyncio
async def test_a_wedged_call_is_stopped_and_reported(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "0.05")
    budget = tool_budget.ToolCallBudget()

    async def _never_answers(ctx):
        await asyncio.get_running_loop().create_future()

    result = await asyncio.wait_for(budget(object(), _never_answers), timeout=3)

    assert result.is_error is True
    said = result.content[0].text
    assert "0.05s" in said
    # The honest half: a stopped call is not a call that did nothing.
    assert "whether the Telegram side completed" in said


@pytest.mark.asyncio
async def test_a_call_that_answers_in_time_is_untouched(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "5")
    budget = tool_budget.ToolCallBudget()
    answer = object()

    async def _answers(ctx):
        return answer

    assert await budget(object(), _answers) is answer


@pytest.mark.asyncio
async def test_zero_means_deliberately_unbounded(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "0")
    budget = tool_budget.ToolCallBudget()
    answer = object()

    async def _slow(ctx):
        await asyncio.sleep(0.01)
        return answer

    assert await asyncio.wait_for(budget(object(), _slow), timeout=3) is answer


def test_the_budget_is_installed_once_and_runs_first():
    """First in the chain after the safeguard, so the ceiling covers the other
    middleware too - a budget wrapping only the innermost handler would not bound
    work on the way out. The safeguard alone sits outside it: an approval may take
    five minutes, and inside the budget that wait would be cut off at 55 s."""
    from telegram_mcp import runtime
    from telegram_mcp.safeguard import Safeguard
    from telegram_mcp.tools import mcp  # noqa: F401  (installs the safeguard)

    tool_budget.install(runtime.mcp)
    tool_budget.install(runtime.mcp)

    budgets = [m for m in runtime.mcp.middleware if isinstance(m, tool_budget.ToolCallBudget)]
    assert len(budgets) == 1
    assert isinstance(runtime.mcp.middleware[0], Safeguard)
    assert isinstance(runtime.mcp.middleware[1], tool_budget.ToolCallBudget)
