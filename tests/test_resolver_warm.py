"""One dialog download per burst of misses, not one per miss.

`_resolve_with_retries` warmed Telethon's entity cache with a bare
`get_dialogs()` on every `ValueError`, with no memory that it had just done so.
`get_dialogs()` downloads the whole dialog list and is the most expensive call
Telethon makes on a large account — and several tools resolve peers in a loop.
`get_folder` resolves every include, exclude and pinned peer one at a time and
swallows each failure into `{"name": "Unknown"}`, so a folder holding cold peers
paid one full download per peer and still rendered "Unknown" for each.

The guard has to be a TTL rather than a flag: a peer that appears after the warm
must still become resolvable, so "warmed once" would trade an N+1 for a chat the
server can never see.
"""

import pytest

from telegram_mcp import runtime as runtime_mod


class ColdClient:
    """Resolves nothing, and counts how often it was asked to download."""

    def __init__(self, resolvable=None):
        self.dialog_downloads = 0
        self.resolvable = set(resolvable or ())

    async def get_dialogs(self):
        self.dialog_downloads += 1
        return []

    async def get_entity(self, identifier):
        if identifier in self.resolvable:
            return f"entity:{identifier}"
        raise ValueError(f"cold: {identifier!r}")


@pytest.fixture(autouse=True)
def _clean_warm_state(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_dialog_warmed", type(runtime_mod._dialog_warmed)())

    async def _already_connected(client):
        return None

    monkeypatch.setattr(runtime_mod, "ensure_connected", _already_connected)
    yield


async def _resolve(client, identifier):
    return await runtime_mod._resolve_with_retries(
        "get_entity", identifier, client, "entity", try_marked=False
    )


@pytest.mark.asyncio
async def test_three_misses_in_a_burst_share_one_download():
    client = ColdClient()

    for identifier in ("@alpha", "@beta", "@gamma"):
        with pytest.raises(ValueError):
            await _resolve(client, identifier)

    assert (
        client.dialog_downloads == 1
    ), f"three unresolvable lookups cost {client.dialog_downloads} full dialog downloads"


@pytest.mark.asyncio
async def test_a_peer_that_appears_after_the_warm_still_resolves(monkeypatch):
    """The reason this is a TTL and not a flag. Without expiry, a chat created
    after the first warm would be permanently unresolvable."""
    client = ColdClient()

    with pytest.raises(ValueError):
        await _resolve(client, "@newchat")
    assert client.dialog_downloads == 1

    # The chat now exists, and the TTL has passed. Drive the clock rather than
    # sleeping: a test that waits 30 real seconds is a test nobody runs.
    client.resolvable.add("@newchat")
    base = runtime_mod.time.monotonic()
    monkeypatch.setattr(
        runtime_mod.time,
        "monotonic",
        lambda: base + runtime_mod._DIALOG_WARM_SECONDS + 1,
    )

    # It now resolves on the first attempt, so no warm is needed for it at all.
    assert await _resolve(client, "@newchat") == "entity:@newchat"
    assert client.dialog_downloads == 1

    # And a peer that is STILL cold gets a fresh warm, because the TTL expired.
    with pytest.raises(ValueError):
        await _resolve(client, "@stillcold")
    assert client.dialog_downloads == 2, "the expired warm was not repeated"


@pytest.mark.asyncio
async def test_a_resolvable_peer_costs_no_download_at_all():
    """Guard the guard: the warm must still not run on the happy path."""
    client = ColdClient(resolvable={"@known"})

    assert await _resolve(client, "@known") == "entity:@known"
    assert client.dialog_downloads == 0


@pytest.mark.asyncio
async def test_two_clients_do_not_share_a_warm():
    """Each account has its own entity cache; one account's warm says nothing
    about another's."""
    first, second = ColdClient(), ColdClient()

    with pytest.raises(ValueError):
        await _resolve(first, "@x")
    with pytest.raises(ValueError):
        await _resolve(second, "@x")

    assert first.dialog_downloads == 1
    assert second.dialog_downloads == 1
