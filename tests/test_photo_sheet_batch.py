"""The contact sheet fetches its tiles at once, which is what it is for.

`get_photo_sheet` downloaded up to 24 tiles one at a time. There is no
rate-limit or flood-wait reason recorded anywhere in `photos.py` or
`photo_source.py` — the only nearby note says the per-tile cost is small and
"the ceiling that matters is the number of them". So the one tool whose whole
purpose is seeing them all at once was the slowest.

`get_custom_emoji` in `tools/inspection.py` already solved exactly this, with a
`batch_width` semaphore and a `gather`, and measured the serial version at "ten
round trips end to end for work with no ordering between the items".

Tile ORDER still matters for placement in the sheet. Fetch order does not.
"""

import asyncio

import pytest

from telegram_mcp.tools import photos as photos_mod


class Reference:
    def __init__(self, identifier, is_current=False):
        self.identifier = identifier
        self.is_current = is_current


class Tracker:
    """Counts how many downloads are in flight simultaneously."""

    def __init__(self, count, failing=(), delays=None):
        self.references = [Reference(n) for n in range(count)]
        self.in_flight = 0
        self.high_water = 0
        self.failing = set(failing)
        self.delays = delays or {}

    async def download(self, cl, reference, max_bytes, thumbnail=False):
        self.in_flight += 1
        self.high_water = max(self.high_water, self.in_flight)
        try:
            # Yield enough times that a serial implementation cannot look
            # concurrent by accident.
            for _ in range(self.delays.get(reference.identifier, 3)):
                await asyncio.sleep(0)
            if reference.identifier in self.failing:
                raise RuntimeError(f"tile {reference.identifier} is broken")
            return (f"bytes-{reference.identifier}".encode(), False)
        finally:
            self.in_flight -= 1


@pytest.fixture
def wired(monkeypatch):
    tracker = Tracker(count=8)

    async def _resolve(chat_id, cl=None, account=None):
        return object()

    async def _list_references(cl, entity, source, wanted):
        return tracker.references[:wanted]

    def _compose(tiles, columns):
        return ("encoded", {"tiles": [label for _raw, label in tiles]})

    monkeypatch.setattr(photos_mod, "get_client", lambda account=None: object())
    monkeypatch.setattr(photos_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(photos_mod, "list_photo_references", _list_references)
    monkeypatch.setattr(photos_mod, "download_photo_bytes", tracker.download)
    monkeypatch.setattr(photos_mod, "compose_contact_sheet", _compose)
    monkeypatch.setattr(photos_mod, "peer_supports_source", lambda *a, **k: True)
    return tracker


@pytest.mark.asyncio
async def test_the_tiles_are_fetched_concurrently(wired):
    await photos_mod.get_photo_sheet("me", limit=8)

    assert wired.high_water > 1, "the tiles were still downloaded one at a time"


@pytest.mark.asyncio
async def test_concurrency_stays_under_the_byte_budget(wired):
    """Not merely 'concurrent': the width comes from the byte budget, so peak
    memory cannot grow with the batch size."""
    from telegram_mcp.media_transfer import batch_width

    await photos_mod.get_photo_sheet("me", limit=8)

    ceiling = batch_width(len(wired.references), photos_mod.SHEET_TILE_BYTES)
    assert wired.high_water <= ceiling, f"{wired.high_water} in flight, ceiling {ceiling}"


@pytest.mark.asyncio
async def test_tiles_keep_reference_order_however_they_finish(monkeypatch, wired):
    """The sheet is a grid: which tile lands where is the REFERENCE order, not
    the completion order. Here the first two references finish last."""
    wired.delays = {0: 12, 1: 8, 2: 1, 3: 1}
    composed = {}

    def _compose(tiles, columns):
        composed["labels"] = [label for _raw, label in tiles]
        return ("encoded", {})

    monkeypatch.setattr(photos_mod, "compose_contact_sheet", _compose)

    await photos_mod.get_photo_sheet("me", limit=4)

    assert composed["labels"] == ["id=0", "id=1", "id=2", "id=3"], composed
    assert wired.high_water > 1, "and they really did overlap"


@pytest.mark.asyncio
async def test_one_broken_tile_is_skipped_not_fatal(monkeypatch):
    tracker = Tracker(count=4, failing={2})

    async def _resolve(chat_id, cl=None, account=None):
        return object()

    async def _list_references(cl, entity, source, wanted):
        return tracker.references[:wanted]

    composed = {}

    def _compose(tiles, columns):
        composed["labels"] = [label for _raw, label in tiles]
        return ("encoded", {})

    monkeypatch.setattr(photos_mod, "get_client", lambda account=None: object())
    monkeypatch.setattr(photos_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(photos_mod, "list_photo_references", _list_references)
    monkeypatch.setattr(photos_mod, "download_photo_bytes", tracker.download)
    monkeypatch.setattr(photos_mod, "compose_contact_sheet", _compose)
    monkeypatch.setattr(photos_mod, "peer_supports_source", lambda *a, **k: True)

    await photos_mod.get_photo_sheet("me", limit=4)

    assert composed["labels"] == ["id=0", "id=1", "id=3"], composed
