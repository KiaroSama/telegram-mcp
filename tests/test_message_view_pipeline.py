"""End to end: upstream ``message_to_dict()``, then the fork's ``deep_message_dict()``.

A per-field test can pass while the real route still loses a joiner, because
upstream sanitizes first and the fork only ever sees what survived that. These
build a whole message and read the final dict. The per-field tests are in
``test_message_view.py``.
"""

from types import SimpleNamespace

import pytest

from helpers_unicode import FAMILY, PERSIAN
from telegram_mcp.message_view import deep_message_dict


def _plain_message(**overrides):
    """A stub carrying every attribute the upstream message_to_dict touches."""
    fields = dict(
        id=1,
        date=None,
        sender_id=5,
        out=False,
        message="hi",
        media=None,
        grouped_id=None,
        reply_to=None,
        fwd_from=None,
        forward=None,
        via_bot_id=None,
        edit_date=None,
        pinned=False,
        views=None,
        forwards=None,
        reactions=None,
        replies=None,
        buttons=None,
        entities=None,
        action=None,
        ttl_period=None,
        sender=None,
        web_preview=None,
        photo=None,
        sticker=None,
        voice=None,
        video_note=None,
        video=None,
        audio=None,
        gif=None,
        document=None,
        contact=None,
        geo=None,
        poll=None,
        file=None,
        chat=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _deep(msg):
    """The real path: upstream's compact view, then the fork's deep view."""
    from telegram_mcp.tools.messages import message_to_dict

    return deep_message_dict(msg, message_to_dict(msg))


@pytest.mark.parametrize("label, name", [("persian zwnj", PERSIAN), ("emoji zwj", FAMILY)])
def test_forward_names_survive_the_full_pipeline(label, name):
    """message_to_dict already stripped these; re-cleaning cannot bring them back."""
    msg = _plain_message(
        fwd_from=SimpleNamespace(date=None, from_name=name, channel_post=None, post_author=name),
        forward=SimpleNamespace(
            chat=SimpleNamespace(title=name, username="chan", first_name=None, last_name=None),
            chat_id=-100123,
            sender=SimpleNamespace(first_name=name, last_name=None),
        ),
    )

    forwarded = _deep(msg)["forwarded"]

    for key in ("from_name", "from_chat", "from_user", "post_author"):
        assert forwarded[key] == name, f"{label}: {key} lost its joiner"
    # Fields upstream computed must survive untouched.
    assert forwarded["from_username"] == "chan"
    assert forwarded["from_chat_id"] == -100123


@pytest.mark.parametrize("label, name", [("persian zwnj", PERSIAN), ("emoji zwj", FAMILY)])
def test_user_sender_name_survives_the_full_pipeline(label, name):
    msg = _plain_message(
        sender=SimpleNamespace(first_name=name, last_name=None, username=None, title=None)
    )
    assert _deep(msg)["sender"] == name, label


@pytest.mark.parametrize("label, title", [("persian zwnj", PERSIAN), ("emoji zwj", FAMILY)])
def test_channel_sender_title_survives_the_full_pipeline(label, title):
    msg = _plain_message(
        sender=SimpleNamespace(title=title, username=None, first_name=None, last_name=None)
    )
    assert _deep(msg)["sender"] == title, label


def test_buttons_made_entirely_of_hostile_characters_never_leak():
    """Cleaning them yields an empty list, which must not fall back to the raw one."""
    msg = _plain_message(
        buttons=[
            [SimpleNamespace(text="\u202e\u200b"), SimpleNamespace(text="\u2062\ufff9")],
            [SimpleNamespace(text="\u180e")],
        ]
    )

    result = _deep(msg)

    assert "buttons" not in result, f"raw labels leaked: {result.get('buttons')!r}"


def test_partly_hostile_buttons_keep_only_the_readable_ones():
    msg = _plain_message(
        buttons=[[SimpleNamespace(text="\u202e\u200b"), SimpleNamespace(text=PERSIAN)]]
    )
    assert _deep(msg)["buttons"] == [PERSIAN]


def test_a_buttons_property_that_raises_does_not_sink_the_whole_message():
    """Message.buttons is a Telethon property that builds MessageButton objects and
    touches input_chat; getattr's default only swallows AttributeError."""

    class _Exploding(SimpleNamespace):
        @property
        def buttons(self):
            raise TypeError("input_chat is unavailable for this message")

    # buttons is a data descriptor on the class, so it must not be passed to
    # __init__ \u2014 SimpleNamespace would try to assign through the property.
    fields = {k: v for k, v in vars(_plain_message()).items() if k != "buttons"}

    data = _deep(_Exploding(**fields))

    assert data["id"] == 1
    assert "buttons" not in data


def test_reply_quote_declares_when_it_was_modified():
    hostile = PERSIAN + "\u202e"
    msg = _plain_message(
        reply_to=SimpleNamespace(quote_text=hostile, quote_offset=12, forum_topic=False)
    )

    quote = _deep(msg)["reply_quote"]

    assert quote["modified"] is True
    assert quote["truncated"] is False
    assert "NOT character-for-character exact" in quote["note"]
    assert quote["offset"] == 12
    assert "UTF-16" in quote["note"]


def test_reply_quote_claims_no_change_when_nothing_changed():
    msg = _plain_message(
        reply_to=SimpleNamespace(quote_text=PERSIAN, quote_offset=3, forum_topic=False)
    )

    quote = _deep(msg)["reply_quote"]

    assert "modified" not in quote
    assert quote["text"] == PERSIAN
    assert "unchanged from what Telegram reported" in quote["note"]


def test_text_fidelity_does_not_claim_byte_exactness():
    raw = PERSIAN + "\u200b"
    msg = _plain_message(message=raw)

    data = _deep(msg)

    assert "Character-accurate" not in data["text_fidelity_note"]
    assert "Fidelity-safe" in data["text_fidelity_note"]
    assert data["text_fidelity_modified"] is True


def test_text_fidelity_reports_an_untouched_message_as_unmodified():
    msg = _plain_message(
        message=PERSIAN,
        sender=SimpleNamespace(first_name="x", last_name=None, username=None, title=None),
    )

    data = _deep(msg)

    assert data["text_fidelity_modified"] is False


# --- Message-level effects ---------------------------------------------------


def test_a_message_level_effect_is_reported_separately_from_the_sticker_one():
    """Telegram can play both at once; the structured view must show both."""
    msg = _plain_message(effect=5104841245755180586)

    effect = _deep(msg)["message_effect"]

    assert effect["effect_id"] == 5104841245755180586
    assert effect["kind"] == "message_effect"
    assert "distinct from a premium sticker" in effect["note"]
    # The ID used to be a dead end pointing at a raw API call; it now names the
    # tool that resolves it, and still defers the composite to the capture route.
    assert "get_message_effect" in effect["note"]
    assert "get_telegram_frames" in effect["note"]


def test_no_message_effect_key_without_one():
    assert "message_effect" not in _deep(_plain_message())
