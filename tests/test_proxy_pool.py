"""The proxy pool: one shared set, optional per-account sets, their health and routes.

Stored owner-only and atomically in the state directory, because a proxy's secret is
often paid for. A file that cannot be read is moved aside, never overwritten. Nothing
meant to be shown carries a secret or a password.
"""

import json

import pytest

from telegram_mcp import proxy_links as pl
from telegram_mcp import proxy_pool as pool

PLAIN = "0123456789abcdef0123456789abcdef"


def _mt(host, port=443):
    return pl.parse_link(f"tg://proxy?server={host}&port={port}&secret={PLAIN}")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy-pool.json"
    monkeypatch.setattr(pool, "pool_path", lambda: path)
    pool.reset_cache()
    yield path
    pool.reset_cache()


def test_adding_dedupes_by_fingerprint_and_counts_duplicates():
    added, duplicates = pool.add([_mt("1.1.1.1"), _mt("2.2.2.2")], source="paste")
    assert [p.host for p in added] == ["1.1.1.1", "2.2.2.2"] and duplicates == 0
    added, duplicates = pool.add([_mt("1.1.1.1"), _mt("3.3.3.3")], source="channel:-100")
    assert [p.host for p in added] == ["3.3.3.3"] and duplicates == 1


def test_an_account_pool_is_separate_from_the_shared_one():
    pool.add([_mt("1.1.1.1")])
    pool.add([_mt("9.9.9.9")], pool="work")
    assert [p.host for p in pool.candidates("main")] == ["1.1.1.1"]
    assert [p.host for p in pool.candidates("work")] == ["9.9.9.9"]


def test_an_emptied_account_pool_falls_back_to_the_shared_one():
    pool.add([_mt("1.1.1.1")])
    (added,), _ = pool.add([_mt("9.9.9.9")], pool="work")
    pool.remove(ids=[added.fingerprint], pool="work")
    assert [p.host for p in pool.candidates("work")] == ["1.1.1.1"]


def test_health_is_recorded_and_orders_the_candidates():
    fast, slow, untested, dead = (_mt(h) for h in ("1.0.0.1", "1.0.0.2", "1.0.0.3", "1.0.0.4"))
    pool.add([dead, untested, slow, fast])
    pool.record(slow.fingerprint, "reachable", latency_ms=900)
    pool.record(fast.fingerprint, "reachable", latency_ms=120)
    pool.record(dead.fingerprint, "failed:connection refused")
    assert [p.host for p in pool.candidates("main")] == [
        "1.0.0.1",
        "1.0.0.2",
        "1.0.0.3",
        "1.0.0.4",
    ]
    listed = {row["host"]: row for row in pool.describe()}
    assert listed["1.0.0.1"]["healthy"] is True and listed["1.0.0.1"]["latency_ms"] == 120
    assert listed["1.0.0.4"]["healthy"] is False and listed["1.0.0.4"]["latency_ms"] is None
    assert listed["1.0.0.3"]["last_result"] is None


def test_a_timed_out_test_is_not_healthy_and_has_no_latency():
    proxy = _mt("1.1.1.1")
    pool.add([proxy])
    pool.record(proxy.fingerprint, "timed_out", latency_ms=10_000)
    row = pool.describe()[0]
    assert (row["healthy"], row["latency_ms"], row["last_result"]) == (False, None, "timed_out")


def test_removing_by_id_and_every_unhealthy_one():
    a, b, c = _mt("1.1.1.1"), _mt("2.2.2.2"), _mt("3.3.3.3")
    pool.add([a, b, c])
    pool.record(b.fingerprint, "failed:refused")
    assert pool.remove(unhealthy=True) == [b.fingerprint]
    assert pool.remove(ids=[a.fingerprint]) == [a.fingerprint]
    assert [row["host"] for row in pool.describe()] == ["3.3.3.3"]


def test_routes_are_remembered_and_listed_as_users():
    proxy = _mt("1.1.1.1")
    pool.add([proxy])
    pool.set_route("main", proxy.fingerprint)
    pool.set_route("work", "direct")
    pool.reset_cache()
    assert pool.route("main") == proxy.fingerprint and pool.route("work") == "direct"
    assert pool.describe()[0]["used_by"] == ["main"]


def test_the_pool_survives_a_restart_and_is_written_whole(store):
    pool.add([_mt("1.1.1.1")], source="paste")
    pool.reset_cache()
    assert [p.host for p in pool.candidates("main")] == ["1.1.1.1"]
    data = json.loads(store.read_text(encoding="utf-8"))
    assert set(data) == {"shared", "accounts", "routes"}
    assert not list(store.parent.glob("*.tmp"))


def test_an_unreadable_file_is_moved_aside_not_overwritten(store):
    store.write_text("{not json", encoding="utf-8")
    pool.reset_cache()
    assert pool.describe() == []
    pool.add([_mt("1.1.1.1")])
    aside = list(store.parent.glob("proxy-pool.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{not json"


def test_nothing_shown_carries_a_secret_or_a_password():
    pool.add([_mt("1.1.1.1"), pl.parse_link("tg://socks?server=2.2.2.2&port=1080&pass=hunter2")])
    shown = json.dumps(pool.describe())
    assert PLAIN not in shown and "hunter2" not in shown


def test_a_stored_proxy_comes_back_whole_for_connecting():
    original = pl.parse_link("tg://socks?server=2.2.2.2&port=1080&user=u&pass=hunter2")
    pool.add([original])
    pool.reset_cache()
    assert pool.get(original.fingerprint) == original
