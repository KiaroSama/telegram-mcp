"""Invite links: reading one, minting one, and telling those apart.

`messages.exportChatInvite` GENERATES a link. Both reading tools reached for it
first, which made "get the invite link" a mutation registered as read-only --
reachable under TELEGRAM_EXPOSED_TOOLS=read-only, and in multi-account mode
fanned out across every account at once.

The redeem half has its own edges: the hash parser was `link.split('/')[-1]`,
which happily accepts an attacker's host, and the lifecycle was decoded out of
error TEXT rather than out of Telethon's typed errors.

No network: a fake client records the TL requests it was handed.
"""

from types import SimpleNamespace

import pytest
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)
from telethon.tl.types import Channel, Chat

from telegram_mcp.tools import invites as mod


class _Client:
    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or {}

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(request)
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise AssertionError(f"unexpected request {name}")
        return answer

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _channel():
    return Channel(
        id=777,
        title="Ch",
        photo=None,
        date=None,
        broadcast=True,
        megagroup=False,
        access_hash=42,
        username=None,
    )


def _basic_chat():
    return Chat(id=88, title="Grp", photo=None, participants_count=3, date=None, version=1)


def _full(link="https://t.me/+abcDEF"):
    return SimpleNamespace(full_chat=SimpleNamespace(exported_invite=SimpleNamespace(link=link)))


@pytest.fixture
def _wire(monkeypatch):
    def wire(entity, answers=None):
        client = _Client(answers)
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return entity

        monkeypatch.setattr(mod, "ensure_connected", _ensure, raising=False)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


# --- reading is not minting -------------------------------------------------


@pytest.mark.asyncio
async def test_reading_a_channels_link_never_exports_a_new_one(_wire):
    client = _wire(_channel(), {"GetFullChannelRequest": _full()})

    result = await mod.get_invite_link(-1000000000777, account="a")

    assert "ExportChatInviteRequest" not in client.names, "a read-only tool minted a link"
    assert client.names == ["GetFullChannelRequest"]
    assert "https://t.me/+abcDEF" in result


@pytest.mark.asyncio
async def test_a_basic_group_still_uses_the_basic_group_call(_wire):
    """messages.getFullChat is only for basic groups; a channel needs
    channels.getFullChannel and answers CHAT_ID_INVALID otherwise."""
    client = _wire(_basic_chat(), {"GetFullChatRequest": _full("https://t.me/+ghiJKL")})

    result = await mod.get_invite_link(-88, account="a")

    assert client.names == ["GetFullChatRequest"]
    assert client.sent("GetFullChatRequest").chat_id == 88
    assert "https://t.me/+ghiJKL" in result


@pytest.mark.asyncio
async def test_a_chat_with_no_link_yet_says_which_tool_makes_one(_wire):
    _wire(
        _channel(),
        {
            "GetFullChannelRequest": SimpleNamespace(
                full_chat=SimpleNamespace(exported_invite=None)
            )
        },
    )

    result = await mod.get_invite_link(-1000000000777, account="a")

    assert "export_chat_invite" in result


def test_the_reading_tool_is_annotated_read_only_and_the_minting_tool_is_not():
    """TELEGRAM_EXPOSED_TOOLS=read-only prunes on this annotation, so it is the
    thing that decided whether a mutation stayed reachable."""
    import telegram_mcp.tools  # noqa: F401  (registration is a side effect)
    from telegram_mcp.runtime import mcp

    hints = {
        tool.name: getattr(tool.annotations, "read_only_hint", None)
        for tool in mcp._tool_manager.list_tools()
        if tool.name in ("get_invite_link", "export_chat_invite")
    }

    assert hints["get_invite_link"] is True
    assert hints["export_chat_invite"] is False, "a minting tool survives read-only exposure"


@pytest.mark.asyncio
async def test_exporting_sends_one_mutation_and_does_not_retry_an_ambiguous_failure(_wire):
    """The fallback re-ran the same mutation, so one ambiguous error could leave
    two live invite links behind."""
    client = _wire(_channel(), {"ExportChatInviteRequest": RuntimeError("timeout")})

    result = await mod.export_chat_invite(-1000000000777, account="a")

    assert client.names.count("ExportChatInviteRequest") == 1
    assert "error" in result.lower()


@pytest.mark.asyncio
async def test_exporting_returns_the_new_link(_wire):
    client = _wire(
        _channel(), {"ExportChatInviteRequest": SimpleNamespace(link="https://t.me/+n")}
    )

    result = await mod.export_chat_invite(-1000000000777, account="a")

    assert "https://t.me/+n" in result
    assert client.names == ["ExportChatInviteRequest"]


@pytest.mark.asyncio
async def test_the_minting_tool_refuses_to_fan_out_across_every_account(monkeypatch, _wire):
    """with_account(readonly=True) fans an omitted account out to all of them; for
    an exporter that is one brand-new invite link per account from one call."""
    from telegram_mcp import connection

    client = _wire(
        _channel(), {"ExportChatInviteRequest": SimpleNamespace(link="https://t.me/+n")}
    )
    monkeypatch.setattr(connection, "clients", {"work": client, "personal": client})

    result = await mod.export_chat_invite(-1000000000777)

    assert "account" in result.lower() and "required" in result.lower()
    assert client.requests == [], "an invite was minted on every account"


# --- the link parser --------------------------------------------------------


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://t.me/joinchat/AAAAAEHbEkejzxUjAUCfYg", "AAAAAEHbEkejzxUjAUCfYg"),
        ("https://t.me/+AAAAAEHbEkejzxUjAUCfYg", "AAAAAEHbEkejzxUjAUCfYg"),
        ("t.me/+AAAAAEHbEkejzxUjAUCfYg", "AAAAAEHbEkejzxUjAUCfYg"),
        ("https://telegram.me/joinchat/HASH123/", "HASH123"),
        ("https://t.me/+HASH123?utm=x#frag", "HASH123"),
        ("tg://join?invite=HASH123", "HASH123"),
        ("+HASH123", "HASH123"),
        ("HASH123", "HASH123"),
    ],
)
def test_supported_invite_forms_yield_their_hash(link, expected):
    assert mod._parse_invite_hash(link) == (expected, None)


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me.evil.example/+HASH123",
        "https://evil.example/joinchat/HASH123",
        "https://t.me/durov",
        "https://t.me/joinchat/",
        "https://t.me/",
        "tg://resolve?domain=durov",
        "",
        "   ",
    ],
)
def test_an_unsupported_or_malformed_invite_is_refused(link):
    parsed, error = mod._parse_invite_hash(link)
    assert parsed is None
    assert error


def test_a_refused_link_does_not_echo_the_link_back():
    """The hash is a bearer credential; a refusal is not a reason to repeat it."""
    _, error = mod._parse_invite_hash("https://evil.example/joinchat/SECRETHASH")
    assert "SECRETHASH" not in error


# --- redeeming --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_join_request_awaiting_approval_is_reported_as_pending_not_as_an_error(_wire):
    """INVITE_REQUEST_SENT means the request reached the admins."""
    client = _wire(
        None,
        {
            "CheckChatInviteRequest": SimpleNamespace(chat=None),
            "ImportChatInviteRequest": InviteRequestSentError(request=None),
        },
    )
    assert client is not None

    result = await mod.import_chat_invite("HASH123", account="a")

    assert "pending" in result.lower() or "approval" in result.lower()
    assert "error" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        (InviteHashExpiredError(request=None), "expired"),
        (InviteHashInvalidError(request=None), "invalid"),
        (UserAlreadyParticipantError(request=None), "already"),
    ],
)
async def test_each_lifecycle_state_comes_from_the_typed_error(_wire, error, expected):
    _wire(
        None,
        {
            "CheckChatInviteRequest": SimpleNamespace(chat=None),
            "ImportChatInviteRequest": error,
        },
    )

    result = await mod.import_chat_invite("HASH123", account="a")

    assert expected in result.lower()


@pytest.mark.asyncio
async def test_joining_by_link_parses_the_hash_out_of_the_url(_wire):
    client = _wire(
        None,
        {
            "CheckChatInviteRequest": SimpleNamespace(chat=None),
            "ImportChatInviteRequest": SimpleNamespace(chats=[SimpleNamespace(title="Grp")]),
        },
    )

    result = await mod.join_chat_by_link("https://t.me/joinchat/HASH123/", account="a")

    assert client.sent("ImportChatInviteRequest").hash == "HASH123"
    assert "Grp" in result


@pytest.mark.asyncio
async def test_joining_a_link_on_another_host_is_refused_without_a_request(_wire):
    client = _wire(None, {})

    result = await mod.join_chat_by_link("https://evil.example/joinchat/HASH123", account="a")

    assert client.requests == [], "an unsupported host still reached Telegram"
    assert "HASH123" not in result


@pytest.mark.asyncio
async def test_the_hash_never_reaches_the_logs(_wire, caplog):
    """The hash grants entry to the chat; a log line is not a place for it."""
    _wire(
        None,
        {
            "CheckChatInviteRequest": SimpleNamespace(chat=None),
            "ImportChatInviteRequest": RuntimeError("boom"),
        },
    )

    with caplog.at_level(0):
        await mod.import_chat_invite("SECRETHASH1234", account="a")

    assert "SECRETHASH1234" not in caplog.text
