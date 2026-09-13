"""Warming the entity cache: who waits, who retries, and what a failure costs.

The stamp used to be written BEFORE `get_dialogs()` ran, and that is two bugs in
one line. A warm that failed or was cancelled still suppressed every retry for
the full thirty-second window, so one transient error made every cold lookup
fail for half a minute. And a second caller arriving during an in-flight warm
read that stamp, concluded a warm had just happened, declined to retry, and
failed against a cache that was still cold.

Nothing here sleeps: the warm is held open on an event and released explicitly.
"""

import asyncio

import pytest

from telegram_mcp import dialog_warm as mod


class _Client:
    """Counts warms and lets the test decide when one completes."""

    def __init__(self, fail=False):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def get_dialogs(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise ConnectionError("the warm did not land")


@pytest.fixture(autouse=True)
def _clean():
    mod._dialog_warmed.clear()
    mod._dialog_warms.clear()
    yield
    for task in list(mod._dialog_warms.values()):
        task.cancel()
    mod._dialog_warms.clear()
    mod._dialog_warmed.clear()


@pytest.mark.asyncio
async def test_two_cold_lookups_share_one_warm_and_both_are_told_it_happened():
    """The second caller used to be told a warm had just happened and skip its
    retry - against a cache that had not been warmed yet."""
    client = _Client()

    first = asyncio.ensure_future(mod.warm_dialogs_once(client))
    await client.started.wait()
    second = asyncio.ensure_future(mod.warm_dialogs_once(client))
    await asyncio.sleep(0)
    client.release.set()

    assert await first is True
    assert await second is True, "the waiter was told the cache was already warm"
    assert client.calls == 1, "the warm ran twice"


@pytest.mark.asyncio
async def test_a_failed_warm_stays_retryable():
    """It stamped first, so one transient failure suppressed every retry for the
    whole window."""
    failing = _Client(fail=True)
    failing.release.set()

    assert await mod.warm_dialogs_once(failing) is False
    assert failing not in mod._dialog_warmed, "a failed warm was recorded as done"

    healthy = _Client()
    healthy.release.set()
    mod._dialog_warms.pop(failing, None)
    assert await mod.warm_dialogs_once(healthy) is True


@pytest.mark.asyncio
async def test_one_caller_giving_up_does_not_cancel_the_warm_for_the_others():
    client = _Client()

    quitter = asyncio.ensure_future(mod.warm_dialogs_once(client))
    await client.started.wait()
    stayer = asyncio.ensure_future(mod.warm_dialogs_once(client))
    await asyncio.sleep(0)

    quitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await quitter
    client.release.set()

    assert await stayer is True, "the abandoned caller took the warm down with it"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_cancelled_warm_is_retried_rather_than_remembered():
    client = _Client()
    started = asyncio.ensure_future(mod.warm_dialogs_once(client))
    await client.started.wait()

    for task in list(mod._dialog_warms.values()):
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await started

    assert client not in mod._dialog_warmed, "a cancelled warm counted as a warm"


@pytest.mark.asyncio
async def test_a_successful_warm_is_not_repeated_inside_the_window():
    """The TTL still does its job: warming is expensive and must not run per lookup."""
    client = _Client()
    client.release.set()
    assert await mod.warm_dialogs_once(client) is True

    assert await mod.warm_dialogs_once(client) is False
    assert client.calls == 1


@pytest.mark.asyncio
async def test_the_in_flight_record_does_not_outlive_the_warm():
    """It holds a strong reference to the client; left behind, it would pin every
    client this process ever retired."""
    client = _Client()
    client.release.set()
    await mod.warm_dialogs_once(client)
    await asyncio.sleep(0)

    assert mod._dialog_warms == {}
