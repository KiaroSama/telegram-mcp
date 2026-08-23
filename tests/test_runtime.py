from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from telethon.tl.types import Channel, Chat, PeerUser, User

from telegram_mcp import runtime


def _tool_names(server):
    return {tool.name for tool in server._tool_manager.list_tools()}


def _synthetic_mcp():
    server = FastMCP("test")

    @server.tool(annotations=ToolAnnotations(title="Read", readOnlyHint=True))
    def read_tool():
        return "read"

    @server.tool(annotations=ToolAnnotations(title="Write", destructiveHint=True))
    def write_tool():
        return "write"

    return server


def test_shared_server_uses_stateless_http_transport():
    """A service restart must not invalidate long-lived Streamable HTTP clients."""
    assert runtime.mcp.settings.stateless_http is True


def test_get_exposed_tools_mode_defaults_to_all(monkeypatch):
    monkeypatch.delenv("TELEGRAM_EXPOSED_TOOLS", raising=False)

    assert runtime._get_exposed_tools_mode() == "all"


def test_apply_exposed_tools_all_keeps_tools():
    server = _synthetic_mcp()

    removed = runtime._apply_exposed_tools_mode(server, "all")

    assert removed == []
    assert _tool_names(server) == {"read_tool", "write_tool"}


def test_apply_exposed_tools_read_only_removes_non_read_only_tools():
    server = _synthetic_mcp()

    removed = runtime._apply_exposed_tools_mode(server, "read-only")

    assert removed == ["write_tool"]
    assert _tool_names(server) == {"read_tool"}


def test_get_exposed_tools_mode_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("TELEGRAM_EXPOSED_TOOLS", "send-everything")

    with pytest.raises(SystemExit) as excinfo:
        runtime._get_exposed_tools_mode()

    message = str(excinfo.value)
    assert "TELEGRAM_EXPOSED_TOOLS" in message
    assert "all" in message
    assert "read-only" in message


def _synthetic_mcp_with_two_writes():
    server = _synthetic_mcp()

    @server.tool(annotations=ToolAnnotations(title="Send", destructiveHint=True))
    def send_tool():
        return "send"

    return server


def test_get_exposed_tools_mode_normalises_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_EXPOSED_TOOLS", " Read-Only+ send_tool , write_tool ")

    assert runtime._get_exposed_tools_mode() == "read-only+send_tool,write_tool"


def test_apply_exposed_tools_allowlist_keeps_named_write_tools():
    server = _synthetic_mcp_with_two_writes()

    removed = runtime._apply_exposed_tools_mode(server, "read-only+send_tool")

    assert removed == ["write_tool"]
    assert _tool_names(server) == {"read_tool", "send_tool"}


def test_apply_exposed_tools_allowlist_rejects_unknown_tool():
    server = _synthetic_mcp_with_two_writes()

    with pytest.raises(SystemExit) as excinfo:
        runtime._apply_exposed_tools_mode(server, "read-only+send_mesage")

    assert "send_mesage" in str(excinfo.value)
    assert _tool_names(server) == {"read_tool", "write_tool", "send_tool"}


def test_get_exposed_tools_mode_rejects_allowlist_with_all():
    with pytest.raises(SystemExit) as excinfo:
        runtime._get_exposed_tools_mode("all+send_tool")

    assert "read-only" in str(excinfo.value)


def test_get_exposed_tools_mode_rejects_empty_allowlist():
    with pytest.raises(SystemExit) as excinfo:
        runtime._get_exposed_tools_mode("read-only+")

    assert "at least one tool" in str(excinfo.value)


class _ResolvingClient:
    def __init__(self, method_name, failures):
        self.method_name = method_name
        self.failures = list(failures)
        self.dialogs_loaded = 0
        self.calls = []

    async def get_dialogs(self):
        self.dialogs_loaded += 1

    async def get_entity(self, identifier):
        return await self._resolve(identifier)

    async def get_input_entity(self, identifier):
        return await self._resolve(identifier)

    async def _resolve(self, identifier):
        self.calls.append(identifier)
        if self.failures:
            raise self.failures.pop(0)
        return f"{self.method_name}:{identifier}"


@pytest.mark.asyncio
async def test_resolve_entity_warms_cache_after_value_error(monkeypatch):
    async def noop(_client):
        return None

    client = _ResolvingClient("entity", [ValueError("cold cache")])
    monkeypatch.setattr(runtime, "ensure_connected", noop)

    assert await runtime.resolve_entity("chat", client) == "entity:chat"
    assert client.dialogs_loaded == 1


@pytest.mark.asyncio
async def test_resolve_input_entity_retries_after_connection_error(monkeypatch):
    async def noop(_client):
        return None

    client = _ResolvingClient("input", [ConnectionError(), ValueError("cold cache")])
    monkeypatch.setattr(runtime, "ensure_connected", noop)

    assert await runtime.resolve_input_entity("chat", client) == "input:chat"
    assert client.dialogs_loaded == 1


def test_marked_id_candidates_only_for_positive_integers():
    assert runtime._marked_id_candidates(123) == [-1000000000123, -123]
    assert runtime._marked_id_candidates(0) == []
    assert runtime._marked_id_candidates(-123) == []
    assert runtime._marked_id_candidates("123") == []


@pytest.mark.asyncio
async def test_resolve_entity_tries_marked_id_candidates_after_cache_miss(monkeypatch):
    async def noop(_client):
        return None

    client = _ResolvingClient("entity", [ValueError("not a user"), ValueError("still cold")])
    monkeypatch.setattr(runtime, "ensure_connected", noop)

    assert await runtime.resolve_entity(123, client) == "entity:-1000000000123"
    assert client.dialogs_loaded == 1
    assert client.calls == [123, 123, -1000000000123]


@pytest.mark.asyncio
async def test_resolve_input_entity_tries_marked_id_candidates_after_cache_miss(monkeypatch):
    async def noop(_client):
        return None

    client = _ResolvingClient("input", [ValueError("not a user"), ValueError("still cold")])
    monkeypatch.setattr(runtime, "ensure_connected", noop)

    assert await runtime.resolve_input_entity(123, client) == "input:-1000000000123"
    assert client.dialogs_loaded == 1
    assert client.calls == [123, 123, -1000000000123]


def test_json_serializer_handles_supported_and_unsupported_values():
    dt = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    assert runtime.json_serializer(dt) == "2026-01-02T03:04:00+00:00"
    assert runtime.json_serializer(b"hello\xff") == "hello�"
    with pytest.raises(TypeError):
        runtime.json_serializer(object())


def test_entity_type_filter_and_formatting_helpers():
    user = User(
        id=1,
        is_self=False,
        contact=False,
        mutual_contact=False,
        deleted=False,
        bot=False,
        bot_chat_history=False,
        bot_nochats=False,
        verified=False,
        restricted=False,
        min=False,
        bot_inline_geo=False,
        support=False,
        scam=False,
        apply_min_photo=False,
        fake=False,
        bot_attach_menu=False,
        premium=False,
        attach_menu_enabled=False,
        bot_can_edit=False,
        close_friend=False,
        stories_hidden=False,
        stories_unavailable=False,
        access_hash=1,
        first_name="John",
        last_name="Doe",
        username="jdoe",
        phone="123",
    )
    chat = Chat(
        id=2, title="Group\x00Name", photo=None, participants_count=3, date=None, version=1
    )
    channel = Channel(
        id=3,
        title="Channel",
        photo=None,
        date=None,
        creator=False,
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
        call_active=False,
        call_not_empty=False,
        fake=False,
        gigagroup=False,
        noforwards=False,
        join_to_send=False,
        join_request=False,
        forum=False,
        stories_hidden=False,
        stories_hidden_min=False,
        stories_unavailable=False,
        access_hash=1,
    )
    supergroup = Channel(
        id=4,
        title="Super",
        photo=None,
        date=None,
        creator=False,
        left=False,
        broadcast=False,
        verified=False,
        megagroup=True,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        call_active=False,
        call_not_empty=False,
        fake=False,
        gigagroup=False,
        noforwards=False,
        join_to_send=False,
        join_request=False,
        forum=False,
        stories_hidden=False,
        stories_hidden_min=False,
        stories_unavailable=False,
        access_hash=1,
    )

    assert runtime.get_entity_type(user) == "User"
    assert runtime.get_entity_filter_type(user) == "user"
    assert runtime.get_entity_type(chat) == "Group (Basic)"
    assert runtime.get_entity_filter_type(chat) == "group"
    assert runtime.get_entity_type(channel) == "Channel"
    assert runtime.get_entity_filter_type(channel) == "channel"
    assert runtime.get_entity_type(supergroup) == "Supergroup"
    assert runtime.get_entity_filter_type(supergroup) == "group"
    assert runtime.get_entity_filter_type(object()) is None
    assert runtime.get_marked_id(user) == 1
    assert runtime.get_marked_id(chat) == -2
    assert runtime.get_marked_id(channel) == -1000000000003
    assert runtime.get_marked_id(supergroup) == -1000000000004

    assert runtime.format_entity(user) == {
        "id": 1,
        "name": "John Doe",
        "type": "user",
        "username": "jdoe",
        "phone": "123",
    }
    assert runtime.format_entity(chat) == {"id": -2, "name": "GroupName", "type": "group"}
    assert runtime.format_entity(channel) == {
        "id": -1000000000003,
        "name": "Channel",
        "type": "channel",
    }


def test_message_formatting_sender_and_engagement_helpers():
    message = SimpleNamespace(
        id=42,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        message="hello\x00world",
        from_id=PeerUser(user_id=99),
        media=SimpleNamespace(),
        sender=SimpleNamespace(first_name="Jane", last_name="Doe"),
        views=10,
        forwards=2,
        reactions=SimpleNamespace(results=[SimpleNamespace(count=3), SimpleNamespace(count=None)]),
    )

    formatted = runtime.format_message(message)
    assert formatted["from_id"] == 99
    assert formatted["has_media"] is True
    assert formatted["text"] == "helloworld"
    assert runtime.get_sender_name(message) == "Jane Doe"
    assert runtime.get_sender_name(SimpleNamespace(sender=None)) == "Unknown"
    assert (
        runtime.get_sender_name(SimpleNamespace(sender=SimpleNamespace(title="A\nGroup")))
        == "A Group"
    )
    assert runtime.get_engagement_info(message) == " | views:10, forwards:2, reactions:3"
    assert runtime.get_engagement_dict(message) == {"views": 10, "forwards": 2, "reactions": 3}
    assert runtime.get_engagement_info(SimpleNamespace()) == ""
    assert runtime.get_engagement_dict(SimpleNamespace()) is None


def test_log_and_format_error_returns_custom_and_generated_messages(caplog):
    custom = runtime.log_and_format_error(
        "validate_user",
        runtime.ValidationError("bad"),
        prefix="VALIDATION-001",
        user_message="bad input",
        user_id="abc",
    )
    assert custom == "bad input"

    generated = runtime.log_and_format_error("get_chat", RuntimeError("boom"))
    assert "code: CHAT-ERR-" in generated
    assert "Check mcp_errors.log" in generated


def test_a_telegram_refusal_is_reported_as_a_sentence_not_a_code():
    """Found live: get_message_reactions on a broadcast channel answered
    `An error occurred (code: GEN-ERR-581)`.

    Underneath was `BroadcastForbiddenError` — Telegram understood the request and
    declined it because of what the peer is. A generic code makes that
    indistinguishable from a bug in this server, which is where an hour went.
    """
    from telethon import errors

    message = runtime.log_and_format_error(
        "get_message_reactions", errors.BroadcastForbiddenError(request=None)
    )

    assert "broadcast channel" in message
    assert "An error occurred" not in message
    assert "code:" in message, "the code is still worth carrying for the log"


def test_an_unrecognised_error_still_gets_the_generic_message():
    """The table must not swallow errors it knows nothing about."""
    message = runtime.log_and_format_error("some_tool", RuntimeError("something new"))

    assert "An error occurred (code:" in message


def test_every_refusal_in_the_table_names_a_real_telethon_error():
    """A typo in a key is silent: the entry simply never matches.

    Checked against telethon's own error classes so the table cannot drift into
    describing errors that do not exist.
    """
    from telethon import errors

    missing = [name for name in runtime.TELEGRAM_REFUSALS if not hasattr(errors, name)]
    assert not missing, f"these are not telethon errors: {missing}"
