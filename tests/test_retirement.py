"""Retiring a client: every shape `disconnect()` really returns, and the drain.

The bug these exist for hid behind a fake. Every existing double answered
`disconnect()` with a coroutine, so the path that matters was never taken:
under a running loop Telethon returns `asyncio.shield(loop.create_task(...))`,
a FUTURE, and `create_task` rejects a future with `TypeError`. The old guard
caught `RuntimeError` only, so it escaped from a function documented never to
raise - while the caller was half-way through swapping the client table.

`_FutureDisconnect` below is therefore the important double, not the tidy one.

No sleeps: every wait is on an event or a bounded `drain_retirements`, and the
drain budget is monkeypatched to a fraction of a second rather than waited out.
"""

import asyncio

import pytest

from telegram_mcp import retirement as mod


class _FutureDisconnect:
    """Telethon's real shape under a running loop: a shielded Future."""

    def __init__(self):
        self.closed = asyncio.Event()

    def disconnect(self):
        async def _close():
            self.closed.set()

        return asyncio.shield(asyncio.get_running_loop().create_task(_close()))


class _CoroutineDisconnect:
    """The shape with no running loop, and what every older double assumed."""

    def __init__(self):
        self.calls = 0

    async def disconnect(self):
        self.calls += 1


class _NeverFinishes:
    """A socket that never finishes closing - the reason the drain is bounded."""

    def __init__(self):
        self.started = asyncio.Event()

    def disconnect(self):
        async def _hang():
            self.started.set()
            await asyncio.Event().wait()

        return asyncio.ensure_future(_hang())


@pytest.fixture(autouse=True)
def _clean_registry():
    """Retirements are module state; a leaked task would mark the next test dirty."""
    mod._retiring.clear()
    yield
    for task in list(mod._retiring):
        task.cancel()
    mod._retiring.clear()


@pytest.mark.asyncio
async def test_a_future_returning_disconnect_is_retired_without_raising():
    """The regression. `create_task` raised TypeError on exactly this shape."""
    client = _FutureDisconnect()

    mod.retire(client)  # must not raise

    assert await mod.drain_retirements(timeout=5) == 0
    assert client.closed.is_set(), "the disconnect never actually ran"


@pytest.mark.asyncio
async def test_a_coroutine_returning_disconnect_is_still_awaited():
    client = _CoroutineDisconnect()

    mod.retire(client)

    assert await mod.drain_retirements(timeout=5) == 0
    assert client.calls == 1


@pytest.mark.asyncio
async def test_none_means_telethon_already_closed_it():
    """No loop running inside Telethon's own call: it closes and returns None."""
    mod.retire(type("_Done", (), {"disconnect": lambda self: None})())

    assert mod._retiring == set(), "nothing to wait for, so nothing may be tracked"


@pytest.mark.asyncio
async def test_a_disconnect_that_raises_is_swallowed_not_propagated():
    """A lookup must not fail because tidying up did."""

    class _Angry:
        def disconnect(self):
            raise OSError("socket already gone")

    mod.retire(_Angry())  # must not raise

    assert mod._retiring == set()


def test_retiring_without_a_running_loop_closes_it_now():
    """The ordinary case from plain synchronous code. Scheduling was useless
    here - the first version did nothing at all and left the socket open."""
    client = _CoroutineDisconnect()

    mod.retire(client)

    assert client.calls == 1, "a client retired outside the loop stayed open"


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_retirement_that_is_still_closing():
    """The lock-release window: the drain must not report done while a socket is
    still going down, because that is when a second connection claims the
    session and Telegram burns it for both."""
    release = asyncio.Event()
    finished = asyncio.Event()

    class _Slow:
        def disconnect(self):
            async def _close():
                await release.wait()
                finished.set()

            return asyncio.ensure_future(_close())

    mod.retire(_Slow())
    assert not finished.is_set()

    early = await mod.drain_retirements(timeout=0.05)
    assert early == 1, "the drain claimed a still-closing socket was done"

    release.set()
    assert await mod.drain_retirements(timeout=5) == 0
    assert finished.is_set()


@pytest.mark.asyncio
async def test_the_drain_is_bounded_and_says_what_it_left(monkeypatch):
    """Exit must not hang on a disconnect that will never complete."""
    monkeypatch.setattr(mod, "_RETIRE_DRAIN_SECONDS", 0.05)
    client = _NeverFinishes()
    mod.retire(client)
    await client.started.wait()

    loop = asyncio.get_running_loop()
    began = loop.time()
    left = await mod.drain_retirements()
    elapsed = loop.time() - began

    assert left == 1
    assert elapsed < 2, f"the drain outran its own budget ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_a_cancelled_retirement_leaves_nothing_tracked():
    """A cancelled disconnect is finished as far as shutdown is concerned; it
    must not keep the drain waiting forever."""
    client = _NeverFinishes()
    mod.retire(client)
    await client.started.wait()
    assert len(mod._retiring) == 1

    for task in list(mod._retiring):
        task.cancel()

    assert await mod.drain_retirements(timeout=5) == 0
    assert mod._retiring == set(), "the done callback never removed the task"
