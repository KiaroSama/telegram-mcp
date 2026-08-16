"""Structured view of a message's inline keyboard — the "glass buttons".

An agent that presses a button decides which one from its label, so the label is
a security surface: upstream hands it back raw, and a raw label can carry a bidi
override that makes it read as something else entirely. Everything here goes
through :func:`display_name`, and says so when the raw text differed.

Two facts about Telegram's data model shape this module:

* **A button label carries no entities.** Every ``KeyboardButton*`` type has a
  plain ``text: str`` and no ``entities`` field, so a premium/custom emoji inside
  a label arrives as its fallback glyph with no ``document_id`` — unresolvable by
  design, not by omission. Verified against the TL schema.
* **Every button type carries ``style``**, and its ``icon`` is a custom-emoji
  document ID. Confirmed live: @EVdlcbot's keyboard carries two styled buttons
  whose icons resolve to animated ``.tgs`` emoji, beside a third with no style.

Kept free of MCP so the description rules are testable without a client.
"""

from typing import Any, Optional

from telegram_mcp.message_view import display_name

# What each button class means for a caller, and whether a callback press can
# reach it. Only a button carrying callback ``data`` answers a press; the rest
# are actions Telegram performs in the client, and saying "pressed" of them
# would be a lie.
_BUTTON_KINDS: dict[str, tuple[str, bool]] = {
    "KeyboardButtonCallback": ("callback", True),
    "KeyboardButtonUrl": ("url", False),
    "KeyboardButtonUrlAuth": ("url_auth", False),
    "KeyboardButtonWebView": ("webview", False),
    "KeyboardButtonSimpleWebView": ("webview", False),
    "KeyboardButtonSwitchInline": ("switch_inline", False),
    "KeyboardButtonUserProfile": ("user_profile", False),
    "KeyboardButtonCopy": ("copy", False),
    "KeyboardButtonBuy": ("buy", False),
    "KeyboardButtonGame": ("game", False),
    "KeyboardButtonRequestPhone": ("request_phone", False),
    "KeyboardButtonRequestGeoLocation": ("request_geo", False),
    "KeyboardButtonRequestPoll": ("request_poll", False),
    "KeyboardButtonRequestPeer": ("request_peer", False),
    "KeyboardButton": ("plain", False),
}

_NOT_PRESSABLE = {
    "url": "Opens a link. The URL is reported; nothing here follows it.",
    "url_auth": "Opens a link that would log the account in to a third-party site.",
    "webview": "Opens a Mini App inside Telegram. There is no callback to answer, so "
    "it cannot be pressed from the API — capture it with get_telegram_frames instead.",
    "switch_inline": "Switches the composer to an inline query in some chat.",
    "user_profile": "Opens a user profile.",
    "copy": "Copies text to the clipboard in the client.",
    "buy": "Starts a payment flow.",
    "game": "Launches a game.",
    "request_phone": "Asks the user to share their phone number.",
    "request_geo": "Asks the user to share their location.",
    "request_poll": "Opens the poll composer.",
    "request_peer": "Asks the user to choose a chat or user to share.",
    "plain": "A reply-keyboard button: it sends its own text as a message rather "
    "than answering a callback.",
}

# Copied onto every listing so a caller cannot mistake a label for the picture.
PREMIUM_EMOJI_NOTE = (
    "A premium or custom emoji inside a button LABEL cannot be resolved: no "
    "KeyboardButton type carries entities, so only the fallback glyph reaches the API. "
    "A styled button's own icon is different - it arrives as 'icon_document_id' below, "
    "already resolved to its glyph and mime type. get_custom_emoji returns the picture; "
    "ask for more than one frame, or the still you get back will not look animated."
)


def describe_style(button) -> Optional[dict[str, Any]]:
    """Background colour and icon of a styled button, or ``None``.

    ``icon`` is a custom-emoji document id — the same id a Telegram client
    resolves before drawing the button. ``inspect_buttons`` resolves it too; here
    it is only reported, because this module stays free of the network.
    """
    style = getattr(button, "style", None)
    if style is None:
        return None

    described: dict[str, Any] = {}
    for flag, name in (
        ("bg_primary", "primary"),
        ("bg_danger", "danger"),
        ("bg_success", "success"),
    ):
        if getattr(style, flag, None):
            described["background"] = name
            break

    icon = getattr(style, "icon", None)
    if icon:
        described["icon_document_id"] = icon
        described["icon_note"] = (
            "A custom emoji document id. inspect_buttons resolves it to the fallback glyph "
            "and mime type; get_custom_emoji returns the picture, and a count above 1 "
            "returns frames of the animation rather than one still."
        )
    return described or None


def describe_button(button, index: int, row: int, column: int) -> dict[str, Any]:
    """One button: what it is, whether a press can reach it, and its real label."""
    kind, pressable = _BUTTON_KINDS.get(type(button).__name__, ("unknown", False))

    raw = getattr(button, "text", None) or ""
    text = display_name(raw)
    described: dict[str, Any] = {
        "index": index,
        "row": row,
        "column": column,
        "kind": kind,
        "text": text,
        "pressable": pressable,
    }
    if text != raw:
        # The agent chooses a button by reading this label. If cleaning changed
        # it, the raw one was carrying something that does not render as itself.
        described["text_altered"] = True

    if kind == "callback":
        # The callback payload is opaque bot state and can encode anything; the
        # index is the safe handle, so the payload is reported as present only.
        described["has_callback_data"] = bool(getattr(button, "data", None))
        if getattr(button, "requires_password", None):
            described["requires_password"] = True
            described["pressable"] = False
            described["press_note"] = (
                "Telegram requires the account's 2FA password to answer this callback, "
                "which this server will not supply."
            )
    else:
        described["press_note"] = _NOT_PRESSABLE.get(kind, "Not a callback button.")

    for attribute, key in (
        ("url", "url"),
        ("copy_text", "copy_text"),
        ("query", "query"),
        ("user_id", "user_id"),
        ("peer_type", "peer_type"),
        ("button_id", "button_id"),
    ):
        value = getattr(button, attribute, None)
        if value is not None:
            described[key] = display_name(value) if isinstance(value, str) else value

    style = describe_style(button)
    if style:
        described["style"] = style
    return described


def describe_keyboard(msg) -> Optional[dict[str, Any]]:
    """A message's keyboard: which kind it is, and its buttons with stable indexes.

    ``reply_markup.rows`` is populated for **both** kinds, which is a trap worth
    naming: ``ReplyInlineMarkup`` is the "glass" keyboard attached under a
    message and is the only one a callback can reach, while
    ``ReplyKeyboardMarkup`` replaces the user's on-screen keyboard and its
    buttons send their own text as a new message. Found live on the first real
    keyboard sampled — reporting the second kind as glass buttons would be a
    plain falsehood.

    The index is what ``click_button`` takes. Selecting by label instead is what
    makes a spoofed label dangerous: two buttons can render identically while
    only one is the one the agent meant.

    Returns ``None`` when the message has no keyboard — distinct from an empty
    ``buttons`` list, which would mean a keyboard whose buttons all vanished.
    """
    markup = getattr(msg, "reply_markup", None)
    rows = getattr(markup, "rows", None)
    if not rows:
        return None

    described: list[dict[str, Any]] = []
    index = 0
    for row_number, row in enumerate(rows):
        for column, button in enumerate(getattr(row, "buttons", None) or []):
            described.append(describe_button(button, index, row_number, column))
            index += 1

    inline = type(markup).__name__ == "ReplyInlineMarkup"
    return {
        "keyboard_type": "inline" if inline else "reply",
        "is_glass": inline,
        "buttons": described,
    }


def find_button(buttons: list[dict[str, Any]], index: int) -> Optional[dict[str, Any]]:
    """The described button at ``index``, or ``None``."""
    return next((b for b in buttons if b["index"] == index), None)
