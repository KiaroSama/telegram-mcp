"""Keeping a client actually connected, and reconnecting it when it is not.

Separate from ``connection`` because it answers a different question. That module
decides WHICH accounts exist and hands back their clients; this one decides
whether the socket in front of you still works and, if not, brings it back -
once, under one deadline, with one caller at a time.

The distinction that drives the whole file: "the socket is open" and "the server
answers" are not the same thing, and only the second matters. A client can be
`is_connected()` and still be talking to nothing.
"""

import asyncio
import logging
import time

from telethon import TelegramClient, functions
from telethon.errors import AuthKeyDuplicatedError, RPCError

from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import StartupMessage

_last_conn_verified: dict[int, float] = {}
_RECONNECT_LOCKS: dict[int, asyncio.Lock] = {}
_CONN_VERIFY_INTERVAL: float = 30.0  # seconds between live pings
_RECONNECT_TIMEOUT: float = 30.0  # seconds before a reconnect attempt is abandoned

# Raised from two places — the reconnect's connect(), and the liveness probe, which
# meets it first when Telegram invalidates the key on first use after the clash.
_BURNED_SESSION_MESSAGE = (
    "Telegram session is no longer usable: the same session string was "
    "used by another client at the same time (AuthKeyDuplicatedError). "
    "Give each concurrent client its own session via "
    "TELEGRAM_SESSION_STRINGS or TELEGRAM_SESSION_STRING_<LABEL>, then "
    "regenerate the burned session with `uv run session_string_generator.py`."
)


async def _force_reconnect(cl: TelegramClient):
    """Disconnect + reconnect this client, one caller at a time.

    The client object is SHARED: every tool call for an account gets the same one
    from `clients`. Two concurrent callers that both found the socket dead used to
    interleave here — A disconnects, B disconnects, A connects, B tears down the
    connection A just brought up. The lock serialises them; the re-check after
    acquiring it means the second caller returns instead of reconnecting a client
    the first one already fixed.
    """
    key = id(cl)
    # ONE deadline over the whole operation. Only `connect()` used to be bounded,
    # and it is the phase least likely to hang: waiting for another caller's lock,
    # closing a half-dead socket and asking Telegram whether the session is still
    # authorized are all round trips that can sit forever, and each of them did so
    # outside the budget that claimed to cover the reconnect.
    phase = "waiting for another reconnect to finish"
    try:
        async with asyncio.timeout(_RECONNECT_TIMEOUT):
            async with _RECONNECT_LOCKS.setdefault(key, asyncio.Lock()):
                phase = "re-checking the connection"
                if cl.is_connected() and time.time() - _last_conn_verified.get(key, 0.0) < (
                    _CONN_VERIFY_INTERVAL
                ):
                    return
                log_event(logging.WARNING, "forcing a reconnect")
                phase = "closing the old connection"
                try:
                    await cl.disconnect()
                except Exception:
                    pass
                phase = "opening a new connection"
                await cl.connect()
                phase = "checking that the session is still authorized"
                await _after_connect(cl, key)
    except AuthKeyDuplicatedError as exc:
        # Telegram permanently invalidates an auth key used from two IPs at
        # once, so retrying here can never succeed — surface it instead of
        # letting the caller sit in a reconnect loop.
        raise StartupMessage(_BURNED_SESSION_MESSAGE) from exc
    except TimeoutError as exc:
        # Names the PHASE, because "it timed out" does not tell an operator
        # whether Telegram is unreachable or another call is wedged holding the
        # lock. No session material appears here.
        raise StartupMessage(
            f"Reconnecting to Telegram timed out after {_RECONNECT_TIMEOUT:.0f}s "
            f"while {phase}."
        ) from exc


async def _after_connect(cl: TelegramClient, key: int) -> None:
    """The authorization check and the success bookkeeping, inside the deadline."""
    if not await cl.is_user_authorized():
        log_event(
            logging.ERROR,
            "not authorized after reconnect; refusing interactive login",
        )
        # A raise, not a call to Telethon's start(). That method defaults to
        # `phone=lambda: input(...)` — a synchronous read inside a coroutine, which
        # blocks the whole event loop, and on stdio it reads the same stdin the MCP
        # protocol speaks over. Wrapping it in a timeout cannot save us: the timeout
        # is scheduled on the very loop input() has stopped, so it never fires. The
        # server would hang silently and permanently. runner.py refuses the same
        # thing at startup for the same reason.
        raise StartupMessage(
            "Telegram session is no longer authorized. Interactive phone login is "
            "disabled for the MCP server because it runs over stdio. Regenerate the "
            "session with `uv run session_string_generator.py` and update "
            "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env; "
            "`Manage-Accounts.ps1` does both for a labelled account."
        )
    _last_conn_verified[key] = time.time()
    log_event(logging.WARNING, "forced reconnect succeeded")


async def ensure_connected(cl: TelegramClient = None):
    """Verify Telegram connection is alive, reconnect if needed.

    is_connected() can return True when the underlying TCP socket is dead.
    We periodically send a lightweight request to verify the connection
    actually works, and force-reconnect on any failure.

    Accepts an explicit client; falls back to the default single-account
    client when called without one.
    """
    if cl is None:
        # Imported here, not at module scope: `connection` owns the registry and
        # imports THIS module, so naming it at the top would close the cycle.
        from telegram_mcp.connection import get_client

        cl = get_client()

    key = id(cl)

    if not cl.is_connected():
        await _force_reconnect(cl)
        return

    # Skip verification if recently confirmed alive
    now = time.time()
    if now - _last_conn_verified.get(key, 0.0) < _CONN_VERIFY_INTERVAL:
        return

    # Verify with a lightweight Telegram API call
    try:
        await asyncio.wait_for(
            cl(functions.help.GetNearestDcRequest()),
            timeout=5.0,
        )
    except AuthKeyDuplicatedError as exc:
        # Also an RPCError, so "the server answered" is literally true — but what it
        # answered is that this session is permanently dead. Falling through to the
        # branch below would record it as verified and send the caller on to a tool
        # call that fails generically, discarding the one message that says how to
        # recover. Must precede the RPCError branch.
        raise StartupMessage(_BURNED_SESSION_MESSAGE) from exc
    except RPCError:
        # The server ANSWERED — it just refused. That is proof the socket is alive,
        # which is the only question this function asks. FloodWaitError is the case
        # that made this matter: reconnecting while the account is rate-limited is
        # exactly the wrong move, and the old `except (..., Exception)` caught every
        # RPC refusal as if the transport had died.
        _last_conn_verified[key] = now
    except Exception:
        # Transport-level: ConnectionError / OSError / asyncio.TimeoutError, and
        # anything else that is NOT the server talking back — including
        # TypeNotFoundError, where Telethon could not parse the reply and the read
        # buffer is desynchronised (see runtime.py:333-341).
        #
        # asyncio.CancelledError is a BaseException and is deliberately NOT caught:
        # a cancelled tool call must not drag the shared client through a reconnect.
        await _force_reconnect(cl)
    else:
        _last_conn_verified[key] = now


__all__ = [
    "_BURNED_SESSION_MESSAGE",
    "_CONN_VERIFY_INTERVAL",
    "_RECONNECT_LOCKS",
    "_RECONNECT_TIMEOUT",
    "_force_reconnect",
    "_last_conn_verified",
    "ensure_connected",
]
