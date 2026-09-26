""" "Always approve": one tool in one chat of one account, kept across restarts.

The owner's choice (2026-09-26). A grant only ever makes the safeguard ask LESS, so the
file that holds it is the owner's alone, written atomically, and a file that cannot be
read means no grants - the safeguard asks again rather than guessing.
"""

import json

import pytest

from telegram_mcp.safeguard import grants


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(grants, "grants_path", lambda: tmp_path / "always-approvals.json")
    grants.reset_cache()
    yield tmp_path / "always-approvals.json"
    grants.reset_cache()


def test_nothing_is_granted_by_default(store):
    assert not grants.is_granted("main", "delete_message", "-100")
    assert grants.list_all() == []


def test_a_grant_covers_that_tool_in_that_chat_of_that_account_only(store):
    grants.add("Main", "delete_message", "@SomeChat")
    assert grants.is_granted("main", "delete_message", "somechat")
    assert not grants.is_granted("main", "delete_message", "other")
    assert not grants.is_granted("main", "leave_chat", "somechat")
    assert not grants.is_granted("work", "delete_message", "somechat")


def test_grants_survive_a_restart(store):
    grants.add("main", "delete_message", -100)
    grants.reset_cache()  # a new process reads the file again
    assert grants.is_granted("main", "delete_message", "-100")
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data == {"grants": [["main", "delete_message", "-100"]], "folders": []}
    assert not list(store.parent.glob("*.tmp"))


def test_adding_twice_keeps_one_and_revoking_removes_it(store):
    grants.add("main", "delete_message", -100)
    grants.add("main", "delete_message", -100)
    assert grants.list_all() == [{"account": "main", "tool": "delete_message", "chat": "-100"}]
    assert grants.revoke("main", "delete_message", -100) is True
    assert grants.revoke("main", "delete_message", -100) is False
    assert not grants.is_granted("main", "delete_message", "-100")


def test_an_unreadable_file_grants_nothing(store):
    store.write_text("{ broken", encoding="utf-8")
    grants.reset_cache()
    assert not grants.is_granted("main", "delete_message", "-100")
    assert grants.state_error()
