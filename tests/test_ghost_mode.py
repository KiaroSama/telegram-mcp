"""Ghost mode: the account reads without being seen.

On by default (the owner, 2026-09-26). Settable for every account, one account or one
chat, most specific wins. While it is on, nothing this server does tells anyone the
owner saw or is typing, and the account is reported offline after the server's own
activity - but not otherwise, so the owner's phone shows presence as usual.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from telegram_mcp.safeguard import ghost


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ghost, "settings_path", lambda: tmp_path / "ghost.json")
    ghost.reset_cache()
    yield tmp_path / "ghost.json"
    ghost.reset_cache()


def test_on_by_default(store):
    assert ghost.is_on("main", -100)
    assert ghost.is_on(None, None)


def test_the_most_specific_setting_wins(store):
    ghost.set_mode(False)  # every account
    assert not ghost.is_on("main", 5)
    ghost.set_mode(True, account="main")
    assert ghost.is_on("main", 5) and not ghost.is_on("other", 5)
    ghost.set_mode(False, account="main", chat=5)
    assert not ghost.is_on("main", 5) and ghost.is_on("main", 6)


def test_chat_keys_are_normalised(store):
    ghost.set_mode(False, account="Main", chat="@SomeOne")
    assert not ghost.is_on("main", "someone")
    assert not ghost.is_on("MAIN", "@someone")


def test_the_file_is_written_whole_and_read_back(store):
    ghost.set_mode(False, account="main", chat=-100)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data == {"default": True, "accounts": {}, "chats": {"main": {"-100": False}}}
    ghost.reset_cache()
    assert not ghost.is_on("main", -100)
    assert not list(store.parent.glob("*.tmp"))


def test_an_unreadable_file_means_ghost_on(store):
    store.write_text("{ not json", encoding="utf-8")
    ghost.reset_cache()
    assert ghost.is_on("main", 5)
    assert ghost.state()["error"]


def test_describe_reports_every_level(store):
    ghost.set_mode(False, account="main")
    view = ghost.describe("main", 5)
    assert view["default"] is True and view["account"] is False and view["chat"] is None
    assert view["effective"] is False
    assert "read markers" in " ".join(view["suppresses"])


# --- presence -----------------------------------------------------------------------


def test_offline_is_sent_after_activity_at_most_once_per_interval():
    sent = []
    now = [100.0]

    async def offline(account):
        sent.append((account, now[0]))

    async def fake_sleep(seconds):
        now[0] += seconds

    async def run():
        presence = ghost.Presence(offline, interval=10, clock=lambda: now[0], sleep=fake_sleep)
        presence.after_activity("main")
        await presence.drain()
        assert sent == [("main", 100.0)]
        now[0] = 103.0
        presence.after_activity("main")  # within 10 s: deferred to 110, not sent now
        presence.after_activity("main")  # still only one pending
        await presence.drain()
        assert sent == [("main", 100.0), ("main", 110.0)]
        presence.after_activity("other")  # accounts are independent
        await presence.drain()
        assert sent[-1] == ("other", 110.0)

    asyncio.run(run())


def test_after_activity_never_blocks_and_a_failure_is_swallowed():
    async def offline(account):
        raise RuntimeError("no network")

    async def run():
        presence = ghost.Presence(offline, interval=10)
        presence.after_activity("main")  # returns at once, no await
        await presence.drain()

    asyncio.run(run())


# --- nothing else emits a seen signal -------------------------------------------------


def test_no_code_sends_a_seen_signal_the_safeguard_does_not_know_about():
    """The only emitters are the three gated tools. A new request type that tells
    someone the owner looked would slip past ghost mode, so its appearance fails."""
    import telegram_mcp

    forbidden = re.compile(
        r"ReadStoriesRequest|IncrementStoryViewsRequest|ReadMessageContentsRequest"
        r"|GetMessagesViewsRequest\([^)]*increment\s*=\s*True"
    )
    root = Path(telegram_mcp.__file__).parent
    hits = [
        f"{path.relative_to(root)}"
        for path in root.rglob("*.py")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert not hits, hits


def test_the_tools_set_and_read_ghost_mode(store):
    from telegram_mcp.tools import ghost_tools

    answer = json.loads(asyncio.run(ghost_tools.set_ghost_mode(False)))
    assert answer["scope"] == "all accounts" and answer["effective"] is False
    view = json.loads(asyncio.run(ghost_tools.get_ghost_mode()))
    assert view["default"] is False
    assert "account" in asyncio.run(ghost_tools.set_ghost_mode(True, chat_id=5))
    status = json.loads(asyncio.run(ghost_tools.safeguard_status()))
    assert status["installed"] is True
    assert "token" not in json.dumps(status).lower()
