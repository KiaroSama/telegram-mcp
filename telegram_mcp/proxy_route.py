"""Testing proxies, and the route an account connects through.

The route order (FR-004): the proxy fixed in ``TELEGRAM_PROXY_*`` for the account if one
is set, then direct, then the pool - the account's own if it has one, else the shared
one - healthy proxies fastest first, untested next, unhealthy last.

A route change happens on the SAME client object: its connection class and proxy are
swapped and it reconnects. No new client, no new lease generation (plan R1). A proxy is
TESTED with a separate, unauthorised ``StringSession()`` client, never the owner's
session (Constitution I), bounded per proxy and in parallelism (FR-011).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from telethon import TelegramClient, types
from telethon.errors import AuthKeyDuplicatedError
from telethon.network import ConnectionTcpFull, ConnectionTcpMTProxyRandomizedIntermediate
from telethon.network.connection.tcpmtproxy import TcpMTProxy
from telethon.sessions import StringSession

from telegram_mcp import proxy_pool
from telegram_mcp.proxy_links import Proxy
from telegram_mcp.safe_log import log_event

__all__ = [
    "BACKOFF_SECONDS",
    "NoRoute",
    "PROBE_PARALLEL",
    "Route",
    "apply_route",
    "connect",
    "order",
    "PROBE_TIMEOUT",
    "connection_for",
    "probe",
    "probe_many",
    "reset_state",
]

PROBE_TIMEOUT = 10.0
PROBE_PARALLEL = 16
BACKOFF_SECONDS = 30.0  # FR-006: after every route failed, no new attempt this soon
ROUTE_TIMEOUT = 10.0  # one route's share of the deadline while others wait behind it
_DISCONNECT_TIMEOUT = 5.0
_clock = time.monotonic


def reset_state() -> None:
    """Forget in-process route state (tests; a fresh process starts empty anyway)."""
    _failed_until.clear()


_failed_until: Dict[str, Tuple[float, str]] = {}


def connection_for(proxy: Optional[Proxy]) -> Tuple[type, Any]:
    """The Telethon connection class and ``proxy=`` argument for one route."""
    if proxy is None:
        return ConnectionTcpFull, None
    if proxy.kind == "mtproto":
        if proxy.variant == "ee":
            from telegram_mcp.proxy_faketls import ConnectionTcpMTProxyFakeTLS

            return ConnectionTcpMTProxyFakeTLS, (proxy.host, proxy.port, proxy.secret)
        return ConnectionTcpMTProxyRandomizedIntermediate, (proxy.host, proxy.port, proxy.secret)
    argument: Dict[str, Any] = {
        "proxy_type": proxy.kind,
        "addr": proxy.host,
        "port": proxy.port,
        "rdns": True,
    }
    if proxy.username:
        argument["username"] = proxy.username
    if proxy.password:
        argument["password"] = proxy.password
    return ConnectionTcpFull, argument


def _probe_client(connection: type, argument: Any, timeout: float):
    """A throwaway client: empty session, no retries, no updates. Never the owner's key."""
    from telegram_mcp.client_identity import client_identity_kwargs
    from telegram_mcp.settings import TELEGRAM_API_HASH, TELEGRAM_API_ID

    return TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        connection=connection,
        proxy=argument,
        connection_retries=0,
        retry_delay=0,
        timeout=timeout,
        receive_updates=False,
        **client_identity_kwargs(),
    )


def _reason(error: BaseException) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else ""
    # Telethon's own words when every attempt failed; with retries off it reads
    # "failed 0 time(s)", which says nothing about the proxy.
    if text.startswith("Connection to Telegram failed"):
        return "could not connect through it"
    return (text or type(error).__name__)[:80]


async def probe(
    proxy: Proxy,
    timeout: float = PROBE_TIMEOUT,
    factory: Optional[Callable[[type, Any, float], Any]] = None,
) -> Tuple[str, Optional[int]]:
    """``("reachable", ms)``, ``("timed_out", None)`` or ``("failed:<why>", None)``."""
    connection, argument = connection_for(proxy)
    started = time.monotonic()
    client = None
    try:
        client = (factory or _probe_client)(connection, argument, timeout)
        await asyncio.wait_for(client.connect(), timeout)
        return "reachable", int((time.monotonic() - started) * 1000)
    except (asyncio.TimeoutError, TimeoutError):
        return "timed_out", None
    except Exception as error:
        return f"failed:{_reason(error)}", None
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), _DISCONNECT_TIMEOUT)
            except Exception:
                pass


def total_budget() -> float:
    """How long one call may spend testing: inside the per-call time budget, with room
    left to answer. A proxy not reached in time is ``not_tested``, never guessed."""
    from telegram_mcp.tool_budget import tool_timeout_seconds

    budget = tool_timeout_seconds()
    return max(5.0, budget - 10.0) if budget else 45.0


async def probe_many(
    proxies: Iterable[Proxy],
    timeout: float = PROBE_TIMEOUT,
    parallel: int = PROBE_PARALLEL,
    factory: Optional[Callable[[type, Any, float], Any]] = None,
    store: bool = True,
    total: Optional[float] = None,
) -> Dict[str, Tuple[str, Optional[int]]]:
    """Test many at once - at most ``parallel`` connections, ``total`` seconds overall.

    Results are stored; a proxy the total bound cut off is ``not_tested`` and nothing is
    stored for it, so an untested proxy is never recorded as failed.
    """
    gate = asyncio.Semaphore(max(1, parallel))
    items: List[Proxy] = list(proxies)

    async def one(proxy: Proxy):
        async with gate:
            return await probe(proxy, timeout, factory)

    tasks = {asyncio.ensure_future(one(proxy)): proxy for proxy in items}
    done, pending = (set(), set())
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=total or total_budget())
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results: Dict[str, Tuple[str, Optional[int]]] = {}
    for task, proxy in tasks.items():
        if task in done and not task.cancelled() and task.exception() is None:
            results[proxy.fingerprint] = task.result()
        else:
            results[proxy.fingerprint] = ("not_tested", None)
    if store:
        for fingerprint, (result, latency) in results.items():
            if result != "not_tested":
                proxy_pool.record(fingerprint, result, latency)
    log_event(
        logging.INFO,
        "proxies tested",
        count=len(results),
        reachable=sum(1 for result, _ in results.values() if result == "reachable"),
        not_tested=len(pending),
    )
    return results


class NoRoute(ConnectionError):
    """Every route failed; the message names each one and why."""


@dataclass(frozen=True)
class Route:
    name: str  # "env", "direct" or a proxy's fingerprint
    connection: type
    argument: Any
    proxy: Optional[Proxy] = None


def _env_route(label: str) -> Optional[Tuple[type, Any]]:
    """The proxy fixed in ``TELEGRAM_PROXY_*`` for this account, exactly as before."""
    from telegram_mcp.proxy import _build_proxy_for_label

    argument, connection = _build_proxy_for_label(label)
    if argument is None:
        return None
    return connection or ConnectionTcpFull, argument


def order(label: str) -> List[Route]:
    """env proxy (if set), direct, then the pool best first (FR-004)."""
    routes: List[Route] = []
    env = _env_route(label)
    if env is not None:
        routes.append(Route("env", env[0], env[1]))
    routes.append(Route("direct", ConnectionTcpFull, None))
    for proxy in proxy_pool.candidates(label):
        connection, argument = connection_for(proxy)
        routes.append(Route(proxy.fingerprint, connection, argument, proxy))
    return routes


def route_named(label: str, name: Optional[str]) -> Optional[Route]:
    for candidate in order(label):
        if candidate.name == name:
            return candidate
    return None


def apply_route(client: Any, chosen: Route) -> None:
    """Put a disconnected client on ``chosen``: the same object, a different way out.

    The three fields ``TelegramClient.set_proxy`` writes, minus its poke into a live
    sender connection - there is none after a disconnect, and poking an MTProxy
    connection with a direct route's ``None`` would raise.
    """
    client._connection = chosen.connection
    client._proxy = chosen.argument
    if issubclass(chosen.connection, TcpMTProxy):
        client._init_request.proxy = types.InputClientProxy(
            *chosen.connection.address_info(chosen.argument)
        )
    else:
        client._init_request.proxy = None


def _why(error: BaseException) -> str:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return _reason(error)


async def connect(client: Any, label: str, deadline: float) -> str:
    """Connect ``client`` over the first route that works within ``deadline`` seconds.

    Each failed proxy is marked unhealthy; the working route is remembered for the next
    start. When every route fails the answer names them all, and for the next
    ``BACKOFF_SECONDS`` the same answer comes back without opening a connection.
    A burned session is never failed over: another route cannot revive it.
    """
    now = _clock()
    held = _failed_until.get(label.lower())
    if held and now < held[0]:
        raise NoRoute(held[1])

    routes = order(label)
    ends = now + deadline
    sender = getattr(client, "_sender", None)
    retries = getattr(sender, "_retries", None)
    tried: List[str] = []
    try:
        for index, chosen in enumerate(routes):
            remaining = ends - _clock()
            if remaining <= 0:
                tried.append(f"{chosen.name}: not tried (out of time)")
                continue
            last = index == len(routes) - 1
            budget = remaining if last else min(remaining, ROUTE_TIMEOUT)
            if sender is not None and retries is not None and not last:
                sender._retries = 1  # one attempt per route while others are waiting
            elif sender is not None and retries is not None:
                sender._retries = retries
            apply_route(client, chosen)
            started = _clock()
            try:
                await asyncio.wait_for(client.connect(), budget)
            except AuthKeyDuplicatedError:
                raise
            except Exception as error:
                why = _why(error)
                tried.append(f"{chosen.name}: {why}")
                if chosen.proxy is not None:
                    result = "timed_out" if why == "timed out" else f"failed:{why}"
                    proxy_pool.record(chosen.proxy.fingerprint, result)
                try:
                    await asyncio.wait_for(client.disconnect(), _DISCONNECT_TIMEOUT)
                except Exception:
                    pass
                continue
            if chosen.proxy is not None:
                latency = int((_clock() - started) * 1000)
                proxy_pool.record(chosen.proxy.fingerprint, "reachable", latency)
            proxy_pool.set_route(label, chosen.name)
            _failed_until.pop(label.lower(), None)
            if chosen.name != "direct" or tried:
                log_event(logging.WARNING, "connected over another route", route=chosen.name)
            return chosen.name
    finally:
        if sender is not None and retries is not None:
            sender._retries = retries

    message = "No route to Telegram works. Tried: " + "; ".join(tried)
    _failed_until[label.lower()] = (_clock() + BACKOFF_SECONDS, message)
    log_event(logging.ERROR, "no route to Telegram", routes=len(tried))
    raise NoRoute(message)
