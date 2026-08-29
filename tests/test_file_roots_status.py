"""The server says why the file tools are off, instead of only refusing.

`download_media` and `upload_file` are registered and refuse every path until an
allowed root is configured. Before this tool, an agent hitting that refusal could
not explain it or route around it — so it retried, or gave up, or told the user
something vague. It was the worst first-contact experience in a 170-tool server,
and the only one where the server already knew the answer and did not say it.

What makes this tool worth anything is the accuracy of its advice, so these tests
assert on the CONTENT of the guidance, not merely that a string came back.
"""

import json

import pytest

from telegram_mcp import file_roots
from telegram_mcp.tools import diagnostics as diag


async def _status(**kwargs):
    """The tool returns `format_tool_result`'s `{"results": {...}}` envelope."""
    raw = await diag.get_file_roots_status(**kwargs)
    payload = json.loads(raw)
    return payload["results"]


@pytest.mark.asyncio
async def test_no_roots_says_disabled_and_names_the_fix(monkeypatch):
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [])

    result = await _status()

    assert result["file_tools_enabled"] is False
    assert result["status"] == file_roots.ROOTS_STATUS_NOT_CONFIGURED
    assert result["roots"] == []
    assert "positional arguments" in result["next_step"], result["next_step"]


@pytest.mark.asyncio
async def test_server_roots_are_reported_as_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])

    result = await _status()

    assert result["file_tools_enabled"] is True
    assert result["status"] == file_roots.ROOTS_STATUS_READY
    assert result["roots"] == [str(tmp_path)]
    assert result["server_command_line_roots"] == [str(tmp_path)]


@pytest.mark.asyncio
async def test_a_client_that_denies_everything_is_distinguished_from_one_that_cannot_answer(
    monkeypatch, tmp_path
):
    """An empty `roots/list` means 'nothing is permitted' and is obeyed. That is a
    different state from a client with no roots support, and the advice differs."""
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])

    class DenyAllContext:
        class session:
            @staticmethod
            async def list_roots():
                return type("R", (), {"roots": []})()

    result = await _status(ctx=DenyAllContext())

    assert result["file_tools_enabled"] is False
    assert result["status"] == file_roots.ROOTS_STATUS_CLIENT_DENY_ALL
    assert "empty list" in result["next_step"]
    assert "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" in result["next_step"]


@pytest.mark.asyncio
async def test_every_status_the_resolver_can_return_has_advice():
    """A status with no advice would render as 'Unrecognised roots status', which
    is the failure this tool exists to prevent, one level up."""
    known = {
        value
        for name, value in vars(file_roots).items()
        if name.startswith("ROOTS_STATUS_") and isinstance(value, str)
    }

    assert known, "no status constants found; this test is not testing anything"
    missing = known - set(diag._ROOTS_ADVICE)
    assert not missing, f"no next_step written for {sorted(missing)}"


def test_the_advice_quotes_the_real_environment_variable():
    """The flag name is verified against the module that reads it rather than
    typed from memory - a wrong name is worse than no advice."""
    import inspect

    source = inspect.getsource(file_roots._server_roots_fallback_enabled)

    assert "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" in source
    quoted = [
        text
        for text in diag._ROOTS_ADVICE.values()
        if "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" in text
    ]
    assert quoted, "no advice mentions the fallback variable at all"
