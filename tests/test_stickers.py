"""Sticker-set writes, where the interesting behaviour is what they REFUSE.

`AddStickerToSet` is not idempotent, so the tests that matter are the ones proving
a doubtful add never reaches Telegram.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import stickers as mod
from telegram_mcp.tools.stickers import (
    STICKERS_PER_SET,
    add_sticker_to_set,
    inspect_sticker_set,
    move_sticker_in_set,
    remove_sticker_from_set,
)


def _set_result(count=3, short_name="Pack", documents=None):
    return SimpleNamespace(
        set=SimpleNamespace(
            short_name=short_name,
            title="A Pack",
            count=count,
            animated=False,
            videos=False,
            emojis=False,
            masks=False,
        ),
        documents=documents if documents is not None else [],
    )


class _Client:
    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or {}

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in self.answers:
            answer = self.answers[name]
            return answer(request) if callable(answer) else answer
        return _set_result()

    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        return client

    return wire


def test_the_platform_cap_is_telegrams_documented_one():
    assert STICKERS_PER_SET == 200


@pytest.mark.asyncio
async def test_inspecting_a_set_reports_its_remaining_slots(_wire):
    documents = [
        SimpleNamespace(
            id=1,
            mime_type="application/x-tgsticker",
            size=5000,
            attributes=[SimpleNamespace(alt="😂")],
        )
    ]
    _wire(_Client({"GetStickerSetRequest": _set_result(count=7, documents=documents)}))

    payload = json.loads(await inspect_sticker_set("Pack", account="a"))
    record = payload["results"][0]

    assert record["declared_count"] == 7
    assert record["slots_remaining"] == STICKERS_PER_SET - 7
    assert "full" not in record
    assert record["stickers"][0]["emoji"] == "😂"


@pytest.mark.asyncio
async def test_a_leading_at_sign_is_stripped_from_the_short_name(_wire):
    client = _wire(_Client({"GetStickerSetRequest": _set_result()}))

    await inspect_sticker_set("@Pack", account="a")

    assert client.sent("GetStickerSetRequest").stickerset.short_name == "Pack"


@pytest.mark.asyncio
async def test_a_changed_count_refuses_the_add_before_it_is_sent(_wire):
    """A set that moved underneath you is what a silently-applied retry looks like."""
    client = _wire(_Client({"GetStickerSetRequest": _set_result(count=9)}))

    result = await add_sticker_to_set("Pack", 1, 2, "😀", expected_count=8, account="a")

    assert "not the 8 you expected" in result
    assert "AddStickerToSetRequest" not in client.names(), "a doubtful add was sent anyway"


@pytest.mark.asyncio
async def test_a_matching_count_lets_the_add_through(_wire):
    client = _wire(
        _Client(
            {
                "GetStickerSetRequest": _set_result(count=8),
                "AddStickerToSetRequest": _set_result(count=9),
            }
        )
    )

    payload = json.loads(
        await add_sticker_to_set("Pack", 1, 2, "😀", expected_count=8, account="a")
    )
    record = payload["results"][0]

    assert "AddStickerToSetRequest" in client.names()
    assert (record["count_before"], record["count_after"]) == (8, 9)
    assert record["added"] is True
    assert "not idempotent" in payload["note"]


@pytest.mark.asyncio
async def test_a_full_set_is_refused_without_an_upload(_wire):
    client = _wire(_Client({"GetStickerSetRequest": _set_result(count=STICKERS_PER_SET)}))

    result = await add_sticker_to_set("Pack", 1, 2, "😀", account="a")

    assert f"limit of {STICKERS_PER_SET}" in result
    assert "AddStickerToSetRequest" not in client.names()


@pytest.mark.asyncio
async def test_an_add_that_did_not_change_the_count_says_so(_wire):
    """The count is the only evidence a write landed; an unchanged one is reported."""
    _wire(
        _Client(
            {
                "GetStickerSetRequest": _set_result(count=8),
                "AddStickerToSetRequest": _set_result(count=8),
            }
        )
    )

    payload = json.loads(await add_sticker_to_set("Pack", 1, 2, "😀", account="a"))

    assert payload["results"][0]["added"] is False


@pytest.mark.asyncio
async def test_removing_reports_the_set_it_left_behind(_wire):
    _wire(_Client({"RemoveStickerFromSetRequest": _set_result(count=2)}))

    payload = json.loads(await remove_sticker_from_set(1, 2, account="a"))

    assert payload["results"][0]["removed_document_id"] == 1
    assert payload["results"][0]["declared_count"] == 2
    assert "not idempotent" in payload["note"]


@pytest.mark.asyncio
async def test_a_negative_position_is_refused_locally(_wire):
    client = _wire(_Client())

    result = await move_sticker_in_set(1, 2, -1, account="a")

    assert "zero or greater" in result
    assert client.requests == []


@pytest.mark.asyncio
async def test_moving_sends_the_position_and_is_marked_idempotent(_wire):
    client = _wire(_Client({"ChangeStickerPositionRequest": _set_result()}))

    payload = json.loads(await move_sticker_in_set(1, 2, 4, account="a"))

    assert client.sent("ChangeStickerPositionRequest").position == 4
    assert payload["results"][0]["position"] == 4
