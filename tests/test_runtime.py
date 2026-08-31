from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from telethon.tl.types import Channel, Chat, User

from telegram_mcp import runtime


def _tool_names(server):
    return {tool.name for tool in server._tool_manager.list_tools()}


def _synthetic_mcp():
    server = MCPServer("test")

    @server.tool(annotations=ToolAnnotations(title="Read", readOnlyHint=True))
    def read_tool():
        return "read"

    @server.tool(annotations=ToolAnnotations(title="Write", destructiveHint=True))
    def write_tool():
        return "write"

    return server


@pytest.mark.asyncio
async def test_shared_server_uses_stateless_http_transport(monkeypatch):
    """A service restart must not invalidate long-lived Streamable HTTP clients.

    Under mcp 1.x this was a constructor argument and could be read back off
    `settings`. 2.x takes it per call, so the only place the guarantee still
    exists is the argument `_serve` passes - which is what this now asserts.
    """
    from telegram_mcp import runner

    seen = {}

    async def _capture(**kwargs):
        seen.update(kwargs)

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setattr(runner.mcp, "run_streamable_http_async", _capture)
    await runner._serve("http")

    assert seen["stateless_http"] is True
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8765


@pytest.mark.asyncio
async def test_transport_security_is_omitted_unless_configured(monkeypatch):
    """Passing None over the SDK's own default would silently disable whatever
    rebinding protection it ships with. Absent means absent."""
    from telegram_mcp import runner

    seen = {}

    async def _capture(**kwargs):
        seen.update(kwargs)

    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(runner.mcp, "run_streamable_http_async", _capture)
    await runner._serve("http")

    assert "transport_security" not in seen

    seen.clear()
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "example.invalid")
    await runner._serve("http")

    assert seen["transport_security"].allowed_hosts == ["example.invalid"]
    assert seen["transport_security"].enable_dns_rebinding_protection is True


@pytest.mark.asyncio
async def test_every_tool_result_is_stamped_as_user_data():
    """The wiring, not just the helper.

    `_annotate_for_user` was covered; the thing that INSTALLS it was not, and it
    is the half that changed - mcp 2.x removed the `_mcp_server.request_handlers`
    seam the old hook swapped. Images are included on purpose: an earlier version
    tested `isinstance(block, TextContent)` and returned screenshots with nothing
    said about them at all.
    """
    from mcp.types import CallToolResult, ImageContent, TextContent

    result = CallToolResult(
        content=[
            TextContent(type="text", text="a message body"),
            ImageContent(type="image", data="AA==", mimeType="image/png"),
        ]
    )

    async def _call_next(_ctx):
        return result

    middleware = runtime._UserAudienceMiddleware()
    returned = await middleware(object(), _call_next)

    assert [block.annotations.audience for block in returned.content] == [["user"], ["user"]]


def test_the_stamper_is_actually_installed_on_the_server():
    """A middleware that exists and is never appended annotates nothing."""
    assert any(
        isinstance(m, runtime._UserAudienceMiddleware) for m in runtime.mcp.middleware
    ), "the user-audience middleware is not in the server's chain"


def test_installing_twice_does_not_stack_the_stamper():
    before = len(runtime.mcp.middleware)

    runtime._install_annotation_hook()

    assert len(runtime.mcp.middleware) == before


def test_the_handshake_reports_a_real_version():
    """`MCPServer` takes `version` explicitly where FastMCP filled it in, and
    passing nothing reports an empty string - which a client renders as a blank
    rather than falling back to anything."""
    assert runtime._server_version()
    assert runtime._server_version() != ""
    assert runtime.mcp.version == runtime._server_version()


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
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sender=SimpleNamespace(first_name="Jane", last_name="Doe"),
        views=10,
        forwards=2,
        reactions=SimpleNamespace(results=[SimpleNamespace(count=3), SimpleNamespace(count=None)]),
    )

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


def test_an_error_code_names_the_same_function_after_a_restart():
    """The code is a correlation tag: a user reports it, a maintainer greps
    mcp_errors.log for it. `hash()` of a str is salted per process (PEP 456), so
    before this was fixed the whole table was re-rolled on every restart — the log
    in this repo holds GEN-ERR-256 and GEN-ERR-679 for the same get_full_user
    failure three minutes apart. These literals are the contract: changing one
    silently orphans every historic log line that carries it.
    """
    assert "code: GEN-ERR-877" in runtime.log_and_format_error(
        "get_full_user", RuntimeError("boom")
    )
    assert "code: GEN-ERR-882" in runtime.log_and_format_error("some_tool", RuntimeError("boom"))
    assert "code: CHAT-ERR-658" in runtime.log_and_format_error("get_chat", RuntimeError("boom"))


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


# --- F18: audience=["user"] belongs on every content block, not only text ----


def test_every_kind_of_content_block_is_marked_as_user_data():
    """The hook claimed to annotate tool results; it only annotated text.

    An image, an audio clip or an embedded resource returned by a tool is just
    as much untrusted user data as a message body, and a client that decides
    what to trust from `audience` was told nothing about them.
    """
    from mcp.types import (
        AudioContent,
        BlobResourceContents,
        EmbeddedResource,
        ImageContent,
        ResourceLink,
        TextContent,
    )

    blocks = [
        TextContent(type="text", text="hello"),
        ImageContent(type="image", data="AA==", mimeType="image/png"),
        AudioContent(type="audio", data="AA==", mimeType="audio/ogg"),
        ResourceLink(type="resource_link", name="doc", uri="https://example.invalid/doc"),
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri="https://example.invalid/blob", blob="AA==", mimeType="image/png"
            ),
        ),
    ]

    annotated = runtime._annotate_for_user(blocks)

    assert [block.annotations.audience for block in annotated] == [["user"]] * len(blocks)


def test_an_explicit_annotation_is_left_alone():
    from mcp.types import Annotations, TextContent

    explicit = Annotations(audience=["assistant"])
    block = TextContent(type="text", text="hello", annotations=explicit)

    (result,) = runtime._annotate_for_user([block])

    assert result.annotations.audience == ["assistant"]
