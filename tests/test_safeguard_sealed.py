"""Approval messages are sealed: only a human sees and answers them (FR-035..FR-038).

The approval bot trusts presses from the owner's own accounts, and a Saved Messages reply
comes from the same account the agent acts as. So the agent must not reach the bot's chat
in any spelling, must not touch or read an approval message in Saved Messages, and must
never see an approval code in any tool result.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from telegram_mcp import safeguard
from telegram_mcp.safeguard import channels, grants, policy, sealed, taint

BOT_ID = "8123456789"
REQUEST = (
    "Approval K7Q2: Account: main · 42 · @me_user\nleaves Chat\nTool: leave_chat   Chat: 5\n"
    "Why asked: gated\n\nReply `yes K7Q2`, `always K7Q2` or `no K7Q2` from another device."
)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sealed, "sealed_path", lambda: tmp_path / "approval-messages.json")
    monkeypatch.setattr(grants, "grants_path", lambda: tmp_path / "always-approvals.json")
    sealed.reset_cache()
    grants.reset_cache()
    taint.clear()
    yield tmp_path / "approval-messages.json"
    sealed.reset_cache()
    grants.reset_cache()


# --- the bot's chat, in every spelling (FR-035) ---------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        BOT_ID,
        int(BOT_ID),
        "safeguard_bot",
        "@Safeguard_Bot",
        "https://t.me/safeguard_bot",
        "t.me/safeguard_bot?start=x",
        "http://telegram.me/safeguard_bot/",
        "tg://resolve?domain=safeguard_bot",
    ],
)
def test_the_bot_chat_is_refused_in_every_spelling(spelling):
    facts = policy.Facts(approval_chats=frozenset({BOT_ID, "safeguard_bot"}))
    for name, read_only in (("get_history", True), ("press_inline_button", False)):
        decision = policy.decide(name, read_only, False, {"chat_id": spelling}, facts)
        assert decision.reasons == ["touches_approval_channel"], (name, spelling)


def test_an_ordinary_link_is_not_mistaken_for_the_bot():
    facts = policy.Facts(approval_chats=frozenset({BOT_ID, "safeguard_bot"}))
    decision = policy.decide("get_history", True, False, {"chat_id": "t.me/other_chat"}, facts)
    assert decision.outcome == "run"


# --- the registry ------------------------------------------------------------------------------


def test_a_posted_request_is_remembered_across_a_restart_owner_only(store):
    sealed.remember_request("main", "K7Q2", 1065900, selves=[42, "Me_User"])
    sealed.reset_cache()
    assert sealed.is_sealed_target("main", {"chat_id": "me", "message_id": 1065900})
    assert store.exists()
    assert "K7Q2" in sealed.codes()


def test_a_corrupt_registry_is_moved_aside_and_the_pattern_still_hides_requests(store):
    store.write_text("{broken", encoding="utf-8")
    sealed.reset_cache()
    assert sealed.redact(REQUEST) == sealed.HIDDEN_REQUEST
    sealed.remember_code("ABCD")
    assert list(store.parent.glob("approval-messages.corrupt-*"))


# --- touching an approval message in Saved Messages (FR-036) -----------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"chat_id": "me", "message_id": 1065900, "new_text": "x"},
        {"chat_id": "self", "message_ids": [1, 1065900]},
        {"chat_id": 42, "message_id": 1065900},
        {"chat_id": "@me_user", "reply_to_message_id": 1065900},
        {"from_chat_id": "me", "to_chat_id": 7, "message_id": 1065900},
    ],
)
def test_an_approval_message_cannot_be_acted_on(arguments):
    sealed.remember_request("main", "K7Q2", 1065900, selves=[42, "me_user"])
    assert sealed.is_sealed_target("main", arguments)


def test_the_same_id_in_another_chat_or_another_message_is_free():
    sealed.remember_request("main", "K7Q2", 1065900, selves=[42, "me_user"])
    assert not sealed.is_sealed_target("main", {"chat_id": -1001234, "message_id": 1065900})
    assert not sealed.is_sealed_target("main", {"chat_id": "me", "message_id": 5})
    assert not sealed.is_sealed_target("work", {"chat_id": "me", "message_id": 1065900})


def test_without_an_account_every_account_is_checked():
    sealed.remember_request("work", "K7Q2", 77, selves=[9])
    assert sealed.is_sealed_target(None, {"chat_id": "me", "message_id": 77})


# --- reading (FR-037) ----------------------------------------------------------------------------


def test_the_request_text_and_every_issued_code_are_hidden():
    sealed.remember_code("K7Q2")
    assert sealed.redact(REQUEST) == sealed.HIDDEN_REQUEST
    escaped = json.dumps({"text": REQUEST, "id": 5})
    assert "K7Q2" not in sealed.redact(escaped)
    assert sealed.HIDDEN_REQUEST in sealed.redact(escaped)
    truncated = "Approval K7Q2: Account: main · 42 · @me_us..."
    assert "K7Q2" not in sealed.redact(truncated)
    assert sealed.redact("yes k7q2") == "yes [hidden]"
    assert sealed.redact("room K7Q3 is free") == "room K7Q3 is free"


def test_request_approval_records_the_code_before_any_channel_sees_it():
    seen = []

    class _Channel:
        kind = "dialog"

        def available(self):
            return True

        async def ask(self, request, timeout):
            seen.append(request.code in sealed.codes())
            return "declined"

    request = channels.new_request("leave_chat", "main", "5", "leaves 5", ["gated"])
    asyncio.run(channels.request_approval(request, [_Channel()], 5))
    assert seen == [True]


def test_posting_to_saved_messages_records_the_message(monkeypatch):
    posted = []

    class _Client:
        def add_event_handler(self, *a):
            pass

        def remove_event_handler(self, *a):
            pass

        async def send_message(self, chat, text):
            return SimpleNamespace(id=1066001)

    async def provide():
        return _Client()

    channel = channels.SavedMessagesChannel(
        client_provider=provide,
        on_posted=lambda message_id, code: posted.append((message_id, code)),
    )
    request = channels.new_request("leave_chat", "main", "5", "leaves 5", ["gated"])
    outcome = asyncio.run(channel.ask(request, 0.05))
    assert outcome == "timed_out" and posted == [(1066001, request.code)]


# --- the middleware ------------------------------------------------------------------------------


def _guard(result, sealed_target=None):
    asked = []

    class _Channel:
        kind = "dialog"

        def available(self):
            return True

        async def ask(self, request, timeout):
            asked.append(request)
            return "approved_once"

    async def _first(account, chat):
        return False

    async def _identity(account):
        return account

    async def _sealed(account, arguments):
        return sealed.is_sealed_target(account, arguments)

    guard = safeguard.Safeguard(
        hints={"edit_message": (False, False), "get_history": (True, False)}.get,
        channels=lambda ctx, account: [_Channel()],
        first_message=_first,
        ghost_on=lambda account, chat: False,
        approval_chats=lambda: frozenset({BOT_ID}),
        account_of=lambda arguments: "main",
        after=lambda account: None,
        identity=_identity,
        sealed_target=sealed_target or _sealed,
        timeout=300,
    )
    ran = []

    async def call_next(ctx):
        ran.append(ctx.params["name"])
        return result

    def call(name, arguments):
        ctx = SimpleNamespace(
            method="tools/call", params={"name": name, "arguments": arguments}, request_id=1
        )
        return asyncio.run(guard(ctx, call_next))

    return call, asked, ran


def test_editing_an_approval_message_is_refused_without_asking():
    sealed.remember_request("main", "K7Q2", 1065900, selves=[42])
    call, asked, ran = _guard("RESULT")
    result = call("edit_message", {"chat_id": "me", "message_id": 1065900, "new_text": "x"})
    assert result.is_error and asked == [] and ran == []
    assert "approval channel" in result.content[0].text


def test_a_read_result_never_carries_the_request_or_a_code():
    sealed.remember_code("K7Q2")
    raw = CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"messages": [{"text": REQUEST}]}))],
        structuredContent={"messages": [{"text": REQUEST}, {"text": "yes K7Q2"}]},
    )
    call, asked, ran = _guard(raw)
    result = call("get_history", {"chat_id": "me"})
    shown = result.content[0].text + json.dumps(result.structured_content)
    assert "K7Q2" not in shown and sealed.HIDDEN_REQUEST in shown
    plain_call, _, _ = _guard(REQUEST)
    assert plain_call("get_history", {"chat_id": "me"}) == sealed.HIDDEN_REQUEST
