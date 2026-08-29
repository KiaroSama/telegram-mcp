"""The shared client fixture, and the trap it has to avoid.

33 hand-rolled copies of this wiring existed across 28 test files. That
boilerplate is the direct reason ~38 registered tools have no behavioural test:
writing one started with 25 lines before it could assert anything.

The trap it must not fall into is this repository's oldest one. Tool modules
open with `from telegram_mcp.runtime import *`, so patching a name on `runtime`
binds a second name and leaves the one the tool actually calls untouched. The
fixture patches the tool module itself.
"""

import pytest

from telegram_mcp.tools import chats as chats_mod


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, request):
        self.sent.append(request)
        return None

    async def get_dialogs(self, limit=None, archived=None):
        return []


def test_it_patches_the_tool_module_not_runtime(wire_client):
    from telegram_mcp import runtime

    before = runtime.get_client
    client = wire_client(chats_mod, Recorder())

    assert chats_mod.get_client() is client
    assert runtime.get_client is before, (
        "the fixture reached through runtime; a star import means that patches a "
        "second name and not the one the tool calls"
    )


@pytest.mark.asyncio
async def test_the_resolver_can_be_given_a_fixed_entity(wire_client):
    sentinel = object()
    wire_client(chats_mod, Recorder(), entity=sentinel)

    assert await chats_mod.resolve_entity("anything") is sentinel


@pytest.mark.asyncio
async def test_a_custom_resolver_wins(wire_client):
    async def _resolve(chat_id, cl, account):
        return f"resolved:{chat_id}"

    wire_client(chats_mod, Recorder(), resolve=_resolve)

    assert await chats_mod.resolve_entity("@someone") == "resolved:@someone"


def test_the_patch_does_not_outlive_the_test():
    """monkeypatch's job, asserted rather than assumed: a fixture that leaked a
    patch across tests would be worse than the boilerplate it replaces."""
    assert not isinstance(chats_mod.get_client, type(lambda: None)) or (
        chats_mod.get_client.__name__ != "<lambda>"
    ), "a previous test's client lambda is still installed"
