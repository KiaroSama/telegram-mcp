"""get_chats must not download the whole dialog list to render one page.

No network: the client is a fake that records the limit it was asked for. Telethon
fetches dialogs 100 at a time, so an unbounded get_dialogs() costs one round trip
per hundred chats on the account to produce twenty rows.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import chats as mod


class _DialogClient:
    """Records every get_dialogs limit and returns at most that many dialogs."""

    def __init__(self, total=250):
        self.limits = []
        self.total = total

    async def get_dialogs(self, limit=None, **kwargs):
        self.limits.append(limit)
        count = self.total if limit is None else min(limit, self.total)
        return [
            SimpleNamespace(entity=SimpleNamespace(id=index, title=f"Chat {index}"))
            for index in range(count)
        ]


@pytest.fixture
def _wire(monkeypatch):
    def wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "get_marked_id", lambda e: e.id)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_first_page_does_not_fetch_every_dialog(_wire):
    client = _wire(_DialogClient(total=250))

    payload = json.loads(await mod.get_chats(page=1, page_size=20, account="a"))

    assert client.limits == [20], f"asked Telethon for {client.limits} dialogs, wanted [20]"
    assert [r["title"] for r in payload["results"]] == [f"Chat {i}" for i in range(20)]


@pytest.mark.asyncio
async def test_a_later_page_fetches_only_up_to_its_own_end(_wire):
    """Telethon's dialog cursor is not addressable by offset, so page 3 still has to
    read pages 1-2 — but it must stop at the end of page 3, not at the end of the
    account."""
    client = _wire(_DialogClient(total=250))

    payload = json.loads(await mod.get_chats(page=3, page_size=20, account="a"))

    assert client.limits == [60], f"asked Telethon for {client.limits} dialogs, wanted [60]"
    assert [r["title"] for r in payload["results"]] == [f"Chat {i}" for i in range(40, 60)]


@pytest.mark.asyncio
async def test_a_page_past_the_end_says_so(_wire):
    _wire(_DialogClient(total=10))

    assert "Page out of range." == await mod.get_chats(page=5, page_size=20, account="a")


@pytest.mark.asyncio
@pytest.mark.parametrize("page, page_size", [(0, 20), (-3, 20), (1, 0), (1, -5)])
async def test_a_nonsense_page_is_refused_not_turned_into_a_negative_slice(_wire, page, page_size):
    """Telethon maps limit<=0 to ZERO dialogs, not all of them (requestiter.py:34,
    dialogs.py:41), so the one outcome that must not happen is a silent empty
    result. It used to be clamped to page 1, which is also silent -- the caller
    asked for something impossible and got a plausible answer to a different
    question. Now it is named, and nothing is fetched."""
    client = _wire(_DialogClient(total=250))

    result = await mod.get_chats(page=page, page_size=page_size, account="a")

    assert not result.startswith("{"), result
    assert "Error" in result
    assert ("page_size" if page_size < 1 else "page") in result
    assert client.limits == [], "a request went out for a page that does not exist"
