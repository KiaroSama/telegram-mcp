"""Picking up an account added, removed or re-logged-in while the server runs.

Restarting for a `.env` edit was not merely inconvenient: the failure when you
forgot was `AuthKeyUnregisteredError` from the session this process was still
holding. That reads like Telegram revoking the login rather than like stale
state here, and it cost several real logins chasing the wrong cause.

The tests drive a REAL file, because the whole mechanism is "did this file
change" and a fake stat would be testing the mock.
"""

import os

import pytest

from telegram_mcp import connection as conn


class _Client:
    """Stands in for a TelegramClient; records whether it was retired."""

    def __init__(self, label):
        self.label = label
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A real .env this module will read, with the module's state reset."""
    path = tmp_path / ".env"

    def _write(lines):
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    monkeypatch.setattr(conn, "_env_file", lambda: str(path))
    monkeypatch.setattr(conn, "_discover_accounts", lambda env=None: _discover(env))
    monkeypatch.setattr(conn, "clients", {}, raising=False)
    monkeypatch.setattr(conn, "_env_stamp", (), raising=False)
    monkeypatch.setattr(conn, "_env_digests", {}, raising=False)
    return _write


def _discover(env):
    """Label -> client, from the same variables the real discovery reads."""
    built = {}
    for key, value in (env or {}).items():
        if key.startswith("TELEGRAM_SESSION_STRING_") and value:
            built[key[len("TELEGRAM_SESSION_STRING_") :].lower()] = _Client(key)
    return built


def _adopt(write, lines):
    """Write the file and let the module take it as the starting point."""
    write(lines)
    conn.refresh_accounts()
    conn.clients.update(_discover({k: v for k, v in _read(lines).items()}))


def _read(lines):
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_an_account_added_while_running_is_picked_up(env_file):
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=aaa"])
    assert set(conn.clients) == {"one"}

    env_file(["TELEGRAM_SESSION_STRING_ONE=aaa", "TELEGRAM_SESSION_STRING_TWO=bbb"])
    changed = conn.refresh_accounts()

    assert "two" in conn.clients, "the new account still needs a restart"
    assert "two" in changed


def test_an_account_removed_while_running_is_dropped_and_disconnected(env_file):
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=aaa", "TELEGRAM_SESSION_STRING_TWO=bbb"])
    retired = conn.clients["two"]

    env_file(["TELEGRAM_SESSION_STRING_ONE=aaa"])
    conn.refresh_accounts()

    assert "two" not in conn.clients
    assert retired.disconnected, "the dropped client was left holding its socket"


def test_a_re_login_replaces_the_client_rather_than_keeping_the_dead_session(env_file):
    """The expensive case. The label is unchanged, so a check that only compared
    NAMES would keep serving the session Telegram has already invalidated - which
    is precisely the AuthKeyUnregisteredError that cost the owner four logins."""
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=aaa"])
    stale = conn.clients["one"]

    env_file(["TELEGRAM_SESSION_STRING_ONE=zzz"])
    changed = conn.refresh_accounts()

    assert conn.clients["one"] is not stale, "the dead session is still in use"
    assert stale.disconnected
    assert "one" in changed


def test_an_untouched_file_costs_nothing(env_file):
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=aaa"])
    before = conn.clients["one"]

    assert conn.refresh_accounts() == []
    assert conn.clients["one"] is before, "an unchanged file rebuilt the clients"


def test_a_broken_env_leaves_the_working_clients_alone(env_file, monkeypatch):
    """A `.env` mid-rewrite, or one that no longer describes a valid set, must
    not take the running server down. The account manager backs up and rewrites,
    so that window is real."""
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=aaa"])
    working = conn.clients["one"]

    def _explode(env=None):
        raise ValueError("Account 'one' is defined more than once")

    monkeypatch.setattr(conn, "_discover_accounts", _explode)
    env_file(["TELEGRAM_SESSION_STRING_ONE=aaa", "TELEGRAM_SESSION_STRING_ONE_=bbb"])

    assert conn.refresh_accounts() == []
    assert conn.clients["one"] is working, "a broken file removed a working account"


def test_the_session_value_is_never_kept_only_its_digest(env_file):
    """A session string is a full login. The reload has to notice it CHANGED
    without holding it."""
    _adopt(env_file, ["TELEGRAM_SESSION_STRING_ONE=1AAAAsecretlogin"])
    env_file(["TELEGRAM_SESSION_STRING_ONE=1AAAAotherlogin"])
    conn.refresh_accounts()

    kept = " ".join(str(v) for v in conn._env_digests.values())
    assert "secretlogin" not in kept
    assert "otherlogin" not in kept
    assert len(conn._env_digests) >= 1, "nothing was remembered, so nothing can be compared"


def test_a_missing_env_file_is_not_an_error(env_file, monkeypatch):
    """A deployment configured entirely through real environment variables has
    no `.env` at all."""
    monkeypatch.setattr(conn, "_env_file", lambda: None)
    assert conn.refresh_accounts() == []
    assert conn._env_fingerprint(None) == ()


def test_an_unreadable_env_file_is_not_an_error(env_file, monkeypatch):
    monkeypatch.setattr(conn, "_env_file", lambda: str(os.devnull) + "-does-not-exist")
    assert conn.refresh_accounts() == []
