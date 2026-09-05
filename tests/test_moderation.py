"""Moderation: banning, unbanning and promoting.

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which request the tools build and what they carry, not
about a returned string that could be right for the wrong reason.

These tools change somebody else's standing in a group, and `ban_user` spells
out twelve restriction flags by hand. A test that reads them back off the
recorded request is what catches the field a future edit drops.
"""

from types import SimpleNamespace

import pytest
import telethon
from telethon.tl.types import ChatAdminRights, ChatBannedRights

from telegram_mcp.tools import admin_rights, moderation as mod
from telegram_mcp.tools.admin_rights import promote_admin
from telegram_mcp.tools.moderation import ban_user

# Bans and the admin-rights model are two modules now. A tool resolves a
# seam from ITS OWN globals, so patching one module would miss the other.
_MODULES = (mod, admin_rights)


def _patch_both(monkeypatch, name, value):
    for module in _MODULES:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


# Every restriction ban_user enables by hand (moderation.py:307-322).
EVERY_RESTRICTION = (
    "view_messages",
    "send_messages",
    "send_media",
    "send_stickers",
    "send_gifs",
    "send_games",
    "send_inline",
    "embed_links",
    "send_polls",
    "change_info",
    "invite_users",
    "pin_messages",
)


def _chat(title="Ops Room"):
    return SimpleNamespace(
        id=99, title=title, username="ops_room", broadcast=False, megagroup=True
    )


def _user(user_id=4242):
    return SimpleNamespace(id=user_id, first_name="Ada", username="ada")


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, fails=None):
        self.requests = []
        self.fails = fails or {}

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in self.fails:
            raise self.fails[name]
        if name in ("EditBannedRequest", "EditAdminRequest"):
            return SimpleNamespace(updates=[])
        if name == "GetParticipantsRequest":
            return SimpleNamespace(users=[], participants=[])
        if name == "GetFullChannelRequest":
            return SimpleNamespace(full_chat=SimpleNamespace(about=""))
        raise AssertionError(f"unexpected request {name}")

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, chat=None, user=None):
        chat = chat if chat is not None else _chat()
        user = user if user is not None else _user()
        _patch_both(monkeypatch, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(which, _client):
            # The tools resolve the chat first, then the user, each by its own id.
            return user if which == "u" else chat

        _patch_both(monkeypatch, "ensure_connected", _ensure)
        _patch_both(monkeypatch, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_banning_a_user_sends_every_restriction_telegram_understands(_wire):
    """Twelve flags spelled out by hand is twelve chances to drop one. A ban that
    silently leaves send_media enabled is not a ban."""
    client = _wire(_Client())

    await ban_user("c", "u", account="a")

    assert client.names == ["EditBannedRequest"]
    rights = client.sent("EditBannedRequest").banned_rights
    assert isinstance(rights, ChatBannedRights)
    assert rights.until_date is None, "the ban is meant to be forever"
    missing = [flag for flag in EVERY_RESTRICTION if getattr(rights, flag) is not True]
    assert missing == [], f"these restrictions were not applied: {missing}"


@pytest.mark.asyncio
async def test_a_ban_names_the_chat_it_actually_banned_from(_wire):
    """The reply interpolates the resolved chat's title, so a tool that banned from
    a different chat than it names cannot pass."""
    client = _wire(_Client(), chat=_chat(title="Ops Room"))

    result = await ban_user("c", "u", account="a")

    assert "Ops Room" in result
    assert client.sent("EditBannedRequest").channel.title == "Ops Room"


@pytest.mark.asyncio
async def test_a_non_mutual_contact_ban_is_refused_in_a_sentence_not_a_code(_wire):
    """Telegram's own refusal is the caller's problem to act on, so it gets a
    sentence rather than an opaque error code."""
    error = telethon.errors.rpcerrorlist.UserNotMutualContactError(request=None)
    _wire(_Client(fails={"EditBannedRequest": error}))

    result = await ban_user("c", "u", account="a")

    assert "mutual contacts" in result
    assert "-ERR-" not in result, "a Telegram refusal was reported as an internal error"


@pytest.mark.asyncio
async def test_promoting_an_admin_sends_the_rights_it_was_given(_wire):
    """The defaults are generous; a caller who asks for less must get less."""
    client = _wire(_Client())

    await promote_admin("c", "u", rights={"add_admins": True, "ban_users": False}, account="a")

    assert client.names == ["EditAdminRequest"]
    granted = client.sent("EditAdminRequest").admin_rights
    assert isinstance(granted, ChatAdminRights)
    assert granted.add_admins is True, "the requested right was not granted"
    assert granted.ban_users is False, "a right the caller declined was granted anyway"
    assert granted.change_info is True, "an unmentioned right lost its default"
