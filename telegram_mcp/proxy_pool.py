"""The proxy pool on disk: a shared set, per-account sets, health and each account's route.

``state_dir()/proxy-pool.json``, owner-only and written atomically, because a proxy's
secret is often paid for (FR-013). One file, three keys::

    {"shared": [<entry>...], "accounts": {"<label>": [<entry>...]}, "routes": {"<label>": ...}}

A route is ``"direct"``, ``"env"`` or a proxy's fingerprint: the last route that worked
for the account, so a restart starts there. A file that cannot be read is moved aside
and the pool starts empty; it is never written over.

``describe()`` is what may be shown: no secret, no user name, no password (FR-012).
"""

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram_mcp.proxy_links import Proxy

__all__ = [
    "add",
    "candidates",
    "describe",
    "get",
    "pool_path",
    "record",
    "remove",
    "reset_cache",
    "route",
    "routes",
    "set_route",
]

SHARED = "shared"
_PROXY_FIELDS = ("kind", "host", "port", "secret", "variant", "username", "password")
_lock = threading.RLock()
_cache: Dict[str, Any] = {}


def pool_path() -> Path:
    from telegram_mcp.settings import state_dir

    return state_dir() / "proxy-pool.json"


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _empty() -> Dict[str, Any]:
    return {"shared": [], "accounts": {}, "routes": {}}


def _data() -> Dict[str, Any]:
    if "data" not in _cache:
        path = pool_path()
        data, unreadable = _empty(), False
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("not an object")
                data = {
                    "shared": list(loaded.get("shared", [])),
                    "accounts": dict(loaded.get("accounts", {})),
                    "routes": dict(loaded.get("routes", {})),
                }
            except (OSError, ValueError, TypeError, AttributeError):
                data, unreadable = _empty(), True
        _cache["data"], _cache["unreadable"] = data, unreadable
    return _cache["data"]


def _save() -> None:
    from telegram_mcp.safeguard.state_files import write_private_json

    path = pool_path()
    if _cache.get("unreadable") and path.exists():
        path.replace(path.with_name(f"{path.stem}.corrupt-{int(time.time() * 1000)}"))
    write_private_json(path, _cache["data"])
    _cache["unreadable"] = False


def _entries(name: str) -> List[Dict[str, Any]]:
    data = _data()
    if name == SHARED:
        return data["shared"]
    return data["accounts"].setdefault(name.lower(), [])


def _pools() -> Iterable[Tuple[str, List[Dict[str, Any]]]]:
    data = _data()
    yield SHARED, data["shared"]
    for label, entries in data["accounts"].items():
        yield label, entries


def _proxy(entry: Dict[str, Any]) -> Proxy:
    return Proxy(**{key: entry.get(key) for key in _PROXY_FIELDS})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(
    proxies: Iterable[Proxy], pool: str = SHARED, source: str = "paste"
) -> Tuple[List[Proxy], int]:
    """Add what is new to ``pool``; a proxy already there only has its source refreshed."""
    with _lock:
        entries = _entries(pool)
        by_id = {entry["id"]: entry for entry in entries}
        added: List[Proxy] = []
        duplicates = 0
        for proxy in proxies:
            existing = by_id.get(proxy.fingerprint)
            if existing is not None:
                existing["source"] = source
                duplicates += 1
                continue
            entry = {key: value for key, value in asdict(proxy).items() if value is not None}
            entry.update(
                id=proxy.fingerprint,
                source=source,
                last_tested=None,
                last_result=None,
                latency_ms=None,
                healthy=None,
            )
            entries.append(entry)
            by_id[proxy.fingerprint] = entry
            added.append(proxy)
        if added or duplicates:
            _save()
        return added, duplicates


def _find(fingerprint: str) -> List[Dict[str, Any]]:
    return [entry for _, entries in _pools() for entry in entries if entry["id"] == fingerprint]


def get(fingerprint: str) -> Optional[Proxy]:
    with _lock:
        found = _find(fingerprint)
        return _proxy(found[0]) if found else None


def record(fingerprint: str, result: str, latency_ms: Optional[int] = None) -> None:
    """A test or a real connect: ``reachable``, ``failed:<reason>`` or ``timed_out``."""
    with _lock:
        found = _find(fingerprint)
        for entry in found:
            entry["last_tested"] = _now()
            entry["last_result"] = result
            entry["healthy"] = result == "reachable"
            entry["latency_ms"] = int(latency_ms) if result == "reachable" and latency_ms else None
        if found:
            _save()


def _order(entry: Dict[str, Any]) -> Tuple[int, float]:
    if entry.get("healthy") is True:
        return 0, float(entry.get("latency_ms") or 0)
    if entry.get("healthy") is None:
        return 1, 0.0
    return 2, 0.0


def candidates(label: Optional[str]) -> List[Proxy]:
    """The account's own pool when it has one, else the shared pool, best first."""
    with _lock:
        own = _data()["accounts"].get((label or "").lower()) or []
        entries = own or _data()["shared"]
        return [_proxy(entry) for entry in sorted(entries, key=_order)]


def remove(
    ids: Optional[Iterable[str]] = None, unhealthy: bool = False, pool: Optional[str] = None
) -> List[str]:
    wanted = set(ids or ())
    with _lock:
        removed: List[str] = []
        for name, entries in list(_pools()):
            if pool is not None and name != pool.lower() and name != pool:
                continue
            keep = []
            for entry in entries:
                if entry["id"] in wanted or (unhealthy and entry.get("healthy") is False):
                    removed.append(entry["id"])
                else:
                    keep.append(entry)
            entries[:] = keep
        data = _data()
        data["accounts"] = {label: items for label, items in data["accounts"].items() if items}
        if removed:
            _save()
        return removed


def set_route(label: str, value: str) -> None:
    with _lock:
        routes = _data()["routes"]
        if routes.get(label.lower()) != value:
            routes[label.lower()] = value
            _save()


def route(label: str) -> Optional[str]:
    with _lock:
        return _data()["routes"].get(label.lower())


def routes() -> Dict[str, str]:
    with _lock:
        return dict(_data()["routes"])


def describe(pool: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every proxy as it may be shown: never a secret, a user name or a password."""
    with _lock:
        used: Dict[str, List[str]] = {}
        for label, value in _data()["routes"].items():
            used.setdefault(value, []).append(label)
        rows = []
        for name, entries in _pools():
            if pool is not None and name != pool.lower() and name != pool:
                continue
            for entry in entries:
                row = _proxy(entry).describe()
                row.update(
                    pool=name,
                    source=entry.get("source"),
                    last_tested=entry.get("last_tested"),
                    last_result=entry.get("last_result"),
                    latency_ms=entry.get("latency_ms"),
                    healthy=entry.get("healthy"),
                    used_by=sorted(used.get(entry["id"], [])),
                )
                rows.append(row)
        return rows
