"""Connecting as an account: proxies, the session pool, discovery, reconnection.

Split out of `test_runtime.py` when the code did the same. The subject is
`telegram_mcp/connection.py`, and the patch seams are on THAT module - it owns the
names its own functions read, so rebinding `runtime._build_client` would set a second
name and change nothing.
"""

import asyncio
import subprocess
import sys

import pytest

from telegram_mcp import connection, runtime


def _clear_session_env(monkeypatch):
    for key in list(runtime.os.environ):
        if key.startswith("TELEGRAM_SESSION_STRING") or key.startswith("TELEGRAM_SESSION_NAME"):
            monkeypatch.delenv(key, raising=False)


class _FakeTelegramClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeStringSession:
    def __init__(self, value):
        self.value = value


def test_discover_accounts_supports_suffixed_and_default_sessions(monkeypatch):
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", "work-session")
    monkeypatch.setenv("TELEGRAM_SESSION_NAME_PERSONAL", "personal.session")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "default-session")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    # An object, not a string: that is what `StringSession` really returns, and
    # it is how a string session is told apart from a file-session NAME, which
    # gets resolved to a real location before Telethon sees it.
    monkeypatch.setattr(connection, "StringSession", _FakeStringSession)

    accounts = runtime._discover_accounts()

    assert sorted(accounts) == ["default", "personal", "work"]
    assert accounts["work"].args[0].value == "work-session"
    # A file session is resolved to a real location before Telethon sees it: a
    # bare name used to make Telethon create the auth-key database wherever the
    # process happened to start, with whatever the umask gave it.
    assert accounts["personal"].args[0] == str(connection.session_file_path("personal.session"))
    assert accounts["default"].args[0].value == "default-session"


def test_discover_accounts_exits_when_no_sessions_configured(monkeypatch):
    _clear_session_env(monkeypatch)

    with pytest.raises(SystemExit):
        runtime._discover_accounts()


def _clear_proxy_env(monkeypatch):
    for key in list(runtime.os.environ):
        if key.startswith("TELEGRAM_PROXY_"):
            monkeypatch.delenv(key, raising=False)


def test_build_proxy_returns_none_when_unset(monkeypatch):
    _clear_proxy_env(monkeypatch)
    assert runtime._build_proxy_for_label("default") == (None, None)


def _stub_python_socks(monkeypatch):
    """Make ``import python_socks`` succeed without installing the package."""
    import sys
    import types

    stub = types.ModuleType("python_socks")
    monkeypatch.setitem(sys.modules, "python_socks", stub)


def test_build_proxy_socks5_with_credentials(monkeypatch):
    _clear_proxy_env(monkeypatch)
    _stub_python_socks(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "1080")
    monkeypatch.setenv("TELEGRAM_PROXY_USERNAME", "alice")
    monkeypatch.setenv("TELEGRAM_PROXY_PASSWORD", "secret")
    monkeypatch.setenv("TELEGRAM_PROXY_RDNS", "false")

    proxy, connection = runtime._build_proxy_for_label("default")

    assert connection is None
    assert proxy == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": False,
        "username": "alice",
        "password": "secret",
    }


def test_build_proxy_per_label_overrides_default(monkeypatch):
    _clear_proxy_env(monkeypatch)
    _stub_python_socks(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "1080")
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE_WORK", "http")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST_WORK", "proxy.work.example")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT_WORK", "3128")

    proxy, connection = runtime._build_proxy_for_label("work")

    assert connection is None
    assert proxy["proxy_type"] == "http"
    assert proxy["addr"] == "proxy.work.example"
    assert proxy["port"] == 3128


def test_build_proxy_mtproxy_returns_connection_class(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "mtproxy")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "mtproxy.example")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "443")
    monkeypatch.setenv("TELEGRAM_PROXY_SECRET", "ee0123456789abcdef")

    proxy, connection = runtime._build_proxy_for_label("default")

    from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

    assert proxy == ("mtproxy.example", 443, "ee0123456789abcdef")
    assert connection is ConnectionTcpMTProxyRandomizedIntermediate


def test_build_proxy_rejects_unknown_type(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "carrier-pigeon")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "1080")

    with pytest.raises(runtime.ValidationError, match="Invalid TELEGRAM_PROXY_TYPE"):
        runtime._build_proxy_for_label("default")


def test_build_proxy_requires_host_and_port(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")

    with pytest.raises(runtime.ValidationError, match="HOST and TELEGRAM_PROXY_PORT"):
        runtime._build_proxy_for_label("default")


def test_build_proxy_rejects_non_integer_port(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "not-a-port")

    with pytest.raises(runtime.ValidationError, match="must be an integer"):
        runtime._build_proxy_for_label("default")


def test_build_proxy_mtproxy_requires_secret(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "mtproxy")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "mtproxy.example")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "443")

    with pytest.raises(runtime.ValidationError, match="SECRET"):
        runtime._build_proxy_for_label("default")


def test_build_proxy_socks_requires_python_socks(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "1080")

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "python_socks":
            raise ImportError("simulated missing python-socks")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(runtime.ValidationError, match="python-socks"):
        runtime._build_proxy_for_label("default")


def test_discover_accounts_passes_proxy_kwargs_to_client(monkeypatch):
    _clear_session_env(monkeypatch)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "default-session")
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "mtproxy")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "mtproxy.example")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "443")
    monkeypatch.setenv("TELEGRAM_PROXY_SECRET", "ee0123456789abcdef")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    accounts = runtime._discover_accounts()

    client = accounts["default"]
    assert client.kwargs["proxy"] == ("mtproxy.example", 443, "ee0123456789abcdef")
    from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

    assert client.kwargs["connection"] is ConnectionTcpMTProxyRandomizedIntermediate


def test_discover_accounts_passes_device_identity_kwargs_to_client(monkeypatch):
    _clear_session_env(monkeypatch)
    _clear_proxy_env(monkeypatch)
    for key in ("TELEGRAM_DEVICE_MODEL", "TELEGRAM_SYSTEM_VERSION", "TELEGRAM_APP_VERSION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "default-session")
    monkeypatch.setenv("TELEGRAM_DEVICE_MODEL", "Telegram MCP")
    monkeypatch.setenv("TELEGRAM_APP_VERSION", "3.1")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    accounts = runtime._discover_accounts()

    client = accounts["default"]
    assert client.kwargs["device_model"] == "Telegram MCP"
    assert client.kwargs["app_version"] == "3.1"
    assert "system_version" not in client.kwargs


def test_get_client_single_and_multi_account_paths(monkeypatch):
    only = object()
    monkeypatch.setattr(connection, "clients", {"only": only})
    assert runtime.get_client() is only
    assert runtime.is_multi_mode() is False

    work = object()
    personal = object()
    monkeypatch.setattr(connection, "clients", {"work": work, "personal": personal})
    assert runtime.is_multi_mode() is True
    assert runtime.get_client("WORK") is work
    with pytest.raises(ValueError, match="Account is required"):
        runtime.get_client()
    with pytest.raises(ValueError, match="Unknown account"):
        runtime.get_client("missing")


@pytest.mark.asyncio
async def test_with_account_routes_single_multi_and_readonly(monkeypatch):
    async def tool(account=None):
        return account or "single"

    monkeypatch.setattr(connection, "clients", {"default": object()})
    assert await runtime.with_account(readonly=False)(tool)() == "single"

    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})
    assert await runtime.with_account(readonly=False)(tool)() == (
        "Error: 'account' is required. Available accounts: work, personal"
    )
    assert await runtime.with_account(readonly=False)(tool)(account="work") == "work"
    import json

    assert json.loads(await runtime.with_account(readonly=True)(tool)()) == {
        "accounts": {"work": "work", "personal": "personal"}
    }


@pytest.mark.asyncio
async def test_the_fan_out_returns_one_parseable_object_per_account(monkeypatch):
    """Every tool returns JSON from format_tool_result. Welding those strings together
    with [label] markers produced something that is neither JSON nor a stable text
    format, so a caller that parsed one account's output fine broke the moment a
    second account was configured."""
    import json

    async def tool(account=None):
        return json.dumps({"results": [{"chat_id": 1, "owner": account}]})

    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})

    payload = json.loads(await runtime.with_account(readonly=True)(tool)())

    assert set(payload["accounts"]) == {"work", "personal"}
    assert payload["accounts"]["work"]["results"][0]["owner"] == "work"
    assert payload["accounts"]["personal"]["results"][0]["owner"] == "personal"


@pytest.mark.asyncio
async def test_a_tool_that_answers_in_prose_keeps_its_sentence(monkeypatch):
    """Not every read tool returns JSON — "Page out of range." and "No messages found."
    are real answers. They must survive the envelope as strings, not become null."""
    import json

    async def tool(account=None):
        return "No messages found."

    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})

    payload = json.loads(await runtime.with_account(readonly=True)(tool)())

    assert payload["accounts"] == {
        "work": "No messages found.",
        "personal": "No messages found.",
    }


@pytest.mark.asyncio
async def test_one_failing_account_does_not_discard_the_others(monkeypatch):
    """gather() without return_exceptions=True propagates the first failure out of the
    wrapper, so four healthy accounts' completed results are thrown away because a
    fifth session expired."""
    import json

    async def tool(account=None):
        if account == "broken":
            raise RuntimeError("session expired")
        return json.dumps({"results": [{"owner": account}]})

    monkeypatch.setattr(
        connection, "clients", {"work": object(), "broken": object(), "personal": object()}
    )

    payload = json.loads(await runtime.with_account(readonly=True)(tool)())

    assert payload["accounts"]["work"]["results"][0]["owner"] == "work"
    assert payload["accounts"]["personal"]["results"][0]["owner"] == "personal"
    assert payload["accounts"]["broken"] == {
        "error": "RuntimeError: session expired",
    }


class _ConnectivityClient:
    def __init__(self, *, connected=True, authorized=True, ping_error=None):
        self.connected = connected
        self.authorized = authorized
        self.ping_error = ping_error
        self.calls = []

    def is_connected(self):
        self.calls.append("is_connected")
        return self.connected

    async def disconnect(self):
        self.calls.append("disconnect")

    async def connect(self):
        self.calls.append("connect")
        self.connected = True

    async def is_user_authorized(self):
        self.calls.append("is_user_authorized")
        return self.authorized

    async def start(self):
        self.calls.append("start")
        self.authorized = True

    async def __call__(self, _request):
        self.calls.append("ping")
        if self.ping_error:
            raise self.ping_error
        return "ok"


@pytest.mark.asyncio
async def test_ensure_connected_refuses_interactive_login_for_an_unauthorized_client(monkeypatch):
    """A revoked session must end the call, not the server.

    Telethon's start() defaults to `phone=lambda: input(...)` — a synchronous read
    inside a coroutine. It blocks the event loop, so the asyncio.wait_for around it
    can never fire, and on stdio it steals the stream the MCP protocol speaks over.
    This test used to assert that start() WAS called; that pinned a hang.
    """
    client = _ConnectivityClient(connected=False, authorized=False)
    monkeypatch.setattr(connection, "_last_conn_verified", {})
    monkeypatch.setattr(connection, "_RECONNECT_LOCKS", {})

    with pytest.raises(RuntimeError, match="session_string_generator.py"):
        await runtime.ensure_connected(client)

    # The second is_connected is _force_reconnect's post-lock re-check.
    assert client.calls == [
        "is_connected",
        "is_connected",
        "disconnect",
        "connect",
        "is_user_authorized",
    ]
    assert "start" not in client.calls, "called the blocking thing"
    assert id(client) not in connection._last_conn_verified, "recorded a failed reconnect"


@pytest.mark.asyncio
async def test_ensure_connected_pings_and_reconnects_on_failed_ping(monkeypatch):
    client = _ConnectivityClient(connected=True, authorized=True, ping_error=ConnectionError())
    monkeypatch.setattr(connection, "_last_conn_verified", {})

    await runtime.ensure_connected(client)

    assert "ping" in client.calls
    assert client.calls[-3:] == ["disconnect", "connect", "is_user_authorized"]


@pytest.mark.asyncio
async def test_ensure_connected_skips_recently_verified_client(monkeypatch):
    client = _ConnectivityClient(connected=True)
    monkeypatch.setattr(connection, "_last_conn_verified", {id(client): runtime.time.time()})

    await runtime.ensure_connected(client)

    assert client.calls == ["is_connected"]


@pytest.mark.asyncio
async def test_a_rate_limit_answer_is_proof_the_connection_is_alive(monkeypatch):
    """FloodWaitError means the server RECEIVED the request, understood it, and is
    throttling the account. Tearing the connection down and dialling again under a
    flood wait is exactly the wrong response — and `except (..., Exception)` made
    every RPC refusal look like a dead socket."""
    from telethon import errors

    client = _ConnectivityClient(
        connected=True, authorized=True, ping_error=errors.FloodWaitError(request=None)
    )
    monkeypatch.setattr(connection, "_last_conn_verified", {})
    monkeypatch.setattr(connection, "_RECONNECT_LOCKS", {})

    await runtime.ensure_connected(client)

    assert client.calls == ["is_connected", "ping"], f"reconnected on a rate limit: {client.calls}"
    assert connection._last_conn_verified[id(client)] > 0, "the probe answered — record it"


@pytest.mark.asyncio
async def test_a_burned_session_is_reported_even_when_the_probe_is_what_finds_it(monkeypatch):
    """AuthKeyDuplicatedError is an RPCError, so "the server answered" is literally
    true — but what it answered is that this session is permanently dead. Reporting
    it as a live connection sends the caller on to a tool call that fails with a
    generic error, throwing away the one message that says how to recover.

    Telegram invalidates the key on first use after the clash, so the probe is a
    likely place to meet it, not an exotic one.
    """
    from telethon.errors import AuthKeyDuplicatedError

    client = _ConnectivityClient(
        connected=True, authorized=True, ping_error=AuthKeyDuplicatedError(request=None)
    )
    monkeypatch.setattr(connection, "_last_conn_verified", {})
    monkeypatch.setattr(connection, "_RECONNECT_LOCKS", {})

    with pytest.raises(RuntimeError, match="no longer usable"):
        await runtime.ensure_connected(client)

    assert id(client) not in connection._last_conn_verified, "recorded a dead session as verified"


@pytest.mark.asyncio
async def test_a_chat_level_refusal_is_also_proof_the_connection_is_alive(monkeypatch):
    """Any RPCError is the server talking back. The probe is help.GetNearestDc, so a
    refusal is unusual — but the rule is about what an answer PROVES, not about which
    error arrived."""
    from telethon import errors

    client = _ConnectivityClient(
        connected=True,
        authorized=True,
        ping_error=errors.rpcerrorlist.ChatAdminRequiredError(request=None),
    )
    monkeypatch.setattr(connection, "_last_conn_verified", {})
    monkeypatch.setattr(connection, "_RECONNECT_LOCKS", {})

    await runtime.ensure_connected(client)

    assert client.calls == ["is_connected", "ping"]


@pytest.mark.asyncio
async def test_two_callers_do_not_interleave_a_reconnect_on_a_shared_client(monkeypatch):
    """The client is shared per account, so two tool calls that both find the socket
    dead used to race: A disconnects, B disconnects, A connects, B tears down the
    connection A just brought up."""

    class _SlowConnectClient(_ConnectivityClient):
        async def connect(self):
            self.calls.append("connect")
            await asyncio.sleep(0)  # a real yield point, exactly like a socket dial
            await asyncio.sleep(0)
            self.connected = True

    client = _SlowConnectClient(connected=False, authorized=True)
    monkeypatch.setattr(connection, "_last_conn_verified", {})
    monkeypatch.setattr(connection, "_RECONNECT_LOCKS", {})

    await asyncio.gather(runtime.ensure_connected(client), runtime.ensure_connected(client))

    assert client.calls.count("connect") == 1, f"reconnected twice: {client.calls}"
    assert client.calls.count("disconnect") == 1, f"disconnected twice: {client.calls}"


class _HangingConnectClient(_ConnectivityClient):
    async def connect(self):
        self.calls.append("connect")
        await asyncio.sleep(3600)


class _DuplicatedKeyClient(_ConnectivityClient):
    async def connect(self):
        from telethon.errors import AuthKeyDuplicatedError

        self.calls.append("connect")
        raise AuthKeyDuplicatedError(request=None)


@pytest.mark.asyncio
async def test_force_reconnect_times_out_instead_of_hanging(monkeypatch):
    """connect() never returns, so the only thing that can end this is the
    timeout inside _force_reconnect. The outer wait_for is the test's OWN bound:
    without it, losing that timeout would hang the suite for an hour instead of
    failing it, and a hang reports as nothing at all."""
    client = _HangingConnectClient(connected=False, authorized=True)
    monkeypatch.setattr(connection, "_RECONNECT_TIMEOUT", 0.01)

    with pytest.raises(RuntimeError, match="timed out"):
        await asyncio.wait_for(runtime._force_reconnect(client), timeout=5)


@pytest.mark.asyncio
async def test_force_reconnect_reports_burned_session(monkeypatch):
    client = _DuplicatedKeyClient(connected=False, authorized=True)

    with pytest.raises(RuntimeError, match="no longer usable"):
        await runtime._force_reconnect(client)


class _LoginPromptingClient(_ConnectivityClient):
    """Its start() is a tripwire. Nothing in this server may call a Telethon API
    that can prompt, because the prompt is a synchronous input() on the event loop.
    """

    async def start(self):
        raise AssertionError("start() would prompt with input() and block the event loop")


@pytest.mark.asyncio
async def test_force_reconnect_refuses_to_prompt_for_a_phone_number(monkeypatch):
    client = _LoginPromptingClient(connected=False, authorized=False)
    monkeypatch.setattr(connection, "_last_conn_verified", {})

    with pytest.raises(RuntimeError, match="no longer authorized") as excinfo:
        await runtime._force_reconnect(client)

    # The operator has to know what to DO. An error that ends a long-running
    # session and only says "broken" costs them the next hour.
    assert "session_string_generator.py" in str(excinfo.value)
    assert "Manage-Accounts.ps1" in str(excinfo.value)


# --- F06: invalid configuration must be refused at startup, not much later ---


@pytest.mark.parametrize("port", ["0", "-1", "70000", "65536"])
def test_build_proxy_rejects_a_port_outside_the_usable_range(monkeypatch, port):
    """A port no socket can bind should not survive until the first connect."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "socks5")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", port)

    with pytest.raises(runtime.ValidationError, match="between 1 and 65535"):
        runtime._build_proxy_for_label("default")


def _fake_client_env(monkeypatch):
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")


@pytest.mark.parametrize("reversed_order", [False, True])
def test_discover_accounts_refuses_a_label_defined_twice(monkeypatch, reversed_order):
    """STRING_WORK and NAME_WORK silently overwrote each other.

    Which one won depended on the order `os.environ` happened to iterate in, so
    the same .env could pick a different account between runs.
    """
    _clear_session_env(monkeypatch)
    _fake_client_env(monkeypatch)
    pairs = [
        ("TELEGRAM_SESSION_STRING_WORK", "work-session"),
        ("TELEGRAM_SESSION_NAME_WORK", "work.session"),
    ]
    for key, value in reversed(pairs) if reversed_order else pairs:
        monkeypatch.setenv(key, value)

    with pytest.raises(runtime.ValidationError) as excinfo:
        runtime._discover_accounts()

    message = str(excinfo.value)
    assert "TELEGRAM_SESSION_STRING_WORK" in message
    assert "TELEGRAM_SESSION_NAME_WORK" in message


def test_discover_accounts_refuses_labels_that_differ_only_in_case(monkeypatch):
    """Labels are lower-cased, so `_WORK` and `_Work` are one account, not two.

    Passed as an explicit mapping because Windows folds environment-variable
    case itself: the clash is only reachable through os.environ on POSIX, and
    the parsing rule under test is the same on both.
    """
    _clear_session_env(monkeypatch)
    _fake_client_env(monkeypatch)
    env = {
        "TELEGRAM_SESSION_STRING_WORK": "one",
        "TELEGRAM_SESSION_STRING_Work": "two",
    }

    with pytest.raises(runtime.ValidationError, match="work"):
        runtime._discover_accounts(env=env)


def test_discover_accounts_refuses_an_empty_label_suffix(monkeypatch):
    """`TELEGRAM_SESSION_STRING_=...` produced an account labelled ''."""
    _clear_session_env(monkeypatch)
    _fake_client_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_", "nameless")

    with pytest.raises(runtime.ValidationError, match="TELEGRAM_SESSION_STRING_"):
        runtime._discover_accounts()


# --- one label rule, shared by whoever writes it and whoever reads it back -----


def test_discovery_canonicalises_a_label_the_same_way_the_writers_do(monkeypatch):
    """A label is a SUFFIX of an environment variable name, and the account
    manager and session generator both normalise one before writing it: spaces
    and hyphens become underscores, case folds.

    Discovery used to read with a weaker rule - a bare strip and lower - so a
    hand-written `TELEGRAM_SESSION_STRING_WORK-2` registered as `work-2`. No tool
    could produce that name, so the account existed and nothing could address it.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK-2", "hyphen-session")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_Second Account", "spaced-session")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    accounts = runtime._discover_accounts()

    assert sorted(accounts) == ["second_account", "work_2"]


def test_two_labels_that_normalise_to_one_account_are_refused(monkeypatch):
    """`WORK-2` and `WORK_2` are the same account once normalised, so letting both
    through would make the running identity depend on environment order - the
    exact failure the existing duplicate check exists to prevent, reached by a
    spelling the old comparison could not see.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK-2", "one")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK_2", "two")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    with pytest.raises(connection.ValidationError) as caught:
        runtime._discover_accounts()

    message = str(caught.value)
    assert "work_2" in message
    assert "TELEGRAM_SESSION_STRING_WORK-2" in message
    assert "TELEGRAM_SESSION_STRING_WORK_2" in message


def test_a_label_the_canonical_rule_refuses_is_reported_not_registered(monkeypatch):
    """Silently registering a name no tool can produce is the failure above with
    extra steps. Refuse it where the operator can still see which variable it was.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING____", "unusable")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    with pytest.raises(connection.ValidationError) as caught:
        runtime._discover_accounts()

    assert "TELEGRAM_SESSION_STRING___" in str(caught.value)


def test_an_alias_row_is_keyed_by_the_same_canonical_label(monkeypatch):
    """The alias store and the client registry have to agree on what an account is
    CALLED, or a row saved on `work_2` cannot be found by the login named
    `work_2`. One function decides for both.
    """
    from telegram_mcp import aliases

    assert aliases.account_key("WORK-2") == "work_2"
    assert aliases.account_key("  Work 2  ") == "work_2"
    assert aliases.account_key("work_2") == "work_2"
    # Not a usable account name, so it keys nothing rather than keying the wrong login.
    assert aliases.account_key("___") is None


def test_importing_the_server_raises_no_deprecation_warning_of_our_own():
    """`pythonjsonlogger.jsonlogger` moved to `pythonjsonlogger.json` and warns on
    every import of the old path. A warning the project emits about itself trains
    readers to ignore the ones that matter.

    Checked in a SUBPROCESS, not with importlib.reload: these modules configure
    logging handlers at import time, so reloading one inside the suite adds a
    second handler to the root logger and the next test to inspect logging sees
    the pollution instead of its own subject. A fresh interpreter is both
    isolated and a stricter check - it fails on the import the server actually
    performs at startup.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import telegram_mcp.connection, telegram_mcp.runtime",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "importing the server raised a DeprecationWarning as an error:\n" + result.stderr
    )
