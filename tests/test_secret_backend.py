"""The seam between the secret-chat tools and whatever carries secret chats.

One function stands there. Today the tools reach TDLib through
`tdlib_registry.secret_client`, called nineteen times from five modules and from
nowhere else; `secret_backend.secret_manager` takes that place. These tests pin the
behaviours the old seam earned the hard way, because a replacement that drops one of
them reintroduces the bug it was written for:

* it refuses while the server is shutting down, so a caller cannot start a backend
  against state that is already being flushed;
* it binds to the account's CURRENT client generation and re-checks that inside the
  lock, because a reload during the wait makes the client captured before it the
  previous account's;
* it caches one manager per account, keyed on the generation it was verified
  against, never on the label alone.

They are written against a fake client and a memory store: the protocol itself is the
package's business and its own suite owns it. What is tested here is the boundary.
"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def backend(monkeypatch):
    from telegram_mcp import secret_backend

    monkeypatch.setattr(secret_backend, "_by_account", {}, raising=False)
    monkeypatch.setattr(secret_backend, "_verified_against", {}, raising=False)
    monkeypatch.setattr(secret_backend, "_closing", False, raising=False)
    return secret_backend


class _Client:
    """Enough of a Telethon client for the manager to be constructed over."""

    def __init__(self, name="a", user_id=1001):
        self.name = name
        self.user_id = user_id
        self.handlers = []

    async def get_me(self, input_peer=False):
        return SimpleNamespace(user_id=self.user_id, id=self.user_id)

    def add_event_handler(self, handler, *args, **kwargs):
        self.handlers.append(handler)

    def remove_event_handler(self, handler, *args, **kwargs):
        if handler in self.handlers:
            self.handlers.remove(handler)

    def is_connected(self):
        return True


def _wire(monkeypatch, backend, client, owners=None):
    """Point the seam at one client, and keep its storage off the disk."""
    from telethon_secret_chat import MemoryStorage

    owners = owners or Path(tempfile.mkdtemp())
    monkeypatch.setattr(backend, "_telethon_client", lambda account: client)
    monkeypatch.setattr(backend, "_storage_for", lambda account: MemoryStorage())
    monkeypatch.setattr(backend, "_owner_path", lambda account: owners / f"{account}.owner.json")
    monkeypatch.setattr(backend, "_store_lock_dir", lambda: owners)
    monkeypatch.setattr(backend, "_store_locks", {}, raising=False)
    return owners


@pytest.mark.asyncio
async def test_secret_manager_returns_a_started_manager_bound_to_the_account_client(
    backend, monkeypatch
):
    """The manager rides the client the rest of the server already holds.

    That is the whole point of the feature: a backend with its own authorisation
    shows the operator a second device for one account, and Principle I forbids it.
    A manager that built its own connection would pass a naive test and reintroduce
    exactly the thing being removed.
    """
    client = _Client()
    _wire(monkeypatch, backend, client)

    manager = await backend.secret_manager("acct")

    assert manager._client is client, "the manager must ride the account's own client"
    assert client.handlers, "start() subscribes to the encryption updates"


@pytest.mark.asyncio
async def test_a_second_call_reuses_the_manager(backend, monkeypatch):
    """One manager per account. Two would mean two subscriptions and two key stores."""
    client = _Client()
    _wire(monkeypatch, backend, client)

    first = await backend.secret_manager("acct")
    second = await backend.secret_manager("acct")

    assert first is second
    assert len(client.handlers) == 1, "start() is idempotent; a second subscription leaks"


@pytest.mark.asyncio
async def test_a_reconfigured_account_does_not_reuse_the_old_generation(backend, monkeypatch):
    """A cached manager is keyed on the CLIENT it was verified against, not the label.

    One session under two labels is still one key, and one label across two
    reconfigurations is two clients. Returning the first manager after the account
    was rebuilt hands the caller a backend wired to a client nobody uses any more -
    the bug `_verified_against` exists to prevent.
    """
    first_client = _Client("first")
    _wire(monkeypatch, backend, first_client)
    first = await backend.secret_manager("acct")

    second_client = _Client("second")
    monkeypatch.setattr(backend, "_telethon_client", lambda account: second_client)

    second = await backend.secret_manager("acct")

    assert second is not first, "a new client generation needs a new manager"
    assert second._client is second_client


@pytest.mark.asyncio
async def test_it_refuses_while_the_server_is_shutting_down(backend, monkeypatch):
    """Shutdown flushes key material. A backend started during the flush races it."""
    _wire(monkeypatch, backend, _Client())
    monkeypatch.setattr(backend, "_closing", True)

    with pytest.raises(backend.SecretChatUnavailable) as raised:
        await backend.secret_manager("acct")

    assert "shutting down" in str(raised.value)


@pytest.mark.asyncio
async def test_no_manager_exists_until_a_tool_asks_for_one(backend, monkeypatch):
    """Lazy, as today. An account that never opens a secret chat pays nothing."""
    _wire(monkeypatch, backend, _Client())

    assert backend._by_account == {}, "constructing nothing is the point"

    await backend.secret_manager("acct")

    assert "acct" in backend._by_account


@pytest.mark.asyncio
async def test_close_all_stops_every_manager_and_is_idempotent(backend, monkeypatch):
    """The shutdown path. `stop()` flushes each chat, and losing that is unrecoverable.

    Idempotence matters because shutdown can be reached twice - once from the signal
    handler and once from the runner's own path - and the second call must not raise.
    """
    client = _Client()
    _wire(monkeypatch, backend, client)
    await backend.secret_manager("acct")

    await backend.close_all()

    assert client.handlers == [], "stop() unsubscribes"
    assert backend._by_account == {}

    await backend.close_all()


@pytest.mark.asyncio
async def test_concurrent_callers_get_one_manager(backend, monkeypatch):
    """Two tools asking at once must not build two backends over one account.

    Two managers means two key stores for one conversation, which is the failure the
    old registry's lock existed for.
    """
    client = _Client()
    _wire(monkeypatch, backend, client)

    managers = await asyncio.gather(*(backend.secret_manager("acct") for _ in range(5)))

    assert len({id(m) for m in managers}) == 1
    assert len(client.handlers) == 1


@pytest.mark.asyncio
async def test_a_key_store_remembers_which_account_it_belongs_to(backend, monkeypatch):
    owners = _wire(monkeypatch, backend, _Client(user_id=1001))

    await backend.secret_manager("acct")

    assert '"user_id": 1001' in (owners / "acct.owner.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_label_reused_for_another_account_does_not_open_the_old_keys(backend, monkeypatch):
    """Constitution II: the store is bound to the account that wrote it. A label that
    now names a different Telegram account must not decrypt the old account's chats."""
    owners = _wire(monkeypatch, backend, _Client(user_id=1001))
    await backend.secret_manager("acct")
    # A restart: the old process's managers and store locks are gone.
    backend._by_account.clear()
    backend._verified_against.clear()
    for lock in backend._store_locks.values():
        lock.release()

    _wire(monkeypatch, backend, _Client(name="b", user_id=2002), owners=owners)
    with pytest.raises(backend.SecretChatUnavailable, match="different Telegram account"):
        await backend.secret_manager("acct")
    assert "acct" not in backend._by_account


@pytest.mark.asyncio
async def test_a_key_store_another_process_holds_is_refused(backend, monkeypatch):
    """Two server processes must never open one account's key store (27.md): the
    second is told who holds it instead of writing keys the first will overwrite."""
    from telegram_mcp.singleton import SessionLock

    owners = _wire(monkeypatch, backend, _Client())
    other_process = SessionLock(backend._store_identity("acct"), lock_dir=owners)
    other_process.acquire(grace_seconds=0.1, poll_interval=0.05)
    try:
        with pytest.raises(backend.SecretChatUnavailable, match="another telegram-mcp process"):
            await backend.secret_manager("acct")
    finally:
        other_process.release()


@pytest.mark.asyncio
async def test_the_store_lock_is_released_at_shutdown(backend, monkeypatch):
    from telegram_mcp.singleton import SessionLock

    owners = _wire(monkeypatch, backend, _Client())
    await backend.secret_manager("acct")
    await backend.close_all()

    after = SessionLock(backend._store_identity("acct"), lock_dir=owners)
    after.acquire(grace_seconds=0.1, poll_interval=0.05)  # free again
    after.release()
