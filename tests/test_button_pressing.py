"""Pressing a button, and refusing to press the wrong one.

Split from ``test_buttons.py``, which had grown past 900 lines covering two
subjects. That file is about what a keyboard IS and how it is reported; this one
is about the press - which is the half that can act on someone else's account.

The thread running through it: **an index is a position, not an identity.** A
keyboard can change between the listing and the press, so the index that was
right when the caller read it can address a different button by the time they
act. That is why a press carries a token and why a changed keyboard is refused
rather than pressed, and why the legacy pair must not become a second, weaker
way in that skips those checks.
"""

import json
from types import SimpleNamespace

import pytest

import telegram_mcp.tools.buttons as buttons_tool
from telegram_mcp.tools.buttons import click_button
from helpers_buttons import (
    _UNUSABLE_TOKEN,
    _button,
    _callback,
    _inspect,
    _message,
    _tokens_of,
    make_wire,
)


@pytest.fixture
def _wire(monkeypatch):
    return make_wire(monkeypatch)


@pytest.fixture
def _wire_legacy(monkeypatch, _wire):
    """The legacy tools live in messages_state; they must reach the safe path
    here, so both modules are wired onto the same fake client."""
    from telegram_mcp.tools import messages_state

    def use(msg, answer=None):
        client = _wire(msg, answer)
        monkeypatch.setattr(messages_state, "get_client", lambda account=None: client)

        async def _connect(cl):
            return None

        async def _resolve(chat_id, cl):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(messages_state, "ensure_connected", _connect, raising=False)
        monkeypatch.setattr(messages_state, "resolve_entity", _resolve)
        return client

    return use


@pytest.mark.asyncio
async def test_the_legacy_press_refuses_to_choose_a_button_by_its_label(_wire_legacy):
    """Two buttons can render identically with different payloads; the legacy
    tool matched the first label and pressed it without a word."""
    from telegram_mcp.tools.messages_state import press_inline_button

    rows = [[_callback(text="Confirm", data=b"HARMLESS"), _callback(text="Confirm", data=b"WIPE")]]
    client = _wire_legacy(_message(rows), answer=SimpleNamespace(message="ok", alert=None))

    result = await press_inline_button(1, 7, button_text="Confirm", account="default")

    assert "button_index" in result
    assert client.calls == [], "a callback was sent for a label-chosen button"


@pytest.mark.asyncio
async def test_the_legacy_press_needs_a_message_id_not_the_latest_message(_wire_legacy):
    from telegram_mcp.tools.messages_state import press_inline_button

    client = _wire_legacy(_message([[_callback()]]))

    result = await press_inline_button(1, button_index=0, account="default")

    assert "message_id" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_the_legacy_press_goes_through_the_index_checked_path(_wire_legacy):
    from telegram_mcp.tools.messages_state import press_inline_button

    rows = [[_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")]]
    client = _wire_legacy(
        _message(rows), answer=SimpleNamespace(message="done", alert=None, url=None)
    )

    token = _tokens_of(await _inspect())[1]
    payload = json.loads(
        await press_inline_button(
            1, 7, button_index=1, button_text="No", press_token=token, account="default"
        )
    )

    assert client.calls[0].data == b"NO"
    assert payload["results"][0]["button_index"] == 1


@pytest.mark.asyncio
async def test_the_legacy_press_uses_a_supplied_label_as_a_guard_not_a_selector(_wire_legacy):
    """button_text alongside an index is the expectation click_button checks."""
    from telegram_mcp.tools.messages_state import press_inline_button

    client = _wire_legacy(_message([[_callback(text="Delete", data=b"DEL")]]))

    result = await press_inline_button(
        1, 7, button_text="Confirm", button_index=0, press_token=_UNUSABLE_TOKEN, account="x"
    )

    assert "nothing was pressed" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_the_legacy_listing_is_the_safe_listing(_wire_legacy):
    """The old listing returned the raw URL and no button kind."""
    from telegram_mcp.tools.messages_state import list_inline_buttons

    _wire_legacy(
        _message(
            [[_callback(), _button("KeyboardButtonUrl", text="Open", url="https://e.example")]]
        )
    )

    payload = json.loads(await list_inline_buttons(1, 7, account="default"))

    assert payload["pressable_indexes"] == [0]
    assert payload["results"][1]["kind"] == "url"


@pytest.mark.asyncio
async def test_the_legacy_listing_needs_a_message_id(_wire_legacy):
    from telegram_mcp.tools.messages_state import list_inline_buttons

    _wire_legacy(_message([[_callback()]]))

    assert "message_id" in await list_inline_buttons(1, account="default")


@pytest.mark.asyncio
async def test_pressing_without_an_expected_label_is_refused_before_any_callback(_wire):
    """expect_text was merely recommended, so an index taken from a listing made
    at any point in the past still sent a real callback. A bot that edits its own
    keyboard turns that into a press on whatever now sits at that position -- the
    audit sent a DELETE payload through it with no guard at all."""
    client = _wire(
        _message([[_callback(text="Delete everything", data=b"DELETE")]]),
        answer=SimpleNamespace(message="gone", alert=None, url=None),
    )

    result = await click_button(1, 7, 0, account="default")

    assert "expect_text" in result
    assert client.calls == [], "a callback was sent against an unverified index"


@pytest.mark.asyncio
async def test_the_refusal_costs_nothing_not_even_a_read(_wire):
    """Nothing about the message can change the answer, so nothing about the
    message needs fetching."""
    client = _wire(_message([[_callback()]]))

    await click_button(1, 7, 0, account="default")

    assert client.calls == []


@pytest.mark.asyncio
async def test_the_legacy_press_also_needs_the_label_it_was_listed_under(_wire_legacy):
    """The older name must not be the weaker route to the same callback."""
    from telegram_mcp.tools.messages_state import press_inline_button

    client = _wire_legacy(
        _message([[_callback(text="Delete everything", data=b"DELETE")]]),
        answer=SimpleNamespace(message="gone", alert=None, url=None),
    )

    result = await press_inline_button(1, 7, button_index=0, account="default")

    assert "button_text" in result
    assert client.calls == [], "the legacy name pressed an unverified index"


@pytest.mark.asyncio
async def test_inspect_publishes_a_distinct_press_token_per_pressable_button(_wire):
    _wire(
        _message(
            [
                [_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")],
                [_button("KeyboardButtonUrl", text="Open", url="https://e.example")],
            ]
        )
    )

    tokens = _tokens_of(await _inspect())

    assert tokens[0] and tokens[1]
    assert tokens[0] != tokens[1], "one token authorized two different buttons"
    assert tokens[2] is None, "a token was minted for a button no press can reach"


@pytest.mark.asyncio
async def test_the_token_does_not_carry_the_callback_payload(_wire):
    """The payload is opaque bot state; publishing it is what the index was for."""
    import base64

    secret = b"transfer:all:to:attacker"
    _wire(_message([[_callback(text="Confirm", data=secret)]]))

    blob = json.dumps(await _inspect())

    assert secret.decode() not in blob
    assert secret.hex() not in blob
    assert base64.b64encode(secret).decode().rstrip("=") not in blob


@pytest.mark.asyncio
async def test_pressing_without_a_press_token_is_refused_before_any_callback(_wire):
    client = _wire(
        _message([[_callback(text="Confirm", data=b"DELETE")]]),
        answer=SimpleNamespace(message="gone", alert=None, url=None),
    )

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert "press_token" in result
    assert client.calls == [], "a callback was sent against an unauthenticated index"


@pytest.mark.asyncio
async def test_a_label_kept_while_the_payload_changed_is_refused(_wire):
    """THE defect: expect_text compares the label, so a bot that keeps the label
    and swaps the callback data passed the guard and the press went through."""
    client = _wire(_message([[_callback(text="Confirm", data=b"HARMLESS")]]))
    token = _tokens_of(await _inspect())[0]

    # Same visible label, different payload — an edit expect_text cannot see.
    client._msg = _message([[_callback(text="Confirm", data=b"WIPE")]])
    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=token, account="default"
    )

    assert "press_token" in result
    assert client.calls == [], "pressed a button whose payload had been swapped"


@pytest.mark.asyncio
async def test_a_token_minted_for_another_button_does_not_authorize_this_one(_wire):
    client = _wire(
        _message([[_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )
    tokens = _tokens_of(await _inspect())

    result = await click_button(
        1, 7, 1, expect_text="No", press_token=tokens[0], account="default"
    )

    assert client.calls == [], "one button's token pressed another button"
    assert "press_token" in result


@pytest.mark.asyncio
async def test_a_token_minted_for_another_message_or_chat_is_refused(_wire):
    client = _wire(
        _message([[_callback(text="Confirm", data=b"OK")]], message_id=7),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )
    token = _tokens_of(await _inspect(chat_id=1, message_id=7))[0]

    client._msg = _message([[_callback(text="Confirm", data=b"OK")]], message_id=8)
    other_message = await click_button(
        1, 8, 0, expect_text="Confirm", press_token=token, account="default"
    )
    client._msg = _message([[_callback(text="Confirm", data=b"OK")]], message_id=7)
    other_chat = await click_button(
        2, 7, 0, expect_text="Confirm", press_token=token, account="default"
    )
    other_account = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=token, account="second"
    )

    assert client.calls == [], "a token crossed a message, chat or account boundary"
    for refusal in (other_message, other_chat, other_account):
        assert "press_token" in refusal


@pytest.mark.asyncio
async def test_a_token_from_before_a_restart_is_refused(_wire, monkeypatch):
    """The key is minted per process, so no token survives a restart. One that did
    would be a durable authorization for a keyboard nobody re-read."""
    client = _wire(
        _message([[_callback(text="Confirm", data=b"OK")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )
    stale = _tokens_of(await _inspect())[0]

    monkeypatch.setattr(buttons_tool, "_PRESS_KEY", b"a-different-process-key")
    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=stale, account="default"
    )

    assert client.calls == []
    assert "press_token" in result


@pytest.mark.asyncio
async def test_a_forged_token_is_refused(_wire):
    """A plain digest of the facts would be forgeable by anyone who can read the
    listing; the binding is keyed, so only this process can mint one."""
    import hashlib

    client = _wire(
        _message([[_callback(text="Confirm", data=b"OK")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )
    forged = "b1:" + hashlib.sha256(b"default|1|7|0|callback|Confirm|OK").hexdigest()[:32]

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=forged, account="default"
    )

    assert client.calls == []
    assert "press_token" in result


@pytest.mark.asyncio
async def test_a_valid_token_presses_the_button_it_was_minted_for(_wire):
    client = _wire(
        _message([[_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")]]),
        answer=SimpleNamespace(message="done", alert=None, url=None),
    )
    tokens = _tokens_of(await _inspect())

    payload = json.loads(
        await click_button(1, 7, 1, expect_text="No", press_token=tokens[1], account="default")
    )

    assert client.calls[0].data == b"NO"
    assert payload["results"][0]["button_index"] == 1


@pytest.mark.asyncio
async def test_two_raw_labels_that_normalize_to_one_display_string_are_refused(_wire):
    """`Con<ZWSP>firm` and `Confirm` are different buttons that read identically.
    expect_text compares the cleaned label, so it cannot separate them, and a
    listing showing them as two identical rows was already misleading."""
    client = _wire(
        _message([[_callback(text="Con​firm", data=b"WIPE"), _callback(text="Confirm")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )

    payload = await _inspect()
    listed = payload["results"]

    assert [b["text"] for b in listed] == ["Confirm", "Confirm"]
    assert listed[0].get("text_collision") is True
    assert listed[1].get("text_collision") is True
    assert payload["pressable_indexes"] == [], "a colliding label stayed pressable"
    assert _tokens_of(payload) == {0: None, 1: None}

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token="b1:0" * 8, account="default"
    )

    assert client.calls == []
    assert "same label" in result


@pytest.mark.asyncio
async def test_two_identical_raw_labels_are_still_separable_by_their_tokens(_wire):
    """A genuine duplicate label is not a sanitizer collision: nothing was hidden,
    and the binding still names exactly one button."""
    client = _wire(
        _message([[_callback(text="Confirm", data=b"A"), _callback(text="Confirm", data=b"B")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )
    tokens = _tokens_of(await _inspect())

    await click_button(1, 7, 1, expect_text="Confirm", press_token=tokens[1], account="default")

    assert client.calls[0].data == b"B"


@pytest.mark.asyncio
async def test_the_legacy_press_also_needs_the_binding(_wire_legacy):
    """The older name must not be the route that skips the token."""
    from telegram_mcp.tools.messages_state import press_inline_button

    client = _wire_legacy(
        _message([[_callback(text="Delete everything", data=b"DELETE")]]),
        answer=SimpleNamespace(message="gone", alert=None, url=None),
    )

    result = await press_inline_button(
        1, 7, button_index=0, button_text="Delete everything", account="default"
    )

    assert "press_token" in result
    assert client.calls == [], "the legacy name pressed without the binding"


@pytest.mark.asyncio
async def test_the_legacy_press_forwards_a_valid_binding(_wire_legacy):
    from telegram_mcp.tools.messages_state import list_inline_buttons, press_inline_button

    client = _wire_legacy(
        _message([[_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")]]),
        answer=SimpleNamespace(message="done", alert=None, url=None),
    )
    listing = json.loads(await list_inline_buttons(1, 7, account="default"))
    token = _tokens_of(listing)[1]

    await press_inline_button(
        1, 7, button_index=1, button_text="No", press_token=token, account="default"
    )

    assert client.calls[0].data == b"NO"
