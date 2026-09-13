"""Every client gets exactly one incoming-message handler, for as long as it serves.

Registration ran once, at import, over whatever `clients` held then. An account
added or re-logged-in while the server ran therefore received nothing - which
looks like a broken account, not an unwired one, and that is what made it
expensive to find. Re-registering everything on each refresh is the other half
of the trap: a surviving client gets a second handler and every burst doubles.

So the tests here assert BOTH directions, and the identity case in between: a
re-login keeps the label and replaces the object.
"""

import pytest

from telegram_mcp import connection as conn
from telegram_mcp.tools import events as mod
from telegram_mcp.tools import events_store as store


class _Client:
    """Records handler attachment the way Telethon's API is actually used."""

    def __init__(self, name):
        self.name = name
        self.handlers = []

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    def remove_event_handler(self, callback):
        self.handlers = [h for h in self.handlers if h is not callback]


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(conn, "clients", {}, raising=False)
    monkeypatch.setattr(mod, "clients", conn.clients, raising=False)
    monkeypatch.setattr(mod, "_incoming_handlers", {})
    monkeypatch.setattr(store, "_pending_msgs", {})
    return conn.clients


def test_an_account_added_while_running_gets_a_handler(registry):
    added = _Client("added")
    registry["new"] = added

    mod._on_clients_changed({"new"}, set())

    assert len(added.handlers) == 1, "a hot-added account received nothing"


def test_an_untouched_account_does_not_collect_a_second_handler(registry):
    steady = _Client("steady")
    registry["steady"] = steady
    mod.register_incoming_handlers()
    assert len(steady.handlers) == 1

    mod.register_incoming_handlers()
    mod.register_incoming_handlers()

    assert len(steady.handlers) == 1, "every burst from this account was multiplied"


def test_a_re_login_moves_the_handler_to_the_new_client(registry):
    """Same label, different object - the case a label-only check misses."""
    old = _Client("old")
    registry["work"] = old
    mod.register_incoming_handlers()
    assert len(old.handlers) == 1

    replacement = _Client("new")
    registry["work"] = replacement
    mod._on_clients_changed({"work"}, set())

    assert old.handlers == [], "the retired client kept listening"
    assert len(replacement.handlers) == 1, "the replacement was left unwired"


def test_a_removed_account_stops_listening(registry):
    gone = _Client("gone")
    registry["gone"] = gone
    mod.register_incoming_handlers()
    registry.pop("gone")

    mod._on_clients_changed(set(), {"gone"})

    assert gone.handlers == []
    assert "gone" not in mod._incoming_handlers


def test_a_replaced_label_does_not_inherit_the_previous_logins_bursts(registry):
    """A pending burst names a chat under an account LABEL. Re-logging that label
    in can point it at a different Telegram user, and the debounce tools would
    then hand the new login the previous one's unread conversations."""
    registry["work"] = _Client("first")
    mod.register_incoming_handlers()
    store._pending_msgs[("work", 111)] = {"count": 3}
    store._pending_msgs[("other", 222)] = {"count": 1}

    registry["work"] = _Client("second")
    mod._on_clients_changed({"work"}, set())

    assert ("work", 111) not in store._pending_msgs, "the new login inherited them"
    assert ("other", 222) in store._pending_msgs, "and an unrelated account was cleared"


def test_the_registry_listener_is_wired_at_import():
    """Nothing calls into `tools.events`; it asks `connection` to call it."""
    assert mod._on_clients_changed in conn._registry_listeners
