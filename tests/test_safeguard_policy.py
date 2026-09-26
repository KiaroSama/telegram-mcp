"""The safeguard's rules: which calls run, which wait for the owner, which are refused.

Pure decisions, no Telegram and no clients: a tool name, its hints, its arguments and a
few facts the middleware gathered go in; run, ask or refuse comes out. The owner's
choices (2026-09-26): reads and ordinary writes run freely; deletions, leaving, banning,
sessions, profile and privacy, invite joins, bulk sends, a first message to a stranger,
seen signals under ghost mode and turning ghost mode off wait for approval; anything that
reaches for the approval channel itself is refused outright.
"""

import pytest

from telegram_mcp.safeguard import policy
from telegram_mcp.tools import mcp  # noqa: F401  (registers every tool)


def _registered():
    return list(mcp._tool_manager.list_tools())


def _decide(name, arguments=None, *, read_only=False, destructive=True, **facts):
    return policy.decide(name, read_only, destructive, arguments or {}, policy.Facts(**facts))


# --- classification ----------------------------------------------------------------


def test_the_explicit_sets_do_not_overlap():
    sets = [policy.GATED, policy.SEEN_SIGNAL, policy.SEND, policy.FREE_WRITES]
    for i, a in enumerate(sets):
        for b in sets[i + 1 :]:
            assert not a & b, sorted(a & b)


def test_every_registered_write_tool_is_named_in_exactly_one_set():
    """A write tool the table forgets would fall to the safety net silently; naming every
    one keeps the decision a deliberate one."""
    named = policy.GATED | policy.SEEN_SIGNAL | policy.SEND | policy.FREE_WRITES
    unnamed = [
        t.name for t in _registered() if not t.annotations.read_only_hint and t.name not in named
    ]
    assert not unnamed, f"write tools with no safeguard category: {', '.join(sorted(unnamed))}"


def test_the_table_names_no_tool_that_does_not_exist():
    registered = {t.name for t in _registered()}
    named = policy.GATED | policy.SEEN_SIGNAL | policy.SEND | policy.FREE_WRITES
    assert not named - registered


def test_read_only_tools_are_free():
    assert policy.categorize("get_history", read_only=True, destructive=False) == "free"


def test_an_unnamed_destructive_tool_is_gated_by_the_safety_net():
    assert policy.categorize("brand_new_wipe", read_only=False, destructive=True) == "gated"
    assert policy.categorize("brand_new_note", read_only=False, destructive=False) == "free"


@pytest.mark.parametrize(
    "tool",
    [
        "delete_message",
        "delete_messages_bulk",
        "delete_chat_history",
        "clear_secret_history",
        "leave_chat",
        "ban_user",
        "block_user",
        "terminate_authorization",
        "update_profile",
        "set_profile_photo",
        "set_privacy_settings",
        "join_chat_by_link",
        "import_chat_invite",
    ],
)
def test_the_owners_gated_set_asks(tool):
    decision = _decide(tool)
    assert decision.outcome == "ask"
    assert "gated" in decision.reasons


@pytest.mark.parametrize(
    "tool", ["send_message", "reply_to_message", "edit_message", "send_reaction"]
)
def test_ordinary_writes_run_freely(tool):
    assert _decide(tool).outcome == "run"


# --- dynamic rules -----------------------------------------------------------------


def test_a_first_message_to_a_stranger_asks():
    decision = _decide("send_message", {"chat_id": 42}, first_message=True)
    assert (decision.outcome, decision.reasons) == ("ask", ["first_message"])


def test_first_message_only_concerns_sending_tools():
    assert _decide("edit_message", first_message=True).outcome == "run"


def test_a_bulk_send_asks():
    decision = _decide("send_message", bulk=True)
    assert decision.reasons == ["bulk_send"]


def test_a_tainted_write_asks_and_carries_the_sources():
    tainted = [{"kind": "url", "source_chat": -5}]
    decision = _decide("send_message", tainted=tainted)
    assert decision.outcome == "ask"
    assert decision.reasons == ["tainted"]
    assert decision.tainted == tainted


def test_a_read_carrying_untrusted_text_stays_free():
    """Resolving a username someone mentioned only reads; nothing is done in their name."""
    tainted = [{"kind": "username", "source_chat": -5}]
    decision = _decide("resolve_username", read_only=True, destructive=False, tainted=tainted)
    assert decision.outcome == "run"


def test_seen_signals_ask_only_under_ghost_mode():
    for tool in ("mark_as_read", "mark_secret_read", "send_secret_typing"):
        assert _decide(tool, ghost_on=True).reasons == ["seen_signal_under_ghost"]
        assert _decide(tool, ghost_on=False).outcome == "run"


def test_turning_ghost_mode_off_asks_and_on_is_free():
    off = _decide("set_ghost_mode", {"enabled": False}, destructive=False)
    on = _decide("set_ghost_mode", {"enabled": True}, destructive=False)
    assert (off.outcome, off.reasons) == ("ask", ["ghost_off"])
    assert on.outcome == "run"


def test_several_reasons_are_all_reported():
    decision = _decide("send_message", first_message=True, bulk=True)
    assert decision.reasons == ["first_message", "bulk_send"]


# --- the approval channel is out of reach --------------------------------------------


def test_touching_the_approval_bot_chat_is_refused_without_asking():
    facts = {"approval_chats": frozenset({"7000001", "approvalbot"})}
    for arguments in ({"chat_id": 7000001}, {"chat_id": "@ApprovalBot"}, {"peer": "7000001"}):
        decision = _decide("press_inline_button", arguments, **facts)
        assert (decision.outcome, decision.reasons) == ("refuse", ["touches_approval_channel"])
    # Even reading it: the codes and buttons live there.
    assert (
        _decide("get_history", {"chat_id": 7000001}, read_only=True, destructive=False, **facts)
    ).outcome == "refuse"


def test_writing_a_pending_code_anywhere_is_refused():
    facts = {"pending_codes": frozenset({"K7Q2"})}
    decision = _decide("send_message", {"chat_id": "me", "message": "yes k7q2"}, **facts)
    assert decision.outcome == "refuse"
    # A word that merely contains the letters is not the code.
    assert _decide("send_message", {"message": "ak7q2b"}, **facts).outcome == "run"


def test_refusal_wins_over_every_other_reason():
    decision = _decide(
        "delete_message",
        {"chat_id": 7000001},
        approval_chats=frozenset({"7000001"}),
        first_message=True,
    )
    assert decision.reasons == ["touches_approval_channel"]


# --- bulk window -------------------------------------------------------------------


def test_the_sixth_distinct_chat_within_a_minute_is_bulk():
    now = [0.0]
    window = policy.SendWindow(clock=lambda: now[0])
    for chat in range(5):
        assert not window.is_bulk("acct", "send_message", chat)
        window.record("acct", "send_message", chat)
    assert not window.is_bulk("acct", "send_message", 3)  # a chat already counted
    assert window.is_bulk("acct", "send_message", 99)
    assert not window.is_bulk("acct", "send_file", 99)  # per tool
    assert not window.is_bulk("other", "send_message", 99)  # per account
    now[0] = 61.0
    assert not window.is_bulk("acct", "send_message", 99)  # the minute has passed


# --- the kernel's own files are out of reach (FR-024) --------------------------------


def _protected():
    import os

    import telegram_mcp.safeguard as package

    return os.path.dirname(package.__file__)


@pytest.mark.parametrize(
    "path",
    [
        lambda root: root + "/policy.py",
        lambda root: root,
        lambda root: root.upper() + r"\policy.py",
        lambda root: root + "/../safeguard/channels.py",
        lambda root: "telegram_mcp/safeguard/policy.py",
        lambda root: r".\TELEGRAM_MCP\Safeguard\__init__.py",
    ],
)
def test_a_path_into_the_safeguard_package_is_refused(path):
    target = path(_protected())
    decision = _decide(
        "download_media",
        {"chat_id": 5, "message_id": 1, "file_path": target},
        protected_paths=(_protected(),),
    )
    assert (decision.outcome, decision.reasons) == ("refuse", ["touches_safeguard_files"])


def test_a_path_elsewhere_is_not_refused():
    decision = _decide(
        "download_media",
        {"chat_id": 5, "message_id": 1, "file_path": "D:/media/downloads/a.jpg"},
        protected_paths=(_protected(),),
    )
    assert decision.outcome == "run"
