"""Application entrypoints for the Telegram MCP server."""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

import math

from telethon.errors import AuthKeyDuplicatedError

from telegram_mcp import runtime as _runtime
from telegram_mcp.connection import _BURNED_SESSION_MESSAGE
from telegram_mcp.runtime import *
from telegram_mcp.singleton import (
    DEFAULT_GRACE_SECONDS,
    SessionLock,
    SessionLockError,
    session_identity,
)
import telegram_mcp.tools  # noqa: F401 - registers MCP tools via decorators

# Populated as each account's session lock is acquired; released in _main's
# finally block so a lock is never held past this process's lifetime.
_session_locks: dict[str, SessionLock] = {}


def _lock_grace_seconds() -> float:
    """The configured lock grace period, or a loud error for anything unusable.

    ``nan`` used to sail straight through: every ``time.monotonic() >= nan``
    comparison is false, so the "wait briefly, then give up" loop waited for
    ever. A malformed value silently became the default, which hid the typo.
    """
    raw = os.getenv("TELEGRAM_LOCK_GRACE_SECONDS")
    if not raw:
        return DEFAULT_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"TELEGRAM_LOCK_GRACE_SECONDS must be a number of seconds, got {raw!r}."
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(
            "TELEGRAM_LOCK_GRACE_SECONDS must be finite and non-negative, " f"got {raw!r}."
        )
    return value


def _reject_duplicate_sessions(configured: dict) -> None:
    """Refuse to start when two labels name the SAME Telegram session.

    Two labels, one auth key is not a configuration that can work: Telegram
    permanently invalidates a key used twice at once. Caught here it is a
    one-line error naming both labels; caught by the session lock it is a
    20-second stall followed by a message about "another process" that does
    not exist.
    """
    by_identity: dict[str, str] = {}
    for label, client in configured.items():
        identity = session_identity(client)
        first = by_identity.setdefault(identity, label)
        if first != label:
            raise RuntimeError(
                f"Accounts '{first}' and '{label}' are configured with the same "
                "Telegram session. One session is one auth key, and connecting it "
                "twice makes Telegram invalidate it for both. Generate a separate "
                "session per account with `uv run session_string_generator.py`."
            )


async def _connect_authorized_client(label, client) -> None:
    # First, prevent our own duplicate-spawn case outright: an exclusive
    # per-session lock means a second instance of this server never even
    # attempts to connect while another instance already holds the same
    # session (see telegram_mcp/singleton.py for why and how).
    lock = SessionLock(session_identity(client))
    await asyncio.to_thread(lock.acquire, grace_seconds=_lock_grace_seconds())
    _session_locks[label] = lock

    # No retry. Telegram invalidates an auth key used from two places at once
    # permanently -- connection.py has said so on the reconnect path all along
    # -- so the four attempts here only spent 2+4+8 seconds re-asking for a key
    # that can never come back, and then reported the raw Telethon error
    # instead of the sentence that says how to recover.
    try:
        await client.connect()
    except AuthKeyDuplicatedError as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        # Nothing is connected on this session, so nothing should still be
        # holding its lock: a retry after fixing the config must not queue
        # behind a lock this failed attempt left standing.
        _session_locks.pop(label, None)
        lock.release()
        raise RuntimeError(f"[{label}] {_BURNED_SESSION_MESSAGE}") from exc

    if await client.is_user_authorized():
        return

    raise RuntimeError(
        f"Telegram client '{label}' is not authorized. Interactive phone login "
        "is disabled for the MCP server because it runs over stdio. Generate a "
        "session string with `uv run session_string_generator.py`, then set "
        "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env. "
        "For existing file sessions, run the login outside the MCP server first."
    )


def _configure_transport_security() -> None:
    """Wire MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS into FastMCP's DNS-rebinding
    protection, e.g. when the server sits behind a reverse proxy on a public
    domain instead of only being reached via 127.0.0.1/localhost.
    """
    raw_hosts = os.getenv("MCP_ALLOWED_HOSTS", "")
    allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
    if not allowed_hosts:
        return

    from mcp.server.transport_security import TransportSecuritySettings

    raw_origins = os.getenv("MCP_ALLOWED_ORIGINS", "")
    allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


async def _serve(transport: str) -> None:
    """Run the MCP server on the selected transport.

    HTTP transports let one long-lived process hold a single shared Telegram
    connection while multiple local MCP clients connect over HTTP, instead of
    each client spawning its own Telethon session (which Telegram
    throttles/flags). "http" is streamable HTTP — the current MCP transport
    that Claude Code (`--transport http`) and Codex (`--url`) speak natively;
    "sse" is kept for clients that only support the legacy SSE transport.
    """
    if transport in ("http", "sse"):
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8765"))
        _configure_transport_security()
        if transport == "http":
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_sse_async()
    else:
        # Use the asynchronous entrypoint instead of mcp.run()
        await mcp.run_stdio_async()


async def _main() -> None:
    try:
        labels = ", ".join(clients.keys())
        _reject_duplicate_sessions(clients)
        print(f"Starting {len(clients)} Telegram client(s) ({labels})...", file=sys.stderr)
        await asyncio.gather(
            *(_connect_authorized_client(label, cl) for label, cl in clients.items())
        )

        # Warm entity caches — StringSession has no persistent cache,
        # so fetch all dialogs once per client to populate them.
        # Runs in background: blocking startup on this (e.g. under a
        # GetDialogsRequest flood wait) makes MCP clients time out, and
        # resolve_entity() re-warms the cache on miss anyway.
        print("Warming entity caches (background)...", file=sys.stderr)

        async def _warm_caches() -> None:
            try:
                await asyncio.gather(*(cl.get_dialogs() for cl in clients.values()))
                print("Entity caches warmed.", file=sys.stderr)
            except Exception as warm_exc:
                print(f"Entity cache warm failed: {warm_exc}", file=sys.stderr)

        # Held deliberately: asyncio keeps only a weak reference to a running task,
        # so dropping this name can let the cache warm-up be collected mid-flight.
        warm_task = asyncio.create_task(_warm_caches())  # noqa: F841

        transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
        print(
            f"Telegram client(s) started ({labels}). Running MCP server ({transport})...",
            file=sys.stderr,
        )
        await _serve(transport)
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        elif isinstance(e, SessionLockError):
            print(
                "Another instance of this MCP server already holds this Telegram "
                "session (e.g. the client restarted the connector without the old "
                "process exiting yet). This instance is exiting instead of "
                "connecting a second time, which would risk Telegram invalidating "
                "the session for both. Retry once the other instance is gone.",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        try:
            await asyncio.gather(
                *(cl.disconnect() for cl in clients.values()), return_exceptions=True
            )
        except Exception:
            pass
        for lock in _session_locks.values():
            lock.release()
        _session_locks.clear()


def main() -> None:
    _configure_allowed_roots_from_cli(sys.argv[1:])
    _runtime._apply_exposed_tools_mode()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
