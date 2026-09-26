"""Testing a proxy, and choosing the route an account connects through.

Part 1, the probe: a proxy is tested with a fresh, unauthorised client over that proxy's
own connection kind - never the owner's session - bounded per proxy and in parallelism,
and a probe that times out is `timed_out`, never `reachable`.
"""

import asyncio

import pytest
from telethon.network import ConnectionTcpFull, ConnectionTcpMTProxyRandomizedIntermediate

from telegram_mcp import proxy_links as pl
from telegram_mcp import proxy_pool as pool
from telegram_mcp import proxy_route as route
from telegram_mcp.proxy_faketls import ConnectionTcpMTProxyFakeTLS

PLAIN = "0123456789abcdef0123456789abcdef"
EE = "ee" + PLAIN + b"www.example.com".hex()


def _mt(host, secret=PLAIN, port=443):
    return pl.parse_link(f"tg://proxy?server={host}&port={port}&secret={secret}")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "pool_path", lambda: tmp_path / "proxy-pool.json")
    pool.reset_cache()
    route.reset_state()
    yield
    pool.reset_cache()
    route.reset_state()


# --- which connection each kind uses -----------------------------------------------------


def test_each_kind_gets_its_own_connection_and_proxy_argument():
    connection, argument = route.connection_for(_mt("1.1.1.1"))
    assert connection is ConnectionTcpMTProxyRandomizedIntermediate
    assert argument == ("1.1.1.1", 443, PLAIN)

    connection, argument = route.connection_for(_mt("1.1.1.1", secret="dd" + PLAIN))
    assert connection is ConnectionTcpMTProxyRandomizedIntermediate and argument[2] == "dd" + PLAIN

    connection, argument = route.connection_for(_mt("1.1.1.1", secret=EE))
    assert connection is ConnectionTcpMTProxyFakeTLS and argument[2] == EE

    socks = pl.parse_link("tg://socks?server=2.2.2.2&port=1080&user=u&pass=p")
    connection, argument = route.connection_for(socks)
    assert connection is ConnectionTcpFull
    assert argument == {
        "proxy_type": "socks5",
        "addr": "2.2.2.2",
        "port": 1080,
        "rdns": True,
        "username": "u",
        "password": "p",
    }

    assert route.connection_for(None) == (ConnectionTcpFull, None)


# --- the probe -----------------------------------------------------------------------------


class _Probe:
    active = 0
    peak = 0

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.disconnected = False

    async def connect(self):
        _Probe.active += 1
        _Probe.peak = max(_Probe.peak, _Probe.active)
        try:
            await self.behaviour()
        finally:
            _Probe.active -= 1

    async def disconnect(self):
        self.disconnected = True


def _factory(behaviours, made):
    def factory(connection, argument, timeout):
        client = _Probe(behaviours(argument))
        made.append((connection, argument, timeout, client))
        return client

    return factory


def test_a_reachable_proxy_reports_its_latency_and_is_disconnected():
    made = []

    async def ok():
        await asyncio.sleep(0)

    result = asyncio.run(route.probe(_mt("1.1.1.1"), factory=_factory(lambda a: ok, made)))
    assert result[0] == "reachable" and isinstance(result[1], int)
    assert made[0][3].disconnected


def test_a_probe_that_times_out_is_timed_out_never_reachable():
    made = []

    async def hang():
        await asyncio.sleep(60)

    result = asyncio.run(
        route.probe(_mt("1.1.1.1"), timeout=0.05, factory=_factory(lambda a: hang, made))
    )
    assert result == ("timed_out", None)
    assert made[0][3].disconnected


def test_a_failing_probe_names_the_failure():
    made = []

    async def refuse():
        raise ConnectionRefusedError("connection refused")

    result = asyncio.run(route.probe(_mt("1.1.1.1"), factory=_factory(lambda a: refuse, made)))
    assert result[0].startswith("failed:") and "refused" in result[0]


def test_many_probes_run_at_most_sixteen_at_once_and_store_their_results():
    made = []
    _Probe.active = _Probe.peak = 0

    async def slow():
        await asyncio.sleep(0.02)

    proxies = [_mt(f"10.0.0.{n}") for n in range(40)]
    pool.add(proxies)
    results = asyncio.run(route.probe_many(proxies, factory=_factory(lambda a: slow, made)))
    assert _Probe.peak <= 16 and len(made) == 40
    assert set(results) == {p.fingerprint for p in proxies}
    assert all(row["last_result"] == "reachable" for row in pool.describe())


def test_the_real_probe_client_is_unauthorised_and_never_retries(monkeypatch):
    from telethon.sessions import StringSession

    seen = {}

    class _Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            seen.update(session=session, **kwargs)

    monkeypatch.setattr(route, "TelegramClient", _Client)
    route._probe_client(ConnectionTcpFull, None, 10)
    assert isinstance(seen["session"], StringSession) and seen["session"].save() == ""
    assert seen["connection_retries"] == 0 and seen["receive_updates"] is False


# --- Part 2: the route order, switching on the same client, failover ----------------------

from types import SimpleNamespace  # noqa: E402

from telethon.errors import AuthKeyDuplicatedError  # noqa: E402
from telethon.network.connection.tcpmtproxy import TcpMTProxy  # noqa: E402


class _Client:
    """Stands in for the account's TelegramClient: connects if ``works`` says so."""

    def __init__(self, works=lambda client: True, hang=False):
        self.works = works
        self.hang = hang
        self._connection = ConnectionTcpFull
        self._proxy = None
        self._init_request = SimpleNamespace(proxy=None)
        self._sender = SimpleNamespace(_retries=5, _connection=None)
        self.attempts = []
        self.disconnects = 0

    async def connect(self):
        self.attempts.append((self._connection, self._proxy))
        if self.hang:
            await asyncio.sleep(60)
        if not self.works(self):
            raise ConnectionError("unreachable")

    async def disconnect(self):
        self.disconnects += 1


def _host_of(client):
    proxy = client._proxy
    if proxy is None:
        return "direct"
    return proxy["addr"] if isinstance(proxy, dict) else proxy[0]


def test_the_order_is_env_then_direct_then_the_pool_best_first(monkeypatch):
    fast, slow = _mt("1.0.0.1"), _mt("1.0.0.2")
    pool.add([slow, fast])
    pool.record(fast.fingerprint, "reachable", 100)
    pool.record(slow.fingerprint, "reachable", 900)
    monkeypatch.setattr(route, "_env_route", lambda label: (ConnectionTcpFull, {"addr": "env"}))
    assert [r.name for r in route.order("main")] == [
        "env",
        "direct",
        fast.fingerprint,
        slow.fingerprint,
    ]
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    assert [r.name for r in route.order("main")][0] == "direct"


def test_an_account_with_its_own_pool_uses_only_its_own(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    pool.add([_mt("1.1.1.1")])
    pool.add([_mt("9.9.9.9")], pool="work")
    hosts = [r.proxy.host for r in route.order("work") if r.proxy]
    assert hosts == ["9.9.9.9"]


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        _mt("1.1.1.1"),
        _mt("1.1.1.1", secret="dd" + PLAIN),
        _mt("1.1.1.1", secret=EE),
        pl.parse_link("socks5://2.2.2.2:1080"),
        pl.parse_link("socks4://2.2.2.2:1080"),
        pl.parse_link("http://2.2.2.2:3128"),
    ],
)
def test_apply_route_switches_the_same_client_object(proxy):
    client = _Client()
    connection, argument = route.connection_for(proxy)
    route.apply_route(client, route.Route("x", connection, argument, proxy))
    assert client._connection is connection and client._proxy == argument
    if issubclass(connection, TcpMTProxy):
        assert (client._init_request.proxy.address, client._init_request.proxy.port) == (
            "1.1.1.1",
            443,
        )
    else:
        assert client._init_request.proxy is None


def test_direct_first_while_it_works_and_no_proxy_is_touched(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    pool.add([_mt("1.1.1.1")])
    client = _Client()
    assert asyncio.run(route.connect(client, "main", 30)) == "direct"
    assert [_host_of_attempt(a) for a in client.attempts] == ["direct"]
    assert pool.route("main") == "direct"


def _host_of_attempt(attempt):
    _, proxy = attempt
    if proxy is None:
        return "direct"
    return proxy["addr"] if isinstance(proxy, dict) else proxy[0]


def test_a_dead_direct_route_fails_over_to_the_first_working_proxy(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    dead, alive = _mt("1.0.0.1"), _mt("1.0.0.2")
    pool.add([dead, alive])
    client = _Client(works=lambda c: _host_of(c) == "1.0.0.2")
    assert asyncio.run(route.connect(client, "main", 30)) == alive.fingerprint
    assert [_host_of_attempt(a) for a in client.attempts] == ["direct", "1.0.0.1", "1.0.0.2"]
    rows = {row["host"]: row for row in pool.describe()}
    assert rows["1.0.0.1"]["healthy"] is False and rows["1.0.0.2"]["healthy"] is True
    assert pool.route("main") == alive.fingerprint
    assert client._sender._retries == 5, "the client's own retry count is restored"


def test_a_failing_env_proxy_falls_through_to_direct(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: (ConnectionTcpFull, {"addr": "env"}))
    client = _Client(works=lambda c: _host_of(c) == "direct")
    assert asyncio.run(route.connect(client, "main", 30)) == "direct"


def test_when_nothing_works_the_failure_names_every_route_and_backs_off(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    proxy = _mt("1.0.0.1")
    pool.add([proxy])
    now = [1000.0]
    monkeypatch.setattr(route, "_clock", lambda: now[0])
    client = _Client(works=lambda c: False)
    with pytest.raises(route.NoRoute) as first:
        asyncio.run(route.connect(client, "main", 30))
    assert "direct" in str(first.value) and proxy.fingerprint in str(first.value)
    attempts = len(client.attempts)

    now[0] += 10  # within the 30 s backoff: the same answer, no connection opened
    with pytest.raises(route.NoRoute) as again:
        asyncio.run(route.connect(client, "main", 30))
    assert str(again.value) == str(first.value) and len(client.attempts) == attempts

    now[0] += 25  # past it: tried again
    with pytest.raises(route.NoRoute):
        asyncio.run(route.connect(client, "main", 30))
    assert len(client.attempts) > attempts


def test_the_deadline_bounds_the_whole_failover(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    pool.add([_mt("1.0.0.1"), _mt("1.0.0.2")])
    client = _Client(hang=True)
    started = asyncio.run(_timed(route.connect(client, "main", 0.3)))
    assert started < 2.0


async def _timed(coroutine):
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(route.NoRoute):
        await coroutine
    return loop.time() - start


def test_a_burned_session_is_never_failed_over(monkeypatch):
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    pool.add([_mt("1.0.0.1")])

    class _Burned(_Client):
        async def connect(self):
            self.attempts.append((self._connection, self._proxy))
            raise AuthKeyDuplicatedError(request=None)

    client = _Burned()
    with pytest.raises(AuthKeyDuplicatedError):
        asyncio.run(route.connect(client, "main", 30))
    assert len(client.attempts) == 1


# --- a restart starts on the route that last worked -----------------------------------------


def _built(monkeypatch):
    from telethon.sessions import StringSession

    from telegram_mcp import session_files

    seen = {}

    class _Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(session_files, "TelegramClient", _Client)
    session_files._build_client(StringSession(), "main")
    return seen


def test_a_new_client_starts_on_the_proxy_that_last_worked(monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROXY_TYPE", raising=False)
    monkeypatch.delenv("TELEGRAM_PROXY_TYPE_MAIN", raising=False)
    proxy = _mt("1.0.0.9")
    pool.add([proxy])
    pool.set_route("main", proxy.fingerprint)
    seen = _built(monkeypatch)
    assert seen["connection"] is ConnectionTcpMTProxyRandomizedIntermediate
    assert seen["proxy"] == ("1.0.0.9", 443, PLAIN)


@pytest.mark.parametrize("recorded", [None, "direct", "env", "0123456789ab"])
def test_otherwise_a_new_client_starts_as_before(monkeypatch, recorded):
    monkeypatch.delenv("TELEGRAM_PROXY_TYPE", raising=False)
    monkeypatch.delenv("TELEGRAM_PROXY_TYPE_MAIN", raising=False)
    if recorded:
        pool.set_route("main", recorded)
    seen = _built(monkeypatch)
    assert "proxy" not in seen and "connection" not in seen


def test_an_env_proxy_always_wins_at_start(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROXY_TYPE", "mtproxy")
    monkeypatch.setenv("TELEGRAM_PROXY_HOST", "7.7.7.7")
    monkeypatch.setenv("TELEGRAM_PROXY_PORT", "443")
    monkeypatch.setenv("TELEGRAM_PROXY_SECRET", PLAIN)
    proxy = _mt("1.0.0.9")
    pool.add([proxy])
    pool.set_route("main", proxy.fingerprint)
    assert _built(monkeypatch)["proxy"] == ("7.7.7.7", 443, PLAIN)


def test_probing_many_stops_at_its_total_bound_and_marks_the_rest_not_tested():
    made = []

    async def slow():
        await asyncio.sleep(5)

    proxies = [_mt(f"10.0.1.{n}") for n in range(20)]
    pool.add(proxies)
    results = asyncio.run(
        route.probe_many(proxies, timeout=10, total=0.1, factory=_factory(lambda a: slow, made))
    )
    assert set(results) == {p.fingerprint for p in proxies}
    assert {outcome for outcome, _ in results.values()} == {"not_tested"}
    assert all(row["last_result"] is None for row in pool.describe()), "nothing stored"
    assert all(client.disconnected for *_, client in made)


def test_telethons_retry_count_message_becomes_a_plain_reason():
    made = []

    async def telethon_gave_up():
        raise ConnectionError("Connection to Telegram failed 0 time(s)")

    result = asyncio.run(
        route.probe(_mt("1.1.1.1"), factory=_factory(lambda a: telethon_gave_up, made))
    )
    assert result == ("failed:could not connect through it", None)
