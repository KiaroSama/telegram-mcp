"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")


@pytest.fixture
def wire_client(monkeypatch):
    """Patch a tool module's client seams in one call.

    There were 33 hand-rolled copies of this across 28 test files, and that is
    the direct reason ~38 registered tools have no behavioural test: writing one
    started with 25 lines of boilerplate before it could assert anything.

    Patch the module that OWNS each name. The tool modules star-import from
    `runtime`, so patching through `runtime` binds a second name and changes
    nothing the tool actually calls - the trap `tests/test_tool_registry.py`
    exists to catch.
    """

    def _wire(module, client, *, resolve=None, entity=None, marked_id=None):
        async def _resolve_entity(chat_id, cl=None, account=None):
            if resolve is not None:
                return await resolve(chat_id, cl, account)
            return entity if entity is not None else object()

        async def _ensure_connected(_client=None):
            return None

        monkeypatch.setattr(module, "get_client", lambda account=None: client)
        for name, value in (
            ("ensure_connected", _ensure_connected),
            ("resolve_entity", _resolve_entity),
            ("resolve_input_entity", _resolve_entity),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
        if marked_id is not None and hasattr(module, "get_marked_id"):
            monkeypatch.setattr(module, "get_marked_id", marked_id)
        return client

    return _wire
