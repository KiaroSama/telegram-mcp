import pytest

from telegram_mcp import runner


class _FakeSession:
    def __init__(self, identity: str):
        self._identity = identity

    def save(self):
        return self._identity


class _FakeClient:
    def __init__(self, *, authorized: bool, identity: str = "test-identity"):
        self.authorized = authorized
        self.connected = False
        self.started = False
        self.session = _FakeSession(identity)

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def _isolate_session_locks(tmp_path, monkeypatch):
    # Give each test its own lock directory (so locks don't leak across tests
    # or collide with a real telegram-mcp instance running on the machine)
    # and a near-zero grace period (so a deliberately-contested lock in a
    # test fails fast instead of sleeping through the real default).
    import telegram_mcp.singleton as singleton_module

    original_init = singleton_module.SessionLock.__init__

    def _init_with_tmp_dir(self, session_identity, *, lock_dir=tmp_path):
        original_init(self, session_identity, lock_dir=lock_dir)

    monkeypatch.setattr(singleton_module.SessionLock, "__init__", _init_with_tmp_dir)
    # Set through the real env var rather than patching _lock_grace_seconds out:
    # the parser is itself under test below, and a stub would hide it.
    monkeypatch.setenv("TELEGRAM_LOCK_GRACE_SECONDS", "0.01")
    yield
    runner._session_locks.clear()


@pytest.mark.asyncio
async def test_connect_authorized_client_uses_existing_session_without_interactive_start():
    client = _FakeClient(authorized=True)

    await runner._connect_authorized_client("default", client)

    assert client.connected is True
    assert client.started is False


@pytest.mark.asyncio
async def test_connect_authorized_client_rejects_unauthorized_session():
    client = _FakeClient(authorized=False)

    with pytest.raises(RuntimeError, match="Interactive phone login is disabled"):
        await runner._connect_authorized_client("default", client)

    assert client.connected is True
    assert client.started is False


@pytest.mark.asyncio
async def test_connect_authorized_client_refuses_concurrent_duplicate_session():
    first = _FakeClient(authorized=True, identity="shared-session")
    second = _FakeClient(authorized=True, identity="shared-session")

    await runner._connect_authorized_client("default", first)

    with pytest.raises(runner.SessionLockError, match="already connected"):
        await runner._connect_authorized_client("default", second)

    assert second.connected is False

    runner._session_locks["default"].release()
    runner._session_locks.clear()


@pytest.mark.asyncio
async def test_connect_authorized_client_allows_different_sessions_concurrently():
    first = _FakeClient(authorized=True, identity="session-a")
    second = _FakeClient(authorized=True, identity="session-b")

    await runner._connect_authorized_client("default", first)
    await runner._connect_authorized_client("work", second)

    assert first.connected is True
    assert second.connected is True


class _FakeSettings:
    def __init__(self):
        self.host = None
        self.port = None
        self.transport_security = None


class _FakeMcp:
    def __init__(self):
        self.settings = _FakeSettings()
        self.ran = None

    async def run_stdio_async(self):
        self.ran = "stdio"

    async def run_sse_async(self):
        self.ran = "sse"

    async def run_streamable_http_async(self):
        self.ran = "http"


@pytest.mark.asyncio
async def test_serve_defaults_to_stdio(monkeypatch):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)

    await runner._serve("stdio")

    assert fake.ran == "stdio"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["htpp", "HTTPS", "", "streamable"])
async def test_serve_refuses_a_transport_it_does_not_know(monkeypatch, transport):
    """`MCP_TRANSPORT=htpp` used to start stdio and say nothing.

    The operator sees a server that started fine and a client that can never
    reach it, with no line anywhere connecting the two.
    """
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)

    with pytest.raises(runner.ValidationError, match="MCP_TRANSPORT"):
        await runner._serve(transport)

    assert fake.ran is None


@pytest.mark.asyncio
@pytest.mark.parametrize("port", ["0", "-1", "70000", "not-a-port"])
async def test_serve_refuses_an_unusable_mcp_port(monkeypatch, port):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.setenv("MCP_PORT", port)

    with pytest.raises(runner.ValidationError, match="MCP_PORT"):
        await runner._serve("http")

    assert fake.ran is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_serve_http_transports_bind_host_and_port(monkeypatch, transport):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9000")
    # 0.0.0.0 is every interface, and serving there now requires saying which
    # thing authenticates callers. Declaring the proxy contract keeps this test
    # about host/port plumbing while exercising the path a real remote bind takes.
    monkeypatch.setenv("MCP_TRUSTED_PROXY_AUTH", "1")

    await runner._serve(transport)

    assert fake.ran == transport
    assert fake.settings.host == "0.0.0.0"
    assert fake.settings.port == 9000


@pytest.mark.asyncio
async def test_serve_http_uses_default_host_and_port(monkeypatch):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)

    await runner._serve("http")

    assert fake.ran == "http"
    assert fake.settings.host == "127.0.0.1"
    assert fake.settings.port == 8765


@pytest.mark.asyncio
async def test_serve_http_leaves_transport_security_unset_by_default(monkeypatch):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

    await runner._serve("http")

    assert fake.settings.transport_security is None


@pytest.mark.asyncio
async def test_serve_http_configures_allowed_hosts(monkeypatch):
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, localhost:8765")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

    await runner._serve("http")

    security = fake.settings.transport_security
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com", "localhost:8765"]
    assert security.allowed_origins == ["https://mcp.example.com"]


# --- F05: the process lock must key on session identity, never on the label ---


def test_two_labels_sharing_one_session_string_get_the_same_lock_file(tmp_path):
    """One auth key, two labels: the lock has to be the SAME file.

    `{label}-{digest}.lock` gave `work` and `personal` a lock each for one
    StringSession, so both connected and Telegram burned the key with
    AuthKeyDuplicatedError - the exact outcome this lock exists to prevent.
    """
    from telegram_mcp.singleton import SessionLock

    first = SessionLock("string:SHARED", lock_dir=tmp_path)
    second = SessionLock("string:SHARED", lock_dir=tmp_path)

    assert first.path == second.path


@pytest.mark.asyncio
async def test_two_labels_on_one_session_are_refused_before_connecting():
    """The second label must never reach connect() with the same auth key."""
    first = _FakeClient(authorized=True, identity="one-and-only")
    second = _FakeClient(authorized=True, identity="one-and-only")

    await runner._connect_authorized_client("work", first)
    with pytest.raises(runner.SessionLockError):
        await runner._connect_authorized_client("personal", second)

    assert second.connected is False


def test_duplicate_sessions_are_named_before_anything_connects():
    """Two labels, one session: say so, rather than stall on the lock."""
    shared = _FakeClient(authorized=True, identity="one-and-only")
    other = _FakeClient(authorized=True, identity="one-and-only")

    with pytest.raises(RuntimeError, match="same"):
        runner._reject_duplicate_sessions({"work": shared, "personal": other})

    assert shared.connected is False
    assert other.connected is False


def test_distinct_sessions_pass_the_duplicate_check():
    runner._reject_duplicate_sessions(
        {
            "work": _FakeClient(authorized=True, identity="a"),
            "personal": _FakeClient(authorized=True, identity="b"),
        }
    )


def test_a_symlinked_file_session_is_the_same_identity(tmp_path):
    """A symlink alias must not buy a second lock on the same session file."""
    from telegram_mcp.singleton import session_identity

    real = tmp_path / "real.session"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "alias.session"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - unprivileged host
        pytest.skip(f"symlinks unavailable here: {exc}")

    class _FileSession:
        def __init__(self, filename):
            self.filename = filename

    class _Client:
        def __init__(self, filename):
            self.session = _FileSession(filename)

    assert session_identity(_Client(str(real))) == session_identity(_Client(str(link)))


def test_lock_grace_refuses_a_non_finite_or_negative_value(monkeypatch):
    """`nan` made the deadline comparison never true: the wait was unbounded."""
    for raw in ("nan", "inf", "-1", "not-a-number"):
        monkeypatch.setenv("TELEGRAM_LOCK_GRACE_SECONDS", raw)
        with pytest.raises(runner.ValidationError, match="TELEGRAM_LOCK_GRACE_SECONDS"):
            runner._lock_grace_seconds()


def test_acquire_rejects_an_unbounded_grace_period_up_front(tmp_path):
    """Validated before the wait loop, so a bad value can never hang the wait."""
    from telegram_mcp.singleton import SessionLock

    lock = SessionLock("string:grace-check", lock_dir=tmp_path)
    with pytest.raises(ValueError, match="grace_seconds"):
        lock.acquire(grace_seconds=float("nan"))
    with pytest.raises(ValueError, match="grace_seconds"):
        lock.acquire(grace_seconds=-1.0)
    assert lock._fh is None


class _ServableClient(_FakeClient):
    def __init__(self, identity):
        super().__init__(authorized=True, identity=identity)
        self.disconnected = False

    async def get_dialogs(self):
        return []

    async def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
async def test_main_releases_every_session_lock_on_the_way_out(monkeypatch):
    """A lock outliving the process it belongs to locks the operator out."""
    fake = _FakeMcp()
    monkeypatch.setattr(runner, "mcp", fake)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    accounts = {"work": _ServableClient("session-work"), "personal": _ServableClient("session-p")}
    monkeypatch.setattr(runner, "clients", accounts)

    await runner._main()

    assert fake.ran == "stdio"
    assert all(client.connected for client in accounts.values())
    assert all(client.disconnected for client in accounts.values())
    assert runner._session_locks == {}


@pytest.mark.asyncio
async def test_main_exits_when_two_accounts_share_one_session(monkeypatch, capsys):
    monkeypatch.setattr(runner, "mcp", _FakeMcp())
    monkeypatch.setattr(
        runner,
        "clients",
        {"work": _ServableClient("shared"), "personal": _ServableClient("shared")},
    )

    with pytest.raises(SystemExit):
        await runner._main()

    assert "same" in capsys.readouterr().err
    assert runner._session_locks == {}


@pytest.mark.asyncio
async def test_a_duplicated_auth_key_is_not_retried(monkeypatch):
    """Telegram invalidates the key permanently; four retries only burn 14s."""
    from telethon.errors import AuthKeyDuplicatedError

    slept: list[float] = []

    async def _no_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(runner.asyncio, "sleep", _no_sleep)

    class _Duplicated(_FakeClient):
        def __init__(self):
            super().__init__(authorized=True, identity="burned")
            self.attempts = 0

        async def connect(self):
            self.attempts += 1
            raise AuthKeyDuplicatedError(request=None)

    client = _Duplicated()
    with pytest.raises(RuntimeError, match="no longer usable"):
        await runner._connect_authorized_client("default", client)

    assert client.attempts == 1, "the burned key was retried"
    assert slept == [], "the run slept waiting for a key that can never come back"


# --- an address anyone can reach is not a configuration detail -----------------


@pytest.fixture
def _no_remote_env(monkeypatch):
    for name in (
        "MCP_TRUSTED_PROXY_AUTH",
        "MCP_ALLOW_UNAUTHENTICATED_REMOTE",
        "MCP_ALLOWED_HOSTS",
        "MCP_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "", "127.0.0.5"])
def test_a_loopback_bind_needs_no_permission(host, _no_remote_env):
    """The default has to stay frictionless, or the gate gets switched off wholesale."""
    runner._refuse_unauthenticated_remote_bind(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # every interface, which is the common accident
        "::",
        "192.168.1.50",
        "10.0.0.7",
        "203.0.113.9",
        "mcp.example.com",  # a name is almost always a deliberate public bind
    ],
)
def test_a_reachable_bind_is_refused_when_nothing_authenticates(host, _no_remote_env):
    """Every tool here acts as the Telegram account. Open on a routable address,
    reaching the port IS the authorization."""
    with pytest.raises(runner.ValidationError) as caught:
        runner._refuse_unauthenticated_remote_bind(host)

    message = str(caught.value)
    assert host in message
    # The message has to say what to DO, or it just gets worked around.
    assert "MCP_TRUSTED_PROXY_AUTH" in message
    assert "127.0.0.1" in message


def test_allowed_hosts_alone_does_not_open_the_gate(monkeypatch, _no_remote_env):
    """The trap this exists to close. `MCP_ALLOWED_HOSTS` enables DNS-rebinding
    protection, which checks which NAME a request arrived under - it stops a
    browser being tricked into calling this server and asks nothing about who is
    calling. Treating it as authentication is the mistake the README used to
    invite, and it is exactly the configuration a public deployment would reach
    for first.
    """
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")

    with pytest.raises(runner.ValidationError) as caught:
        runner._refuse_unauthenticated_remote_bind("0.0.0.0")

    assert "MCP_ALLOWED_HOSTS is not an answer" in str(caught.value)


def test_a_declared_authenticating_proxy_is_accepted(monkeypatch, _no_remote_env):
    """The server cannot verify that a proxy authenticates; the operator asserts it.
    An assertion that has to be written down is still worth more than a default."""
    monkeypatch.setenv("MCP_TRUSTED_PROXY_AUTH", "1")

    runner._refuse_unauthenticated_remote_bind("0.0.0.0")


def test_the_dangerous_override_works_and_says_so(monkeypatch, capsys, _no_remote_env):
    """Kept, because a private lab network is a real case - but it announces itself
    on every start rather than being a quiet flag someone set once and forgot."""
    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED_REMOTE", "1")

    runner._refuse_unauthenticated_remote_bind("0.0.0.0")

    warning = capsys.readouterr().err
    assert "no authentication" in warning
    assert "controls the configured Telegram account" in warning


def test_an_unparseable_host_is_treated_as_remote(_no_remote_env):
    """Wrong in the safe direction. Guessing "probably local" about an address that
    decides who can read someone's messages is not a guess worth making."""
    assert runner._binds_beyond_this_machine("not-an-address at all") is True
