"""Two folder edits at once used to keep one of them, and tell both they worked.

Telegram has no compare-and-set on a dialog filter. `UpdateDialogFilterRequest`
replaces the WHOLE filter, and `add_chat_to_folder` rebuilds that filter from a
snapshot it read moments earlier — so two concurrent calls each write their own
snapshot-plus-one and the later write erases the earlier chat. Both calls still
return "added".

MCP clients issue tool calls concurrently, so "add these five chats to folder 3"
is the normal way this gets used, not a corner case. `aliases.py:update_aliases`
already makes this argument for its own store.
"""

import asyncio

import pytest
from telethon.tl import types

from telegram_mcp.tools import folders as folders_mod


class SharedFilterStore:
    """One mutable filter list, as Telegram would hold it, with a real await
    between the read and the write so two callers can genuinely interleave."""

    def __init__(self):
        self.filter = types.DialogFilter(
            id=3,
            title=types.TextWithEntities(text="Work", entities=[]),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
        )
        self.writes = 0

    async def __call__(self, request):
        from telethon.tl import functions

        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            # The interleaving point. Without a lock both callers read the same
            # empty include_peers here.
            await asyncio.sleep(0)
            return types.messages.DialogFilters(filters=[self.filter], tags_enabled=False)
        if isinstance(request, functions.messages.UpdateDialogFilterRequest):
            await asyncio.sleep(0)
            self.writes += 1
            self.filter = request.filter
            return True
        raise AssertionError(f"unexpected request {type(request).__name__}")

    async def get_me(self, input_peer=False):
        return types.InputPeerUser(user_id=1, access_hash=0)


@pytest.fixture
def wired(monkeypatch):
    store = SharedFilterStore()

    async def _resolve_input(chat_id, cl=None, account=None):
        return types.InputPeerChannel(channel_id=int(chat_id), access_hash=0)

    async def _connected(cl):
        return None

    monkeypatch.setattr(folders_mod, "get_client", lambda account=None: store)
    monkeypatch.setattr(folders_mod, "resolve_input_entity", _resolve_input)
    monkeypatch.setattr(folders_mod, "ensure_connected", _connected, raising=False)
    monkeypatch.setattr(folders_mod, "_folder_locks", {})
    return store


@pytest.mark.asyncio
async def test_two_concurrent_adds_both_survive(wired):
    await asyncio.gather(
        folders_mod.add_chat_to_folder(3, "111"),
        folders_mod.add_chat_to_folder(3, "222"),
    )

    kept = {getattr(p, "channel_id", None) for p in wired.filter.include_peers}
    assert kept == {111, 222}, f"one of the two writes was lost: the folder holds {kept}"
    assert wired.writes == 2


@pytest.mark.asyncio
async def test_five_concurrent_adds_all_survive(wired):
    await asyncio.gather(*(folders_mod.add_chat_to_folder(3, str(100 + n)) for n in range(5)))

    kept = {getattr(p, "channel_id", None) for p in wired.filter.include_peers}
    assert kept == {100, 101, 102, 103, 104}, f"the folder holds {kept}"


@pytest.mark.asyncio
async def test_the_lock_is_per_folder_not_global(wired):
    """Guard the guard: serialising every folder edit in the process would be a
    different bug. Two different folders must not contend."""
    first = folders_mod._folder_lock("acct", 3)
    second = folders_mod._folder_lock("acct", 4)
    same = folders_mod._folder_lock("acct", 3)

    assert first is same
    assert first is not second


def test_one_account_label_and_none_share_a_lock(monkeypatch):
    """`get_client` ignores the account argument when a single account is
    configured, so `None` and the real label are one session and must be one
    lock. account_key is the rule that decides it."""
    monkeypatch.setattr(folders_mod, "_folder_locks", {})
    from telegram_mcp import runtime

    monkeypatch.setattr(runtime, "clients", {"solo": object()}, raising=False)

    assert folders_mod._folder_lock(None, 3) is folders_mod._folder_lock("solo", 3)


def test_no_folder_tool_returns_a_rendered_exception():
    """Every other failure path in the package goes through log_and_format_error,
    which routes the exception through safe_exception so only its type, length
    and frame basenames are recorded. These three sites handed the caller the
    rendered text instead."""
    import inspect

    source = inspect.getsource(folders_mod)

    assert "str(e)" not in source
    assert source.count("Failed to resolve chat") == 0
