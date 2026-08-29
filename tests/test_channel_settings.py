"""The six reversible channel settings, and the two things easy to get wrong.

`docs/api-coverage.md` measures 42 of 59 `channels.*` requests as unreached, the
largest gap in the project. These six are the reversible half of Phase 1: what a
moderator reaches for on a bad day — close a channel to posting, hide the member
list, hide pre-join history.

Two things these tests exist to pin, because both are invisible from the return
value:

  - `ToggleSignaturesRequest` takes TWO flags in layer 227, `signatures_enabled`
    and `profiles_enabled`. Sending one where two are meant either drops the
    profile link or adds one nobody asked for, and linking a signature to a
    profile discloses more than the name alone.
  - Every one of these is channel-only. A basic group must get a sentence, not a
    raw RPC error naming a type the caller never mentioned.
"""

import pytest
from telethon.tl import functions, types

from telegram_mcp.tools import channel_settings as settings_mod

CHANNEL = types.Channel(
    id=555,
    title="Announcements",
    photo=None,
    date=None,
    creator=True,
    left=False,
    broadcast=True,
    verified=False,
    megagroup=False,
    restricted=False,
    signatures=False,
    min=False,
    scam=False,
    has_link=False,
    has_geo=False,
    slowmode_enabled=False,
)

BASIC_GROUP = types.Chat(
    id=777,
    title="Old Group",
    photo=None,
    participants_count=3,
    date=None,
    version=1,
)


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, request):
        self.sent.append(request)
        return True


def _wire(monkeypatch, entity):
    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return entity

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(settings_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(settings_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(settings_mod, "ensure_connected", _connected)
    return client


@pytest.mark.parametrize(
    "tool_name,kwargs,request_type,field",
    [
        (
            "set_join_to_send",
            {"enabled": True},
            functions.channels.ToggleJoinToSendRequest,
            "enabled",
        ),
        (
            "set_join_request",
            {"enabled": True},
            functions.channels.ToggleJoinRequestRequest,
            "enabled",
        ),
        (
            "set_prehistory_hidden",
            {"hidden": True},
            functions.channels.TogglePreHistoryHiddenRequest,
            "enabled",
        ),
        (
            "set_participants_hidden",
            {"hidden": True},
            functions.channels.ToggleParticipantsHiddenRequest,
            "enabled",
        ),
        (
            "set_view_forum_as_messages",
            {"enabled": True},
            functions.channels.ToggleViewForumAsMessagesRequest,
            "enabled",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_toggle_sends_its_own_request(
    monkeypatch, tool_name, kwargs, request_type, field
):
    client = _wire(monkeypatch, CHANNEL)

    await getattr(settings_mod, tool_name)("@announcements", **kwargs)

    assert len(client.sent) == 1
    request = client.sent[0]
    assert isinstance(request, request_type)
    assert request.channel is CHANNEL, "the resolved entity did not reach the request"
    assert getattr(request, field) is True


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("set_join_to_send", {"enabled": False}),
        ("set_join_request", {"enabled": False}),
        ("set_prehistory_hidden", {"hidden": False}),
        ("set_participants_hidden", {"hidden": False}),
        ("set_view_forum_as_messages", {"enabled": False}),
    ],
)
@pytest.mark.asyncio
async def test_each_toggle_is_reversible(monkeypatch, tool_name, kwargs):
    """These are in this module BECAUSE they are reversible. A tool that only
    ever sends True would be a one-way door wearing a boolean."""
    client = _wire(monkeypatch, CHANNEL)

    await getattr(settings_mod, tool_name)("@announcements", **kwargs)

    assert client.sent[0].enabled is False


@pytest.mark.asyncio
async def test_signatures_carries_both_flags(monkeypatch):
    """Layer 227 split this into signatures_enabled and profiles_enabled."""
    client = _wire(monkeypatch, CHANNEL)

    await settings_mod.set_signatures("@announcements", enabled=True, show_profiles=True)

    request = client.sent[0]
    assert request.signatures_enabled is True
    assert request.profiles_enabled is True


@pytest.mark.asyncio
async def test_profiles_cannot_be_linked_when_signatures_are_off(monkeypatch):
    """A profile link on an unsigned post has nothing to attach to; sending it
    would be an incoherent pair Telegram has to reject or silently ignore."""
    client = _wire(monkeypatch, CHANNEL)

    await settings_mod.set_signatures("@announcements", enabled=False, show_profiles=True)

    request = client.sent[0]
    assert request.signatures_enabled is False
    assert request.profiles_enabled is False


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("set_join_to_send", {"enabled": True}),
        ("set_join_request", {"enabled": True}),
        ("set_prehistory_hidden", {"hidden": True}),
        ("set_participants_hidden", {"hidden": True}),
        ("set_signatures", {"enabled": True}),
        ("set_view_forum_as_messages", {"enabled": True}),
    ],
)
@pytest.mark.asyncio
async def test_a_basic_group_gets_a_sentence_and_no_request(monkeypatch, tool_name, kwargs):
    client = _wire(monkeypatch, BASIC_GROUP)

    result = await getattr(settings_mod, tool_name)(-777, **kwargs)

    assert client.sent == [], "a channel-only request went out for a basic group"
    assert "basic group" in result.lower()
    assert "supergroup" in result.lower(), "the refusal should say what to do about it"


def test_no_irreversible_setting_slipped_into_this_module():
    """The Structural group — DeleteChannel, ConvertToGigagroup — is deliberately
    not here: it needs a confirmation protocol this codebase does not have yet.
    This module's whole claim is that everything in it is undoable."""
    import inspect

    source = inspect.getsource(settings_mod)

    # The REQUEST form, not the bare name: the module docstring names these in
    # order to say it excludes them, and a check that cannot tell an explanation
    # from a call is a check nobody can write documentation around.
    for forbidden in ("DeleteChannel", "ConvertToGigagroup", "EditLocation"):
        assert (
            f"{forbidden}Request(" not in source
        ), f"{forbidden} is irreversible and needs a confirmation protocol first"
