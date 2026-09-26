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
        # Set the instant the hung phase is ENTERED, so a test that needs the
        # reconnect to be INSIDE that phase can wait for the fact rather than for
        # a number of event-loop turns. `await asyncio.sleep(0)` yields exactly
        # one turn, and one turn is not enough on a loaded runner: the first
        # caller had not yet taken the reconnect lock, so the second took it,
        # walked on to the connect phase and reported a different - also correct -
        # message. That is the 2026-09-19 windows-latest flake.
        self.hanging = asyncio.Event()

    def is_connected(self):
        return self.connected

    async def _maybe_hang(self, phase):
        if self.hang == phase:
            self.hanging.set()
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

    # Checked before the message, because it decides what a failure MEANS. There
    # is one budget over the whole reconnect, so this case only proves what it
    # claims if the budget expired INSIDE the hung phase. Nothing yields to the
    # loop between entering the timeout and reaching the hang - the fake's other
    # phases return without awaiting anything - so the only way to arrive here
    # with this unset is the process losing 50ms to preemption, and then the
    # message names an earlier phase for a reason that has nothing to do with
    # what is under test. Saying so beats an unexplained phase mismatch.
    assert client.hanging.is_set(), (
        f"the budget expired before the {phase} phase was entered, so this run "
        "says nothing about whether that phase is inside it"
    )
    assert named in str(raised.value), f"the message did not name the {phase} phase"


@pytest.mark.asyncio
async def test_waiting_for_another_callers_lock_is_bounded_too(monkeypatch):
    """The client is SHARED, so a wedged reconnect held the lock and every other
    caller queued behind it without any deadline of its own.

    Two things have to be true at once for this to test what it says, and the
    version that flaked on windows-latest guaranteed neither. The holder must
    have TAKEN the lock, and it must still be HOLDING it while the waiter waits.
    """
    wedged = _Client(hang="connect")

    # The holder gets a budget it will not reach. Under the autouse 0.05s the
    # holder times out 50ms after it starts and RELEASES the lock, so a waiter
    # that arrives a moment later finds the lock free, walks on to the connect
    # phase and times out there - which is how this test came to assert the lock
    # path and exercise the connect path.
    monkeypatch.setattr(mod, "_RECONNECT_TIMEOUT", 30.0)
    first = asyncio.ensure_future(mod._force_reconnect(wedged))

    # The real synchronisation point. `_force_reconnect` takes the lock BEFORE it
    # runs any phase, so a client that has entered its hung phase is a client
    # whose caller is holding the lock - exactly the state under test - and that
    # is a fact to wait for, not a number of turns to guess at.
    await asyncio.wait_for(wedged.hanging.wait(), timeout=5)

    # Only now does the waiter get a short budget, and the only thing it can
    # spend that budget on is the lock.
    monkeypatch.setattr(mod, "_RECONNECT_TIMEOUT", 0.05)
    with pytest.raises(StartupMessage) as raised:
        await asyncio.wait_for(mod._force_reconnect(wedged), timeout=5)

    # Still load-bearing: if the lock wait moved back outside the budget, the
    # waiter would block on the lock forever, the outer `wait_for` would raise
    # TimeoutError rather than StartupMessage, and `pytest.raises` would fail.
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


# --- the reconnect goes through the account's route (spec 005, FR-004/FR-005) -------------


@pytest.fixture
def routed(monkeypatch):
    from telegram_mcp import connection, proxy_route

    calls = []
    client = _Client()

    async def _connect(cl, label, remaining):
        calls.append((cl, label, remaining))
        cl.connected = True
        return "direct"

    monkeypatch.setattr(mod, "_RECONNECT_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "clients", {"main": client})
    monkeypatch.setattr(proxy_route, "connect", _connect)
    return client, calls


def test_a_known_account_reconnects_through_its_route_within_the_same_deadline(routed):
    client, calls = routed
    asyncio.run(mod._force_reconnect(client))
    assert [(c, label) for c, label, _ in calls] == [(client, "main")]
    assert 0 < calls[0][2] <= 5.0


def test_a_routed_reconnect_still_refuses_interactive_login(routed):
    client, calls = routed
    client.authorized = False
    with pytest.raises(StartupMessage, match="no longer authorized"):
        asyncio.run(mod._force_reconnect(client))
    assert calls


def test_when_no_route_works_the_answer_names_them(routed, monkeypatch):
    from telegram_mcp import proxy_route

    client, _ = routed

    async def _none(cl, label, remaining):
        raise proxy_route.NoRoute("No route to Telegram works. Tried: direct: refused")

    monkeypatch.setattr(proxy_route, "connect", _none)
    with pytest.raises(StartupMessage, match="Tried: direct: refused"):
        asyncio.run(mod._force_reconnect(client))


def test_a_client_that_belongs_to_no_account_connects_as_before(monkeypatch):
    from telegram_mcp import connection, proxy_route

    monkeypatch.setattr(connection, "clients", {})

    async def _never(*args):
        raise AssertionError("an unknown client must not be routed")

    monkeypatch.setattr(proxy_route, "connect", _never)
    client = _Client()
    monkeypatch.setattr(mod, "_RECONNECT_TIMEOUT", 5.0)
    asyncio.run(mod._force_reconnect(client))
    assert client.connected
