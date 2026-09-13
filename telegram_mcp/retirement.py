"""Closing a client this process no longer serves, and waiting until it is closed.

Split out of ``connection`` when the tracking below pushed that file over the
size ceiling. It is a real seam, not a slice: retirement is the only thing here
that has to outlive the call that starts it. ``refresh_accounts`` is synchronous
and drops a replaced client on the spot, so something has to hold the handle
until the socket is actually down - and shutdown has to be able to wait for it.

The two facts this module exists for:

* **Telethon's ``disconnect()`` does not return what its docstring says.** Under
  a running loop it returns ``asyncio.shield(loop.create_task(...))`` - a
  FUTURE. With no running loop it returns ``None`` and has already closed.
* **A session lock released while a socket is still closing is a second
  connection claiming one session.** That is the window Telegram answers with
  ``AuthKeyDuplicatedError``, and it costs the slot for whoever owned it.
"""

import asyncio

# How long shutdown waits for a retired client's socket to finish closing.
# Shorter than the reconnect budget: this runs at exit, where a hung disconnect
# must not hold the process open.
_RETIRE_DRAIN_SECONDS: float = 10.0

# Retirements still closing. A set, not a count: shutdown waits on the tasks
# themselves, and a done callback removes each one.
_retiring: set = set()


async def _awaited(awaitable):
    """Adapt a non-coroutine awaitable for ``asyncio.run``, which demands one."""
    await awaitable


def retire(client) -> None:
    """Close a client this process no longer serves.

    ``get_client`` is synchronous and is called both from inside the server's
    loop and from plain code, so there are two cases and the first version
    handled only one: it scheduled the disconnect on the running loop and did
    nothing at all when there was none - which is the ordinary case, so the
    socket simply stayed open. Its own test caught that.

    Never raises. A lookup must not fail because tidying up did - and it did:
    ``create_task`` rejects a future with ``TypeError``, which is not the
    ``RuntimeError`` the old code guarded against, so it escaped from a function
    documented never to raise, during a registry swap with the client table
    half-rebuilt. ``ensure_future`` accepts either shape.
    """
    try:
        closing = client.disconnect()
    except Exception:
        return
    if closing is None:  # Telethon already closed it synchronously
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop here: close it below rather than leaving it open
    else:
        try:
            task = asyncio.ensure_future(closing)
        except Exception:
            return
        # Tracked, because dropping the handle is how a session lock came to be
        # released while the socket under it was still closing.
        _retiring.add(task)
        task.add_done_callback(_retiring.discard)
        return
    try:
        asyncio.run(closing if asyncio.iscoroutine(closing) else _awaited(closing))
    except Exception:
        try:
            closing.close()  # at least do not leave a pending coroutine
        except Exception:
            pass


async def drain_retirements(timeout: float = None) -> int:
    """Wait for retired clients to finish disconnecting; return how many did not.

    Bounded on purpose. A disconnect that will not complete must not hold the
    process open, but nor may shutdown release a session lock while the socket
    it protects is still going down.

    The budget is read HERE, not bound as a default argument: a default is
    evaluated once at import, so changing `_RETIRE_DRAIN_SECONDS` afterwards -
    which is exactly what a test does, and what an operator override would -
    silently did nothing while appearing to work.
    """
    if timeout is None:
        timeout = _RETIRE_DRAIN_SECONDS
    pending = {t for t in _retiring if not t.done()}
    if not pending:
        return 0
    _done, still_running = await asyncio.wait(pending, timeout=timeout)
    return len(still_running)


__all__ = [
    "_RETIRE_DRAIN_SECONDS",
    "_retiring",
    "drain_retirements",
    "retire",
]
