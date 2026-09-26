"""The five proxy tools, answered per contracts/proxy-tools.md.

No answer ever carries a secret, a user name or a password. A source channel is read
only when asked, through plain history reads (no seen signal), and what came from it is
remembered as untrusted content like any other message the model is shown.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from telegram_mcp import proxy_pool as pool
from telegram_mcp import proxy_route as route
from telegram_mcp.tools import proxy_tools as tools

PLAIN = "0123456789abcdef0123456789abcdef"


def _link(host, port=443):
    return f"tg://proxy?server={host}&port={port}&secret={PLAIN}"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "pool_path", lambda: tmp_path / "proxy-pool.json")
    pool.reset_cache()
    route.reset_state()

    async def _probe_many(proxies, **kwargs):
        results = {}
        for proxy in proxies:
            outcome = ("reachable", 4000) if proxy.host.startswith("1.") else ("timed_out", None)
            results[proxy.fingerprint] = outcome
            pool.record(proxy.fingerprint, *outcome)
        return results

    monkeypatch.setattr(route, "probe_many", _probe_many)
    yield
    pool.reset_cache()


def _run(coroutine):
    return json.loads(asyncio.run(coroutine))


def test_adding_pasted_text_reports_added_duplicates_and_invalid():
    text = f"{_link('1.1.1.1')}\n{_link('1.1.1.1')}\n{_link('2.2.2.2')}\n{_link('3.3.3.3', 0)}"
    answer = _run(tools.add_proxies(text=text))
    assert [(a["host"], a["result"], a["latency_ms"]) for a in answer["added"]] == [
        ("1.1.1.1", "reachable", 4000),
        ("2.2.2.2", "timed_out", None),
    ]
    assert answer["duplicates"] == 1  # the same link twice in one paste
    assert [i["reason"] for i in answer["invalid"]] == ["port out of range"]
    again = _run(tools.add_proxies(text=_link("1.1.1.1")))
    assert (again["added"], again["duplicates"]) == ([], 1)
    assert PLAIN not in json.dumps(answer)


def test_adding_needs_text_or_a_source():
    assert "text" in asyncio.run(tools.add_proxies()) and "source" in asyncio.run(
        tools.add_proxies()
    )


class _Channel:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def iter_messages(self, entity, limit=None):
        self.calls.append(("iter_messages", entity, limit))
        for message in self.messages:
            yield message


def _post(text):
    return SimpleNamespace(message=text, get_entities_text=lambda: [], reply_markup=None)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("https://t.me/ProxyDaemi", "ProxyDaemi"),
        ("t.me/ProxyDaemi", "ProxyDaemi"),
        ("@ProxyDaemi", "ProxyDaemi"),
        ("-1001234567890", -1001234567890),
    ],
)
def test_a_source_is_read_by_its_last_200_posts_without_a_seen_signal(
    monkeypatch, source, expected
):
    channel = _Channel([_post("ad"), _post(_link("1.1.1.1")), _post(_link("1.2.2.2"))])
    resolved, noted = [], []

    async def _resolve(identifier, cl):
        resolved.append(identifier)
        return "entity"

    monkeypatch.setattr(tools, "get_client", lambda account=None: channel)
    monkeypatch.setattr(tools, "resolve_entity", _resolve)
    monkeypatch.setattr(tools, "note_rendered", lambda msg, account=None: noted.append(msg))
    answer = _run(tools.add_proxies(source=source))
    assert resolved == [expected]
    assert channel.calls == [("iter_messages", "entity", 200)]
    assert sorted(a["host"] for a in answer["added"]) == ["1.1.1.1", "1.2.2.2"]
    assert len(noted) == 2, "posts that supplied proxies are remembered as untrusted"
    assert {row["source"] for row in pool.describe()} == {"channel:" + str(expected)}


def test_an_unreadable_source_leaves_the_pool_unchanged(monkeypatch):
    async def _resolve(identifier, cl):
        raise ValueError("No user has that username")

    monkeypatch.setattr(tools, "get_client", lambda account=None: _Channel([]))
    monkeypatch.setattr(tools, "resolve_entity", _resolve)
    answer = asyncio.run(tools.add_proxies(source="@nobody_here"))
    assert "could not be read" in answer and pool.describe() == []


def test_an_account_pool_is_filled_when_named():
    _run(tools.add_proxies(text=_link("1.1.1.1"), pool="work"))
    assert [row["pool"] for row in pool.describe()] == ["work"]


def test_listing_shows_health_and_users_and_no_secret():
    _run(tools.add_proxies(text=f"{_link('1.1.1.1')}\n{_link('2.2.2.2')}"))
    first = pool.describe()[0]["id"]
    pool.set_route("main", first)
    listing = _run(tools.list_proxies())
    rows = {row["host"]: row for row in listing["proxies"]}
    assert rows["1.1.1.1"]["healthy"] is True and rows["1.1.1.1"]["used_by"] == ["main"]
    assert rows["2.2.2.2"]["healthy"] is False
    assert PLAIN not in json.dumps(listing)


def test_testing_again_stores_fresh_results(monkeypatch):
    _run(tools.add_proxies(text=_link("1.1.1.1")))
    answer = _run(tools.test_proxies())
    assert [(r["host"], r["result"]) for r in answer["results"]] == [("1.1.1.1", "reachable")]


def test_removing_by_id_and_all_unhealthy():
    _run(tools.add_proxies(text=f"{_link('1.1.1.1')}\n{_link('2.2.2.2')}\n{_link('1.3.3.3')}"))
    unhealthy = _run(tools.remove_proxies(unhealthy=True))
    assert len(unhealthy["removed"]) == 1
    first = pool.describe()[0]["id"]
    assert _run(tools.remove_proxies(ids=[first]))["removed"] == [first]
    assert "ids" in asyncio.run(tools.remove_proxies())


def test_the_route_of_each_account_is_reported(monkeypatch):
    from telegram_mcp import connection

    monkeypatch.setattr(connection, "clients", {"main": object(), "work": object()})
    monkeypatch.setattr(route, "_env_route", lambda label: None)
    _run(tools.add_proxies(text=_link("2.2.2.2")))
    pool.set_route("work", "direct")
    answer = _run(tools.get_connection_route())
    main = answer["accounts"]["main"]
    assert main["current"] == "direct" and main["order"][0] == "direct"
    assert main["last_failures"] == {main["order"][1]: "timed_out"}
    only = _run(tools.get_connection_route(account="work"))
    assert list(only["accounts"]) == ["work"]
