"""Application entrypoints for the Telegram MCP server."""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

from telethon.errors import AuthKeyDuplicatedError

from telegram_mcp import runtime as _runtime
from telegram_mcp.connection import _BURNED_SESSION_MESSAGE, harden_env_file, parse_port
from telegram_mcp.paging import bounded_number
from telegram_mcp.safe_log import safe_exception
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

# Every transport this server can actually run. Anything else is a typo.
_TRANSPORTS = ("stdio", "http", "sse")


def _lock_grace_seconds() -> float:
    """The configured lock grace period, or a loud error for anything unusable.

    ``nan`` used to sail straight through: every ``time.monotonic() >= nan``
    comparison is false, so the "wait briefly, then give up" loop waited for
    ever. A malformed value silently became the default, which hid the typo.
    """
    raw = os.getenv("TELEGRAM_LOCK_GRACE_SECONDS")
    if not raw:
        return DEFAULT_GRACE_SECONDS
    # The same rule the lock itself applies, from the same table: a local
    # finite/non-negative test accepted 1e18, which is an unbounded wait written
    # in digits and would have stalled startup rather than failing it.
    span = bounded_number(raw, "lock_grace_seconds")
    if span.error:
        raise ValidationError(f"TELEGRAM_LOCK_GRACE_SECONDS: {span.error}")
    return span.value


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


def _binds_beyond_this_machine(host: str) -> bool:
    """Whether ``host`` accepts connections from anywhere but this machine.

    Unparseable names answer True. A hostname here is almost always a deliberate
    public bind, and guessing "probably local" about an address that decides who
    can reach a Telegram account is the wrong direction to be wrong in.
    """
    import ipaddress

    candidate = (host or "").strip().strip("[]")
    if candidate.lower() in {"localhost", ""}:
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return True
    # 0.0.0.0 and :: are not "unspecified" in any harmless sense here - they mean
    # every interface the machine has.
    return not address.is_loopback


def _refuse_unauthenticated_remote_bind(host: str) -> None:
    """Stop a remote bind that nothing authenticates.

    `MCP_ALLOWED_HOSTS` and the DNS-rebinding protection it enables are not
    authentication. They check which name a request arrived under, which stops a
    browser on the operator's own machine being tricked into calling this server
    - it asks nothing about WHO is calling. Bound to a routable address without
    something in front that does, every tool here is available to anyone who can
    reach the port: read any conversation, send as the account, delete history.

    This server implements no authentication of its own, and that is deliberate -
    a hand-rolled token scheme that no real client has exercised would read as
    protection without being any. So the safe configurations are the two where
    something else is doing the work, and both have to be stated explicitly.
    """
    if not _binds_beyond_this_machine(host):
        return
    if _parse_bool_env(os.getenv("MCP_TRUSTED_PROXY_AUTH"), False):
        return
    if _parse_bool_env(os.getenv("MCP_ALLOW_UNAUTHENTICATED_REMOTE"), False):
        print(
            f"WARNING: serving on {host} with no authentication, because "
            "MCP_ALLOW_UNAUTHENTICATED_REMOTE is set. Anyone who can reach this "
            "port controls the configured Telegram account(s).",
            file=sys.stderr,
        )
        return

    raise ValidationError(
        f"Refusing to serve on {host}: that address is reachable from outside this "
        "machine and nothing here authenticates callers. Every tool on this server "
        "acts as your Telegram account.\n"
        "  - Keep it local (the default): unset MCP_HOST, or set it to 127.0.0.1.\n"
        "  - Behind a reverse proxy that authenticates requests: set "
        "MCP_TRUSTED_PROXY_AUTH=1 to state that it does.\n"
        "  - Deliberately open, on a trusted private network: set "
        "MCP_ALLOW_UNAUTHENTICATED_REMOTE=1.\n"
        "MCP_ALLOWED_HOSTS is not an answer here - it checks which name a request "
        "used, never who sent it."
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

    An unrecognised name is refused rather than quietly falling back to stdio:
    `MCP_TRANSPORT=htpp` used to produce a server that reported a healthy start
    and an HTTP client that could never reach it.
    """
    if transport not in _TRANSPORTS:
        raise ValidationError(
            f"Invalid MCP_TRANSPORT {transport!r}. Expected one of: "
            f"{', '.join(sorted(_TRANSPORTS))}."
        )
    if transport in ("http", "sse"):
        host = os.getenv("MCP_HOST", "127.0.0.1")
        # Before the port is opened, not after: a refusal that arrives once the
        # socket is already listening has already been too late.
        _refuse_unauthenticated_remote_bind(host)
        mcp.settings.host = host
        mcp.settings.port = parse_port(os.getenv("MCP_PORT", "8765"), "MCP_PORT")
        _configure_transport_security()
        if transport == "http":
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_sse_async()
    else:
        # Use the asynchronous entrypoint instead of mcp.run()
        await mcp.run_stdio_async()


# Startup failures this package raises itself, whose message is a fixed sentence
# telling the operator what to change. Everything else -- Telethon, sqlite, the
# OS -- carries whatever the failing call was given, and is reduced to a type,
# a length and a digest.
#
# The narrowness is the point: a server that will not start prints one line, and
# an operator who cannot read it has nothing else to go on. `RuntimeError` is on
# the list because the three that reach here are raised by `_reject_duplicate_
# sessions`, `_connect_authorized_client` and `connection._force_reconnect`, each
# with a literal sentence. A RuntimeError raised elsewhere with caller data would
# also print; that is the residual cost of keeping this readable, and it is
# bounded to the startup path, which runs before any tool call.
_READABLE_STARTUP_ERRORS = (ValidationError, SessionLockError, RuntimeError)


def _startup_text(error: BaseException) -> str:
    if isinstance(error, _READABLE_STARTUP_ERRORS):
        return str(error)
    return safe_exception(error)


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
                # stderr may be persisted by the launcher, so this says what
                # failed and where, never what the failing call was given.
                print(
                    f"Entity cache warm failed: {safe_exception(warm_exc)}",
                    file=sys.stderr,
                )

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
        print(f"Error starting client: {_startup_text(e)}", file=sys.stderr)
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
    # Startup, not import: this touches a file's permissions, and a library that
    # does that merely by being imported is a surprise. `.env` holds the API hash
    # and, in the single-account setup, a session string; the documented
    # `cp .env.example .env` leaves it 0644 under a normal umask.
    harden_env_file()
    _configure_allowed_roots_from_cli(sys.argv[1:])
    _runtime._apply_exposed_tools_mode()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
