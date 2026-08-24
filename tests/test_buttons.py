"""Inline-keyboard inspection and pressing.

The fakes mirror Telethon's real shapes: every ``KeyboardButton*`` class carries
``text`` and ``style`` and nothing else in common, only ``KeyboardButtonCallback``
carries ``data``, and no button type carries entities.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.button_view import describe_button, describe_keyboard, describe_style
import telegram_mcp.tools.buttons as buttons_tool
from telegram_mcp.tools.buttons import click_button, inspect_buttons


class _Style:
    def __init__(self, bg_primary=None, bg_danger=None, bg_success=None, icon=None):
        self.bg_primary, self.bg_danger = bg_primary, bg_danger
        self.bg_success, self.icon = bg_success, icon


def _button(cls_name, **fields):
    """A button whose class NAME drives the description, as in Telethon."""
    fields.setdefault("style", None)
    return type(cls_name, (SimpleNamespace,), {})(**fields)


def _callback(text="Confirm", data=b"cb:1", **kw):
    return _button("KeyboardButtonCallback", text=text, data=data, **kw)


def _message(rows, message_id=7, inline=True):
    """A message with a keyboard. `inline` picks glass vs reply — Telethon
    distinguishes them by the markup CLASS, and both fill `rows`."""
    markup_cls = "ReplyInlineMarkup" if inline else "ReplyKeyboardMarkup"
    markup = type(markup_cls, (SimpleNamespace,), {})(
        rows=[SimpleNamespace(buttons=r) for r in rows]
    )
    return SimpleNamespace(id=message_id, reply_markup=markup)


def _buttons_of(msg):
    """The flat button list, for tests that do not care about the keyboard kind."""
    return describe_keyboard(msg)["buttons"]


# Well-formed and worthless: it authorizes nothing. Tests that assert a press is
# refused for some OTHER reason pass this, so they also pin the order — a more
# specific refusal must win over "that token does not authorize this button".
_UNUSABLE_TOKEN = "b1:" + "0" * 32


# --- what a button is -------------------------------------------------------


def test_a_callback_button_is_pressable_and_a_url_button_is_not():
    keyboard = _buttons_of(
        _message(
            [[_callback(), _button("KeyboardButtonUrl", text="Open", url="https://e.example")]]
        )
    )

    assert [b["kind"] for b in keyboard] == ["callback", "url"]
    assert [b["pressable"] for b in keyboard] == [True, False]
    assert keyboard[1]["url"] == "https://e.example"
    assert "press_note" in keyboard[1]


def test_a_mini_app_button_says_it_cannot_be_pressed_and_names_the_capture_route():
    """A WebView opens a Mini App; there is no callback to answer."""
    keyboard = _buttons_of(
        _message([[_button("KeyboardButtonWebView", text="Play", url="https://app.example")]])
    )

    assert keyboard[0]["pressable"] is False
    assert "get_telegram_frames" in keyboard[0]["press_note"]


def test_a_password_gated_callback_is_refused_rather_than_attempted():
    keyboard = _buttons_of(_message([[_callback(requires_password=True)]]))

    assert keyboard[0]["requires_password"] is True
    assert keyboard[0]["pressable"] is False
    assert "2FA" in keyboard[0]["press_note"]


def test_indexes_are_flat_and_row_major_across_rows():
    """click_button takes this index, so its meaning must not depend on layout."""
    keyboard = _buttons_of(
        _message([[_callback(text="a"), _callback(text="b")], [_callback(text="c")]])
    )

    assert [(b["index"], b["row"], b["column"]) for b in keyboard] == [
        (0, 0, 0),
        (1, 0, 1),
        (2, 1, 0),
    ]


def test_a_message_without_a_keyboard_is_none_not_empty():
    """None means "no keyboard"; [] would mean "a keyboard whose buttons vanished"."""
    assert describe_keyboard(SimpleNamespace(id=1, reply_markup=None)) is None


# --- the label is a security surface ---------------------------------------


def test_a_bidi_override_in_a_label_is_stripped_and_flagged():
    """The label is what an agent reads to choose; a raw one can read as another."""
    keyboard = _buttons_of(_message([[_callback(text="Cancel‮Delete")]]))

    assert "‮" not in keyboard[0]["text"], "the override survived into the label"
    assert keyboard[0]["text_altered"] is True


def test_a_persian_or_emoji_label_survives_intact_and_is_not_flagged():
    label = "می‌کند \U0001f468‍\U0001f469‍\U0001f467"
    keyboard = _buttons_of(_message([[_callback(text=label)]]))

    assert keyboard[0]["text"] == label
    assert "text_altered" not in keyboard[0]


# --- style and the premium-emoji question ----------------------------------


def test_a_styled_button_reports_its_background_and_icon_document_id():
    styled = _callback(style=_Style(bg_danger=True, icon=5107584321108051014))
    described = describe_button(styled, 0, 0, 0)

    assert described["style"]["background"] == "danger"
    assert described["style"]["icon_document_id"] == 5107584321108051014
    assert "get_custom_emoji" in described["style"]["icon_note"]


def test_an_unstyled_button_reports_no_style_key():
    assert describe_style(_callback()) is None
    assert "style" not in describe_button(_callback(), 0, 0, 0)


def test_a_style_with_only_an_icon_still_reports_it():
    described = describe_style(_callback(style=_Style(icon=42)))
    assert described == {"icon_document_id": 42, "icon_note": described["icon_note"]}
    assert "background" not in described


# --- the tools --------------------------------------------------------------


class _Client:
    def __init__(self, msg, answer=None):
        self._msg, self._answer = msg, answer
        self.calls = []

    async def get_messages(self, entity, ids=None):
        return self._msg

    async def __call__(self, request):
        self.calls.append(request)
        return self._answer


@pytest.fixture
def _wire(monkeypatch):
    def use(msg, answer=None):
        client = _Client(msg, answer)
        monkeypatch.setattr(buttons_tool, "get_client", lambda account=None: client)

        async def _connect(cl):
            return None

        async def _resolve(chat_id, cl):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(buttons_tool, "ensure_connected", _connect)
        monkeypatch.setattr(buttons_tool, "resolve_entity", _resolve)
        return client

    return use


@pytest.mark.asyncio
async def test_inspect_buttons_publishes_the_pressable_indexes(_wire):
    _wire(_message([[_callback(), _button("KeyboardButtonUrl", text="Open", url="u")]]))

    payload = json.loads(await inspect_buttons(1, 7, account="default"))

    assert payload["pressable_indexes"] == [0]
    assert payload["button_count"] == 2
    assert "cannot be resolved" in payload["premium_emoji"]


@pytest.mark.asyncio
async def test_clicking_a_callback_button_sends_that_buttons_payload(_wire):
    """The payload must come from the button at the index, not from a label match."""
    rows = [[_callback(text="Yes", data=b"YES"), _callback(text="No", data=b"NO")]]
    client = _wire(_message(rows), answer=SimpleNamespace(message="done", alert=None, url=None))

    token = _tokens_of(await _inspect())[1]
    payload = json.loads(
        await click_button(1, 7, 1, expect_text="No", press_token=token, account="default")
    )

    assert client.calls[0].data == b"NO", "pressed the wrong button"
    assert payload["results"][0]["button_index"] == 1
    assert payload["results"][0]["bot_message"] == "done"


@pytest.mark.asyncio
async def test_clicking_a_non_callback_button_is_refused_without_a_request(_wire):
    client = _wire(_message([[_button("KeyboardButtonUrl", text="Open", url="u")]]))

    result = await click_button(
        1, 7, 0, expect_text="Open", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert isinstance(result, str) and "not a callback button" in result
    assert client.calls == [], "a request was sent for a button that cannot be pressed"


@pytest.mark.asyncio
async def test_clicking_an_out_of_range_index_names_the_valid_range(_wire):
    client = _wire(_message([[_callback()]]))

    result = await click_button(
        1, 7, 5, expect_text="Confirm", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert "no button 5" in result and "0-0" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_a_password_gated_button_is_never_pressed(_wire):
    client = _wire(_message([[_callback(requires_password=True)]]))

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert "2FA" in result or "password" in result
    assert client.calls == [], "a 2FA-gated callback was attempted"


@pytest.mark.asyncio
async def test_a_silent_answer_is_reported_as_delivered_not_as_empty(_wire):
    _wire(
        _message([[_callback()]]),
        answer=SimpleNamespace(message=None, alert=None, url=None),
    )

    token = _tokens_of(await _inspect())[0]
    payload = json.loads(
        await click_button(1, 7, 0, expect_text="Confirm", press_token=token, account="default")
    )

    assert payload["results"][0]["bot_message"] is None
    assert "delivered" in payload["results"][0]["note_no_answer"]


# --- glass vs reply keyboard: both fill reply_markup.rows -------------------


def test_a_reply_keyboard_is_not_reported_as_glass():
    """Found live: the first real keyboard sampled was a reply keyboard, and
    an earlier version of this module listed its buttons as glass ones."""
    keyboard = describe_keyboard(_message([[_callback()]], inline=False))

    assert keyboard["keyboard_type"] == "reply"
    assert keyboard["is_glass"] is False


def test_an_inline_keyboard_is_reported_as_glass():
    keyboard = describe_keyboard(_message([[_callback()]]))

    assert keyboard["keyboard_type"] == "inline"
    assert keyboard["is_glass"] is True


@pytest.mark.asyncio
async def test_inspect_buttons_names_a_reply_keyboard_for_what_it_is(_wire):
    _wire(_message([[_callback()]], inline=False))

    payload = json.loads(await inspect_buttons(1, 7, account="default"))

    assert payload["keyboard_type"] == "reply"
    assert "REPLY keyboard" in payload["keyboard_note"]
    assert "send_message" in payload["keyboard_note"]


@pytest.mark.asyncio
async def test_clicking_a_reply_keyboard_button_is_refused_without_a_request(_wire):
    """Its buttons have callback-shaped fakes here, so only the markup type saves us."""
    client = _wire(_message([[_callback()]], inline=False))

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert "REPLY keyboard" in result and "send_message" in result
    assert client.calls == [], "a callback was sent for a reply-keyboard button"


# --- an index is a position, not an identity -------------------------------


@pytest.mark.asyncio
async def test_a_changed_keyboard_refuses_the_press_instead_of_hitting_the_wrong_button(_wire):
    """A bot can edit its own keyboard between the listing and the press."""
    client = _wire(_message([[_callback(text="Delete", data=b"DEL")]]))

    result = await click_button(
        1, 7, 0, expect_text="Confirm", press_token=_UNUSABLE_TOKEN, account="default"
    )

    assert "now reads 'Delete'" in result and "nothing was pressed" in result
    assert client.calls == [], "pressed a button whose label had changed"


@pytest.mark.asyncio
async def test_a_matching_expectation_presses_normally(_wire):
    client = _wire(
        _message([[_callback(text="Confirm", data=b"OK")]]),
        answer=SimpleNamespace(message="ok", alert=None, url=None),
    )

    token = _tokens_of(await _inspect())[0]
    payload = json.loads(
        await click_button(1, 7, 0, expect_text="Confirm", press_token=token, account="default")
    )

    assert client.calls[0].data == b"OK"
    assert payload["results"][0]["button_text"] == "Confirm"


# --- resolving what the icon emoji actually is ------------------------------


class _IconClient(_Client):
    """Answers GetCustomEmojiDocuments, the call a real client makes before drawing."""

    def __init__(self, msg, documents=None, raises=False):
        super().__init__(msg)
        self._documents = documents or []
        self._raises = raises

    async def __call__(self, request):
        self.calls.append(request)
        if self._raises:
            raise RuntimeError("emoji service unavailable")
        return self._documents


def _emoji_doc(doc_id, mime="application/x-tgsticker", alt="🔥"):
    return SimpleNamespace(
        id=doc_id,
        mime_type=mime,
        attributes=[SimpleNamespace(alt=alt)],
    )


@pytest.fixture
def _wire_icons(monkeypatch):
    def use(msg, documents=None, raises=False):
        client = _IconClient(msg, documents, raises)
        monkeypatch.setattr(buttons_tool, "get_client", lambda account=None: client)

        async def _connect(cl):
            return None

        async def _resolve(chat_id, cl):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(buttons_tool, "ensure_connected", _connect)
        monkeypatch.setattr(buttons_tool, "resolve_entity", _resolve)
        return client

    return use


@pytest.mark.asyncio
async def test_a_styled_buttons_icon_is_resolved_to_the_emoji_it_shows(_wire_icons):
    """The number in style.icon is a document id — the same one a client resolves."""
    styled = _callback(style=_Style(bg_primary=True, icon=555))
    client = _wire_icons(_message([[styled]]), documents=[_emoji_doc(555, alt="🎉")])

    payload = json.loads(await inspect_buttons(1, 7, account="default"))
    style = payload["results"][0]["style"]

    assert style["alt"] == "🎉", "the fallback glyph was not resolved"
    assert style["animated"] is True
    assert style["icon_document_id"] == 555
    assert len(client.calls) == 1, "one request should cover the whole keyboard"


@pytest.mark.asyncio
async def test_one_request_covers_every_icon_on_the_keyboard(_wire_icons):
    rows = [[_callback(text="a", style=_Style(icon=1)), _callback(text="b", style=_Style(icon=2))]]
    client = _wire_icons(
        _message(rows), documents=[_emoji_doc(1, alt="A"), _emoji_doc(2, alt="B")]
    )

    payload = json.loads(await inspect_buttons(1, 7, account="default"))

    assert [b["style"]["alt"] for b in payload["results"]] == ["A", "B"]
    assert len(client.calls) == 1
    assert sorted(client.calls[0].document_id) == [1, 2]


@pytest.mark.asyncio
async def test_an_id_telegram_will_not_resolve_is_reported_not_invented(_wire_icons):
    """An id that resolves to nothing was not a custom emoji after all."""
    _wire_icons(_message([[_callback(style=_Style(icon=999))]]), documents=[])

    payload = json.loads(await inspect_buttons(1, 7, account="default"))
    style = payload["results"][0]["style"]

    assert "alt" not in style
    assert "no document" in style["icon_error"]


@pytest.mark.asyncio
async def test_a_failed_icon_lookup_does_not_cost_the_listing(_wire_icons):
    _wire_icons(_message([[_callback(style=_Style(icon=7))]]), raises=True)

    payload = json.loads(await inspect_buttons(1, 7, account="default"))

    assert payload["button_count"] == 1, "the listing was lost to an icon lookup"
    assert "could not resolve" in payload["results"][0]["style"]["icon_error"]


@pytest.mark.asyncio
async def test_resolve_icons_false_sends_no_extra_request(_wire_icons):
    client = _wire_icons(_message([[_callback(style=_Style(icon=7))]]), documents=[_emoji_doc(7)])

    payload = json.loads(await inspect_buttons(1, 7, resolve_icons=False, account="default"))

    assert client.calls == [], "an extra round trip was made despite resolve_icons=False"
    assert payload["results"][0]["style"]["icon_document_id"] == 7
    assert "alt" not in payload["results"][0]["style"]


@pytest.mark.asyncio
async def test_a_keyboard_with_no_styled_button_makes_no_lookup(_wire_icons):
    client = _wire_icons(_message([[_callback()]]))

    await inspect_buttons(1, 7, account="default")

    assert client.calls == [], "a lookup was made for a keyboard with no icons"


# --- values that are machine data, not prose --------------------------------


def test_a_request_peer_button_does_not_break_the_whole_listing():
    """`peer_type` is a TLObject, the only non-scalar among the copied fields.

    Left as itself it reached json.dumps and raised TypeError, which failed the
    entire listing — every other button on that keyboard included.
    """
    keyboard = _buttons_of(
        _message(
            [
                [
                    _callback(text="Confirm"),
                    _button(
                        "KeyboardButtonRequestPeer",
                        text="Choose a group",
                        button_id=1,
                        peer_type=type("RequestPeerTypeChat", (SimpleNamespace,), {})(),
                    ),
                ]
            ]
        )
    )

    json.dumps(keyboard)  # the contract every inspect_buttons test relies on
    assert keyboard[1]["peer_type"] == "RequestPeerTypeChat"
    assert keyboard[0]["text"] == "Confirm", "an unrelated button was lost"


def test_a_long_url_is_reported_whole_rather_than_ellipsised():
    """A Mini App start param routinely runs past display_name's prose default."""
    url = "https://app.example/webapp?tgWebAppStartParam=" + "s" * 260

    described = describe_button(_button("KeyboardButtonWebView", text="Open", url=url), 0, 0, 0)

    assert described["url"] == url
    assert not described["url"].endswith("\u2026")
    assert "url_altered" not in described


def test_a_url_carrying_a_direction_override_is_cleaned_and_flagged():
    """Sanitizing still applies — a bidi override in a URL is the same spoof."""
    described = describe_button(
        _button("KeyboardButtonUrl", text="Open", url="https://e.example/\u202egpj.exe"),
        0,
        0,
        0,
    )

    assert "\u202e" not in described["url"]
    assert described["url_altered"] is True, "a changed URL was asserted as the real one"


# --- the legacy pair must not be a second, weaker way in --------------------


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


# --- an index is a position; the label is the identity ---------------------


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


# --- a label is not an identity: the press token ---------------------------
#
# expect_text compares the SANITIZED label of whatever now sits at the index. A
# bot keeps the label and swaps the callback data and the guard sees nothing; two
# different raw labels normalize to one display string and the guard cannot tell
# them apart either. These press against an authenticated binding minted by the
# listing instead.


def _tokens_of(payload):
    """index -> press_token, from an inspect_buttons payload."""
    return {b["index"]: b.get("press_token") for b in payload["results"]}


async def _inspect(chat_id=1, message_id=7, account="default"):
    return json.loads(await inspect_buttons(chat_id, message_id, account=account))


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
