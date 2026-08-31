"""The server says why the file tools are off, instead of only refusing.

`download_media` and `upload_file` are registered and refuse every path until an
allowed root is configured. Before this tool, an agent hitting that refusal could
not explain it or route around it — so it retried, or gave up, or told the user
something vague. It was the worst first-contact experience in a 170-tool server,
and the only one where the server already knew the answer and did not say it.

What makes this tool worth anything is the accuracy of its advice, so these tests
assert on the CONTENT of the guidance, not merely that a string came back.

That accuracy failed once in a way worth pinning. The advice was a flat
status-to-sentence map, and two states recommended the server's own command-line
roots as the way out no matter what: with the server started without any -- the
launcher's default -- a live call reported `server_fallback_allowed: true` while
its own next_step read "no fallback is enabled ... set
TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1". Both halves were wrong, the suggested
change was already in place, and the real remedy was never named. Following it
cost a server restart and produced the identical state. The advice for those two
states is computed from the facts now, and the tests below hold each branch.
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


def _every_status():
    return {
        value
        for name, value in vars(file_roots).items()
        if name.startswith("ROOTS_STATUS_") and isinstance(value, str)
    }


def test_every_status_the_resolver_can_return_has_advice():
    """A status with no advice renders as 'Unrecognised roots status', which is
    the failure this tool exists to prevent, one level up.

    Asked of the resolver rather than of the map: two states deliberately have no
    map entry because their advice depends on the server's configuration, and a
    test that only knew about the map would call those a gap.
    """
    known = _every_status()
    assert known, "no status constants found; this test is not testing anything"

    for status in known:
        advice = diag._roots_advice(status)
        assert "Unrecognised" not in advice, f"no next_step written for {status}"
        assert advice.strip(), status


def test_the_fallback_is_not_recommended_when_there_is_nothing_to_fall_back_to(monkeypatch):
    """The bug this file's header describes.

    With no command-line roots on the server, pointing the caller at
    TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK buys them a restart and the same state.
    """
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [])

    for status in (file_roots.ROOTS_STATUS_ERROR, file_roots.ROOTS_STATUS_CLIENT_DENY_ALL):
        advice = diag._roots_advice(status)

        assert (
            "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" not in advice
        ), f"{status} still recommends the fallback with no roots behind it: {advice}"
        assert "positional arguments" in advice, advice


def test_the_advice_never_claims_the_fallback_is_off_while_it_is_on(monkeypatch, tmp_path):
    """The other half of the same bug: the old text asserted "no fallback is
    enabled" as a fact, in a state reached with it enabled."""
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")

    advice = diag._roots_advice(file_roots.ROOTS_STATUS_ERROR)

    assert "no fallback is enabled" not in advice
    assert "already enabled" in advice, advice


def test_the_fallback_is_still_recommended_when_it_would_actually_help(monkeypatch, tmp_path):
    """The fix must not overshoot: with roots configured and the flag off, the
    flag IS the remedy and has to keep being named."""
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [tmp_path])
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)

    advice = diag._roots_advice(file_roots.ROOTS_STATUS_ERROR)

    assert "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1" in advice, advice


def test_the_advice_quotes_the_real_environment_variable():
    """The flag name is verified against the module that reads it rather than
    typed from memory - a wrong name is worse than no advice."""
    import inspect

    source = inspect.getsource(file_roots._server_roots_fallback_enabled)

    assert "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" in source
    quoted = [
        text
        for text in list(diag._ROOTS_ADVICE.values())
        + [diag._roots_advice(status) for status in _every_status()]
        if "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK" in text
    ]
    assert quoted, "no advice mentions the fallback variable at all"
