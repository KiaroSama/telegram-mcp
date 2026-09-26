"""The server says which folders the file tools may use, instead of only refusing.

An agent that hits a refused path cannot explain it or route around it unless the
server tells it the rule. Since 2026-09-26 the rule is the safeguard's (FR-030..FR-034):
`files/outbox` and `files/downloads` are always usable, the owner's configured and
"always allow" folders too, and every other folder needs the owner's answer. These tests
assert on the content of the answer, not merely that a string came back.
"""

import json
from pathlib import Path

import pytest

from telegram_mcp import file_roots
from telegram_mcp.safeguard import grants
from telegram_mcp.tools import diagnostics as diag


async def _status():
    return json.loads(await diag.get_file_roots_status())["results"]


@pytest.mark.asyncio
async def test_a_fresh_install_can_use_files_outbox_and_downloads(_folders_are_the_tests_own):
    file_roots.SERVER_ALLOWED_ROOTS[:] = []
    result = await _status()
    files = (_folders_are_the_tests_own / "files").resolve()
    assert result["file_tools_enabled"] is True
    assert {Path(p).resolve() for p in result["free_folders"]} == {
        files / "downloads",
        files / "outbox",
    }
    assert result["configured_folders"] == []
    assert result["always_allowed_folders"] == []
    assert (files / "downloads").is_dir() and (files / "outbox").is_dir()


@pytest.mark.asyncio
async def test_configured_and_always_allowed_folders_are_listed(tmp_path):
    configured = (tmp_path / "configured").resolve()
    granted = (tmp_path / "granted").resolve()
    configured.mkdir()
    file_roots.SERVER_ALLOWED_ROOTS[:] = [configured]
    grants.add_folder(granted)

    result = await _status()
    roots = {Path(p).resolve() for p in result["roots"]}
    assert configured in roots and granted in roots
    assert [Path(p).resolve() for p in result["configured_folders"]] == [configured]
    assert [Path(p).resolve() for p in result["always_allowed_folders"]] == [granted]


@pytest.mark.asyncio
async def test_the_rule_names_what_is_free_and_what_asks():
    rule = (await _status())["rule"]
    for phrase in ("files/outbox", "files/downloads", "TELEGRAM_FILE_ROOTS", "allow / deny"):
        assert phrase in rule
