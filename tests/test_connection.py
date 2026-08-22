"""Connecting as an account: proxies, the session pool, discovery, reconnection.

Split out of `test_runtime.py` when the code did the same. The subject is
`telegram_mcp/connection.py`, and the patch seams are on THAT module - it owns the
names its own functions read, so rebinding `runtime._build_client` would set a second
name and change nothing.
"""

import asyncio

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


def test_discover_accounts_supports_suffixed_and_default_sessions(monkeypatch):
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", "work-session")
    monkeypatch.setenv("TELEGRAM_SESSION_NAME_PERSONAL", "personal.session")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "default-session")
    monkeypatch.setattr(connection, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(connection, "StringSession", lambda value: f"StringSession:{value}")

    accounts = runtime._discover_accounts()

    assert sorted(accounts) == ["default", "personal", "work"]
    assert accounts["work"].args[0] == "StringSession:work-session"
    assert accounts["personal"].args[0] == "personal.session"
    assert accounts["default"].args[0] == "StringSession:default-session"


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
    assert (
        await runtime.with_account(readonly=True)(tool)() == "[work]\nwork\n\n[personal]\npersonal"
    )


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
async def test_ensure_connected_reconnects_disconnected_client(monkeypatch):
    client = _ConnectivityClient(connected=False, authorized=False)
    monkeypatch.setattr(connection, "_last_conn_verified", {})

    await runtime.ensure_connected(client)

    assert client.calls == ["is_connected", "disconnect", "connect", "is_user_authorized", "start"]
    assert connection._last_conn_verified[id(client)] > 0


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
    client = _HangingConnectClient(connected=False, authorized=True)
    monkeypatch.setattr(connection, "_RECONNECT_TIMEOUT", 0.01)

    with pytest.raises(RuntimeError, match="timed out"):
        await runtime._force_reconnect(client)


@pytest.mark.asyncio
async def test_force_reconnect_reports_burned_session(monkeypatch):
    client = _DuplicatedKeyClient(connected=False, authorized=True)

    with pytest.raises(RuntimeError, match="no longer usable"):
        await runtime._force_reconnect(client)
