"""Reconnecting is bounded as a WHOLE, not just where it was easy to bound.

`connect()` was the one phase wrapped in a timeout, and it is the phase least
likely to hang. Waiting for another caller's lock, closing a half-dead socket
and asking Telegram whether the session is still authorized are all round trips
that can sit forever, and every one of them sat outside the budget that claimed
to cover the reconnect.

The doubles here hang on an event rather than sleeping, and the budget is
monkeypatched to a fraction of a second, so nothing waits out a real timeout.
"""

import asyncio

import pytest

from telegram_mcp import reconnect as mod
from telegram_mcp.settings import StartupMessage


class _Client:
    """A client whose phases the test decides the timing of."""

    def __init__(self, hang=None):
        self.hang = hang
        self.connected = False
        self.authorized = True
        self.disconnects = 0

    def is_connected(self):
        return self.connected

    async def _maybe_hang(self, phase):
        if self.hang == phase:
            await asyncio.Event().wait()

    async def disconnect(self):
        self.disconnects += 1
        await self._maybe_hang("disconnect")

    async def connect(self):
        await self._maybe_hang("connect")
        self.connected = True

    async def is_user_authorized(self):
        await self._maybe_hang("authorize")
        return self.authorized


@pytest.fixture(autouse=True)
def _bounded(monkeypatch):
    monkeypatch.setattr(mod, "_RECONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(mod, "_RECONNECT_LOCKS", {})
    monkeypatch.setattr(mod, "_last_conn_verified", {})


@pytest.mark.parametrize(
    "phase, named",
    [
        ("disconnect", "closing the old connection"),
        ("connect", "opening a new connection"),
        ("authorize", "checking that the session is still authorized"),
    ],
)
@pytest.mark.asyncio
async def test_every_phase_is_inside_the_budget(phase, named):
    """Two of these three used to be outside it entirely."""
    client = _Client(hang=phase)

    with pytest.raises(StartupMessage) as raised:
        await asyncio.wait_for(mod._force_reconnect(client), timeout=5)

    assert named in str(raised.value), f"the message did not name the {phase} phase"


@pytest.mark.asyncio
async def test_waiting_for_another_callers_lock_is_bounded_too():
    """The client is SHARED, so a wedged reconnect held the lock and every other
    caller queued behind it without any deadline of its own."""
    wedged = _Client(hang="connect")
    first = asyncio.ensure_future(mod._force_reconnect(wedged))
    await asyncio.sleep(0)

    with pytest.raises(StartupMessage) as raised:
        await asyncio.wait_for(mod._force_reconnect(wedged), timeout=5)

    assert "waiting for another reconnect to finish" in str(raised.value)
    first.cancel()
    with pytest.raises((asyncio.CancelledError, StartupMessage)):
        await first


@pytest.mark.asyncio
async def test_a_healthy_reconnect_still_succeeds():
    client = _Client()

    await mod._force_reconnect(client)

    assert client.connected
    assert id(client) in mod._last_conn_verified


@pytest.mark.asyncio
async def test_an_unauthorized_session_is_refused_not_prompted():
    """Telethon's `start()` would read stdin - the same stdin the MCP protocol
    speaks over - from inside the event loop."""
    client = _Client()
    client.authorized = False

    with pytest.raises(StartupMessage) as raised:
        await mod._force_reconnect(client)

    assert "no longer authorized" in str(raised.value)
    assert "session_string_generator" in str(raised.value)


@pytest.mark.asyncio
async def test_the_timeout_message_carries_no_session_material():
    client = _Client(hang="connect")

    with pytest.raises(StartupMessage) as raised:
        await mod._force_reconnect(client)

    said = str(raised.value)
    assert "opening a new connection" in said
    assert "1A" not in said and "session=" not in said.lower()
