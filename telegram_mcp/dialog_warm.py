"""Warming a client's entity cache once, and letting everyone who needs it wait.

`StringSession` keeps no persistent entity cache, so the first lookup of a chat
raises `ValueError` until `get_dialogs()` has run. That warm is expensive and
must not run per lookup, which is what the TTL is for - but a TTL alone gets two
things wrong, and both were live:

* **The stamp was written before the call, not after.** A warm that failed or
  was cancelled still suppressed every retry for the whole window, so one
  transient error made every cold lookup fail for thirty seconds.
* **A concurrent caller saw that stamp and skipped.** Two cold lookups at once
  meant one warmed and the other was told a warm had just happened, declined to
  retry, and failed against a cache that was still cold.

So the in-flight warm is an owned task that waiters share, and the stamp is
written only on success. Waiting is `shield`ed: a tool call that gives up must
not cancel the warm the other waiters are relying on.
"""

import asyncio
import time
import weakref

# Per client, when its cache was last warmed SUCCESSFULLY. Weak, so a retired
# client's entry goes with it rather than pinning the client forever.
_dialog_warmed: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

# Per client, the warm currently running. A plain dict, because the task holds a
# strong reference to the client and a weak key could never be collected; the
# entry is removed as soon as the task finishes.
_dialog_warms: dict = {}

_DIALOG_WARM_SECONDS = 30.0


async def _warm_dialogs(client) -> None:
    await client.get_dialogs()
    # AFTER it completed, never before.
    _dialog_warmed[client] = time.monotonic()


def _forget_warm(client, task) -> None:
    if _dialog_warms.get(client) is task:
        _dialog_warms.pop(client, None)


async def warm_dialogs_once(client) -> bool:
    """Warm the entity cache at most once per `_DIALOG_WARM_SECONDS` per client.

    Returns True when the cache was warmed - by this call or by one it waited on
    - so a caller knows a retry can see something new. False means the cache is
    unchanged and asking again would get the same answer.
    """
    last = _dialog_warmed.get(client)
    if last is not None and time.monotonic() - last < _DIALOG_WARM_SECONDS:
        return False

    task = _dialog_warms.get(client)
    if task is None or task.done():
        task = asyncio.ensure_future(_warm_dialogs(client))
        _dialog_warms[client] = task
        task.add_done_callback(lambda finished, c=client: _forget_warm(c, finished))

    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # This caller is going away; the warm is not. Propagate, never swallow.
        raise
    except Exception:
        # Failed, so nothing was stamped and the next caller tries again.
        return False
    return True


__all__ = [
    "_DIALOG_WARM_SECONDS",
    "_dialog_warmed",
    "_dialog_warms",
    "warm_dialogs_once",
]
