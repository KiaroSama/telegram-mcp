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


# --- installing and removing a whole pack ------------------------------------
#
# A different axis from the tools above: those edit a set's CONTENTS for everyone
# who has it, these attach or detach the pack from this one account.


@pytest.mark.asyncio
async def test_installing_by_short_name_sends_the_short_name_reference(_wire):
    client = _wire(
        _Client(
            {
                "InstallStickerSetRequest": SimpleNamespace(sets=[]),
                "GetStickerSetRequest": _set_result(short_name="Pack"),
            }
        )
    )

    await mod.install_sticker_set(short_name="Pack", account="a")

    sent = client.sent("InstallStickerSetRequest")
    assert type(sent.stickerset).__name__ == "InputStickerSetShortName"
    assert sent.stickerset.short_name == "Pack"
    assert sent.archived is False


@pytest.mark.asyncio
async def test_an_emoji_pack_installs_through_the_same_call_and_is_named_as_one(_wire):
    """Telegram has no separate "install emoji pack" method - an emoji pack IS a
    sticker set with `emojis` set. A caller who meant one and got the other can
    only tell from the answer, so the answer says which arrived."""
    emoji_set = _set_result(short_name="Faces")
    emoji_set.set.emojis = True
    _wire(
        _Client(
            {
                "InstallStickerSetRequest": SimpleNamespace(sets=[]),
                "GetStickerSetRequest": emoji_set,
            }
        )
    )

    record = json.loads(await mod.install_sticker_set(short_name="Faces", account="a"))["results"][
        0
    ]

    assert record["kind"] == "emoji"
    assert record["installed"] is True


@pytest.mark.asyncio
async def test_packs_telegram_archived_to_make_room_are_named(_wire):
    """`stickerSetInstallResultArchive` is not a failure: the account was at its
    pack limit, so Telegram archived older packs and listed them. Reporting it as
    plain success hides packs vanishing from the picker."""
    displaced = SimpleNamespace(set=SimpleNamespace(short_name="Old", title="An Old Pack"))
    _wire(
        _Client(
            {
                "InstallStickerSetRequest": SimpleNamespace(sets=[displaced]),
                "GetStickerSetRequest": _set_result(short_name="Pack"),
            }
        )
    )

    answer = await mod.install_sticker_set(short_name="Pack", account="a")
    record = json.loads(answer)["results"][0]

    assert record["displaced_to_archive"] == [{"short_name": "Old", "title": "An Old Pack"}]
    assert "pack limit" in answer


@pytest.mark.asyncio
async def test_an_id_without_its_access_hash_is_refused_before_any_request(_wire):
    """Half a reference cannot address a set, and InputStickerSetID would raise
    on the None rather than say which half was missing."""
    client = _wire(_Client())

    answer = await mod.install_sticker_set(set_id=123, account="a")

    assert "access_hash" in answer
    assert client.requests == []


@pytest.mark.asyncio
async def test_nothing_identifying_the_pack_is_refused(_wire):
    client = _wire(_Client())

    answer = await mod.uninstall_sticker_set(account="a")

    assert "Nothing identifies the pack" in answer
    assert client.requests == []


@pytest.mark.asyncio
async def test_uninstalling_reads_the_pack_first_so_it_can_be_put_back(_wire):
    """A removal that does not name what went leaves the caller unable to undo it:
    install_sticker_set needs the short name, and after the uninstall the set is
    no longer in this account's list to look up."""
    client = _wire(
        _Client(
            {
                "GetStickerSetRequest": _set_result(short_name="Pack"),
                "UninstallStickerSetRequest": True,
            }
        )
    )

    answer = await mod.uninstall_sticker_set(short_name="Pack", account="a")

    assert client.names() == ["GetStickerSetRequest", "UninstallStickerSetRequest"], (
        "the read must come BEFORE the removal: " f"{client.names()}"
    )
    assert "install_sticker_set(short_name='Pack')" in answer
    assert json.loads(answer)["results"][0]["uninstalled"] is True


@pytest.mark.asyncio
async def test_a_pack_that_cannot_be_described_is_still_uninstalled(_wire):
    """Losing the description must not lose the removal. The read is there to
    make the answer useful, not to gate the write."""

    def _refuse(request):
        raise ValueError("Telegram will not describe this set")

    client = _wire(_Client({"GetStickerSetRequest": _refuse, "UninstallStickerSetRequest": True}))

    answer = await mod.uninstall_sticker_set(short_name="Pack", account="a")

    assert client.sent("UninstallStickerSetRequest") is not None, "the removal was skipped"
    assert json.loads(answer)["results"][0]["uninstalled"] is True


@pytest.mark.asyncio
async def test_a_short_name_that_is_not_one_says_where_a_short_name_comes_from(_wire):
    """The commonest mistake with these two tools is passing a title or a t.me URL
    instead of the short name. Telethon raises StickersetInvalidError for all of
    them, and the generic handler turned that into "An error occurred (code:
    GEN-ERR-413)" - measured live against a name that did not exist, which says
    nothing about the name being the problem."""
    from telethon import errors

    def _invalid(request):
        raise errors.StickersetInvalidError(request=None)

    _wire(_Client({"InstallStickerSetRequest": _invalid, "GetStickerSetRequest": _invalid}))

    answer = await mod.install_sticker_set(short_name="Not A Real Pack", account="a")

    assert "GEN-ERR" not in answer, "still an opaque code"
    assert "'Not A Real Pack'" in answer, "the name it refused is not quoted back"
    assert "t.me/addstickers" in answer, "it does not say where a short name comes from"


@pytest.mark.asyncio
async def test_an_unrecognised_id_blames_the_pairing_rather_than_the_name(_wire):
    """An access_hash is bound to the set AND to the account, so one carried over
    from somewhere else fails identically to a wrong id. Quoting a short name that
    was never passed would send the reader looking for the wrong mistake."""
    from telethon import errors

    def _invalid(request):
        raise errors.StickersetInvalidError(request=None)

    _wire(_Client({"UninstallStickerSetRequest": _invalid, "GetStickerSetRequest": _invalid}))

    answer = await mod.uninstall_sticker_set(set_id=123, access_hash=456, account="a")

    assert "access_hash" in answer
    assert "123" in answer
    assert "short name" not in answer, "it blames a name the caller never gave"
