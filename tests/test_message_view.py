"""Unit tests for the deep structured message view.

Every message here is a stub: the module reads plain attributes off the Telethon
object and never calls the API, so no client and no network are involved.
"""

from types import SimpleNamespace

import pytest

from sanitize import sanitize_name, sanitize_user_content
from telegram_mcp.message_view import (
    deep_message_dict,
    fidelity_text,
    describe_custom_emoji,
    describe_entities,
    describe_media,
    describe_reactions,
    describe_topic,
    _fidelity_forward,
    describe_buttons,
    describe_reply_quote,
    fidelity_sender_name,
    display_name,
    message_permalink,
)

# "🎉" is one Python character but two UTF-16 code units, so every offset after
# it differs from its Python index. "bold" starts at UTF-16 offset 9, index 8.
EMOJI_TEXT = "🎉 hello bold world"


def _typed(class_name, **fields):
    """Stub object whose class name is what the module inspects."""
    return type(class_name, (), fields)()


def _entity(kind, **fields):
    return _typed(f"MessageEntity{kind}", **fields)


def test_entity_offsets_are_utf16_code_units():
    msg = SimpleNamespace(
        message=EMOJI_TEXT,
        entities=[
            _entity("Bold", offset=9, length=4),
            _entity("Italic", offset=0, length=2),
        ],
    )

    described = describe_entities(msg)

    assert [item["text"] for item in described] == ["bold", "🎉"]
    assert EMOJI_TEXT[9 : 9 + 4] != "bold"  # a naive Python slice would be wrong


def test_entity_kind_naming_and_passthrough_fields():
    msg = SimpleNamespace(
        message="link code spoiler name",
        entities=[
            _entity("TextUrl", offset=0, length=4, url="https://example.com"),
            _entity("Pre", offset=5, length=4, language="python"),
            _entity("MentionName", offset=15, length=4, user_id=99),
            _entity("Blockquote", offset=0, length=4, collapsed=True),
            _entity("CustomEmoji", offset=10, length=4, document_id=555),
        ],
    )

    described = describe_entities(msg)

    assert [item["type"] for item in described] == [
        "text_url",
        "pre",
        "mention_name",
        "blockquote",
        "custom_emoji",
    ]
    assert described[0]["url"] == "https://example.com"
    assert described[1]["language"] == "python"
    assert described[2]["user_id"] == 99
    assert described[3]["collapsed"] is True
    assert described[4]["custom_emoji_id"] == 555


def test_custom_emoji_lists_only_custom_emoji_entities():
    msg = SimpleNamespace(
        message="🎉 party",
        entities=[
            _entity("CustomEmoji", offset=0, length=2, document_id=12345),
            _entity("Bold", offset=3, length=5),
        ],
    )

    assert describe_custom_emoji(msg) == [{"document_id": 12345, "placeholder": "🎉", "offset": 0}]


def test_user_controlled_text_and_filenames_stay_sanitized():
    """Message text and file names are attacker-controlled. Without this, the
    offset assertions above still pass with the sanitizer calls removed."""
    msg = SimpleNamespace(
        message="bo\x07ld\u200b hello",
        entities=[_entity("Bold", offset=0, length=6)],
        media=_typed("MessageMediaDocument"),
        file=SimpleNamespace(name="cl\x07ip\u202e.mp4"),
    )

    assert describe_entities(msg)[0]["text"] == "bold"
    assert describe_media(msg)["file_name"] == "clip.mp4"


def test_reactions_report_totals_chosen_flag_and_custom_emoji():
    msg = SimpleNamespace(
        reactions=SimpleNamespace(
            results=[
                SimpleNamespace(count=3, reaction=SimpleNamespace(emoticon="👍")),
                # chosen_order 0 is falsy but still means "this account reacted".
                SimpleNamespace(
                    count=5, reaction=SimpleNamespace(document_id=777), chosen_order=0
                ),
            ],
            can_see_list=True,
        )
    )

    assert describe_reactions(msg) == {
        "total": 8,
        "items": [
            {"count": 3, "emoji": "👍"},
            {"count": 5, "custom_emoji_id": 777, "chosen": True},
        ],
        "can_see_list": True,
    }


@pytest.mark.parametrize("reactions", [None, SimpleNamespace(results=[])])
def test_reactions_absent_or_empty_return_none(reactions):
    assert describe_reactions(SimpleNamespace(reactions=reactions)) is None


def test_media_absent_returns_none():
    assert describe_media(SimpleNamespace(media=None)) is None


def test_media_maps_file_metadata_thumbnails_and_downloadable():
    document = _typed(
        "Document",
        id=777,
        attributes=[_typed("DocumentAttributeVideo"), _typed("DocumentAttributeFilename")],
        thumbs=[_typed("PhotoSize", type="m", w=320, h=180, size=1024)],
    )
    msg = SimpleNamespace(
        media=_typed("MessageMediaDocument"),
        video=document,
        document=document,
        file=SimpleNamespace(
            name="clip.mp4",
            mime_type="video/mp4",
            size=2048,
            width=1280,
            height=720,
            duration=12.5,
        ),
    )

    info = describe_media(msg)

    assert info["telegram_type"] == "MessageMediaDocument"
    assert info["kind"] == "video"
    assert info["file_name"] == "clip.mp4"
    assert info["mime_type"] == "video/mp4"
    assert info["size_bytes"] == 2048
    assert (info["width"], info["height"]) == (1280, 720)
    assert info["duration_seconds"] == 12.5
    assert info["document_id"] == 777
    assert info["attributes"] == ["DocumentAttributeVideo", "DocumentAttributeFilename"]
    assert info["downloadable"] is True
    assert info["has_thumbnail"] is True
    assert info["thumbnails"] == [
        {
            "thumb_index": 0,
            "type": "m",
            "width": 320,
            "height": 180,
            "bytes": 1024,
            "kind": "PhotoSize",
        }
    ]


@pytest.mark.parametrize(
    ("mime_type", "animation_format"),
    [("application/x-tgsticker", "lottie_tgs"), ("video/webm", "video_webm")],
)
def test_media_kind_prefers_sticker_and_names_the_animation_format(mime_type, animation_format):
    document = _typed(
        "Document",
        id=42,
        attributes=[
            _typed(
                "DocumentAttributeSticker", stickerset=SimpleNamespace(short_name="AgenticPack")
            ),
            _typed("DocumentAttributeAnimated"),
        ],
    )
    msg = SimpleNamespace(
        media=_typed("MessageMediaDocument"),
        sticker=document,
        document=document,
        file=SimpleNamespace(mime_type=mime_type, size=64),
    )

    info = describe_media(msg)

    assert info["kind"] == "sticker"
    assert info["sticker_set"] == "AgenticPack"
    assert info["animated"] is True
    assert info["animation_format"] == animation_format


def test_topic_only_for_forum_replies():
    assert describe_topic(SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id=7))) is None
    assert describe_topic(
        SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=99))
    ) == {"is_topic_message": True, "topic_id": 99}


@pytest.mark.parametrize(
    ("chat", "expected"),
    [
        (SimpleNamespace(username="agenticai"), "https://t.me/agenticai/55"),
        (
            SimpleNamespace(username=None, id=1234567890, megagroup=True),
            "https://t.me/c/1234567890/55",
        ),
        (
            SimpleNamespace(username=None, id=1234567890, broadcast=True),
            "https://t.me/c/1234567890/55",
        ),
        (SimpleNamespace(username=None, id=42), None),
        (None, None),
    ],
)
def test_message_permalink_forms(chat, expected):
    assert message_permalink(SimpleNamespace(id=55, chat=None), chat=chat) == expected


def test_deep_message_dict_preserves_base_and_adds_detail():
    base = {"id": 55, "text": "hello bold", "sender": "Jane", "media": "video"}
    msg = SimpleNamespace(
        id=55,
        message="hello bold",
        entities=[_entity("Bold", offset=6, length=4)],
        reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=99),
        noforwards=True,
    )

    data = deep_message_dict(msg, base, chat=SimpleNamespace(username="agenticai"))

    assert base == {"id": 55, "text": "hello bold", "sender": "Jane", "media": "video"}
    assert data.items() >= base.items()
    assert data["entities"] == [{"type": "bold", "offset": 6, "length": 4, "text": "bold"}]
    assert data["topic"] == {"is_topic_message": True, "topic_id": 99}
    assert data["permalink"] == "https://t.me/agenticai/55"
    assert data["protected"] is True


# --- Text fidelity and entity-offset regressions -----------------------------


def _utf16_slice(text, offset, length):
    return text.encode("utf-16-le")[offset * 2 : (offset + length) * 2].decode("utf-16-le")


@pytest.mark.parametrize(
    "label, text",
    [
        ("persian zwnj", "\u0645\u06cc\u200c\u06a9\u0646\u062f"),
        ("emoji zwj family", "\U0001f468\u200d\U0001f469\u200d\U0001f467"),
        ("rlm bidi mark", "abc\u200f\u062f\u0065f"),
        ("lrm bidi mark", "abc\u200e def"),
        ("repeated newlines", "a\n\n\n\nb"),
        ("tabs", "a\tb"),
    ],
)
def test_fidelity_text_preserves_legitimate_unicode(label, text):
    """The generic sanitizer strips these; doing so corrupts real messages."""
    clean, _offsets = fidelity_text(text)
    assert clean == text, label


@pytest.mark.parametrize(
    "label, text, expected",
    [
        ("zero width space", "a\u200bb", "ab"),
        ("word joiner", "a\u2060b", "ab"),
        ("bom", "a\ufeffb", "ab"),
        ("rlo override", "a\u202eb", "ab"),
        ("lri isolate", "a\u2066b", "ab"),
        ("null control", "a\x00b", "ab"),
    ],
)
def test_fidelity_text_drops_unsafe_invisibles(label, text, expected):
    clean, _offsets = fidelity_text(text)
    assert clean == expected, label


def test_entity_offsets_index_the_exposed_text():
    """The bug: offsets were computed on raw text while sanitized text was shown."""
    raw = "\u0645\u06cc\u200c\u06a9\u0646\u062f"  # mi + ZWNJ + konad
    message = SimpleNamespace(message=raw, entities=[_entity("Bold", offset=3, length=3)])

    entity = describe_entities(message)[0]
    clean, _offsets = fidelity_text(raw)

    assert _utf16_slice(clean, entity["offset"], entity["length"]) == entity["text"]
    assert entity["text"] == "\u06a9\u0646\u062f"


def test_entity_offsets_handle_surrogate_pairs():
    raw = "\U0001f389 bold here"  # the emoji is two UTF-16 units
    message = SimpleNamespace(message=raw, entities=[_entity("Bold", offset=3, length=4)])

    entity = describe_entities(message)[0]
    clean, _offsets = fidelity_text(raw)

    assert entity["text"] == "bold"
    assert _utf16_slice(clean, entity["offset"], entity["length"]) == "bold"


def test_entity_offsets_rebase_when_an_unsafe_char_is_removed():
    raw = "a\u200bBOLD"  # the ZWSP before the entity shifts every later offset
    message = SimpleNamespace(message=raw, entities=[_entity("Bold", offset=2, length=4)])

    entity = describe_entities(message)[0]
    clean, _offsets = fidelity_text(raw)

    assert entity["offset"] == 1, "offset was not rebased onto the cleaned text"
    assert _utf16_slice(clean, entity["offset"], entity["length"]) == "BOLD"


def test_out_of_range_entity_offsets_are_passed_through_untouched():
    message = SimpleNamespace(message="short", entities=[_entity("Bold", offset=99, length=4)])
    entity = describe_entities(message)[0]
    assert entity["offset"] == 99
    assert "text" not in entity


def test_deep_message_dict_exposes_fidelity_text_when_it_differs():
    raw = "\u0645\u06cc\u200c\u06a9\u0646\u062f"
    message = SimpleNamespace(message=raw, entities=[])
    data = deep_message_dict(message, {"id": 1, "text": sanitize_user_content(raw)})

    assert data["text_fidelity"] == raw
    assert data["text"] != raw, "precondition: the sanitized text really does differ"
    assert "ntity offsets index into this field" in data["text_fidelity_note"]


# --- display_name: single-line, bounded, but Unicode-faithful ----------------


@pytest.mark.parametrize(
    "label, text",
    [
        ("emoji zwj family", "\U0001f468\u200d\U0001f469\u200d\U0001f467"),
        ("persian zwnj", "\u0645\u06cc\u200c\u06a9\u0646\u062f"),
        (
            "regional flag tag sequence",
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
        ),
    ],
)
def test_display_name_keeps_compound_sequences_sanitize_name_destroys(label, text):
    assert display_name(text) == text, label
    # Guard the premise: this is exactly what the generic helper gets wrong.
    assert sanitize_name(text) != text, f"{label}: sanitize_name no longer breaks this"


@pytest.mark.parametrize(
    "label, text, expected",
    [
        ("rlo override", "a\u202eb", "ab"),
        ("zero width space", "a\u200bb", "ab"),
        ("newline", "a\nb", "a b"),
        ("carriage return", "a\rb", "a b"),
        ("tab", "a\tb", "a b"),
        ("collapsed spaces", "a     b", "a b"),
        ("surrounding space", "  a b  ", "a b"),
        ("control char", "a\x07b", "ab"),
    ],
)
def test_display_name_still_removes_unsafe_and_forces_one_line(label, text, expected):
    assert display_name(text) == expected, label


def test_display_name_bounds_its_length():
    """The ellipsis is part of the budget, so the result is exactly max_length."""
    result = display_name("x" * 400)

    assert result == "x" * 255 + "…"
    assert len(result) == 256


def test_display_name_handles_none():
    assert display_name(None) == ""


def test_display_name_preserves_a_keycap_sequence():
    """Not a sanitize_name bug - VS16 is Mn and the keycap mark is Me, not Cf - but
    the sequence still has to survive display_name intact."""
    keycap = "1️⃣"
    assert display_name(keycap) == keycap


@pytest.mark.parametrize(
    "label, separator",
    [
        ("carriage return", "\r"),
        ("line feed", "\n"),
        ("crlf", "\r\n"),
        ("tab", "\t"),
        ("vertical tab", "\v"),
        ("form feed", "\f"),
        ("next line U+0085", "\u0085"),
        ("line separator U+2028", "\u2028"),
        ("paragraph separator U+2029", "\u2029"),
    ],
)
def test_display_name_is_single_line_for_every_unicode_break(label, separator):
    """U+2028/U+2029 are Zl/Zp, so they survive the fidelity pass untouched."""
    result = display_name(f"a{separator}b")

    assert result == "a b", label
    assert separator not in result


@pytest.mark.parametrize("max_length", [1, 2, 10, 256])
def test_display_name_never_exceeds_max_length(max_length):
    """The ellipsis counts towards the bound the caller asked for."""
    assert len(display_name("x" * 500, max_length=max_length)) <= max_length


def test_display_name_leaves_text_at_the_bound_untruncated():
    exact = "x" * 256
    assert display_name(exact) == exact


def test_premium_sticker_effect_is_reported_when_present():
    """Telegram ships it as a VideoSize of type "f"; no preview here renders it."""
    document = SimpleNamespace(
        id=1,
        attributes=[],
        thumbs=[],
        video_thumbs=[
            SimpleNamespace(type="v", w=100, h=100, size=10),
            SimpleNamespace(type="f", w=512, h=512, size=4096),
        ],
    )
    msg = SimpleNamespace(media=object(), document=document, sticker=document, file=None)

    effect = describe_media(msg)["premium_effect"]

    assert effect["kind"] == "premium_sticker_effect"
    assert (effect["width"], effect["height"], effect["bytes"]) == (512, 512, 4096)
    assert "get_telegram_frames" in effect["note"]


def test_no_premium_effect_key_for_an_ordinary_sticker():
    document = SimpleNamespace(
        id=1, attributes=[], thumbs=[], video_thumbs=[SimpleNamespace(type="v", w=1, h=1, size=1)]
    )
    msg = SimpleNamespace(media=object(), document=document, sticker=document, file=None)

    assert "premium_effect" not in describe_media(msg)


# --- Fidelity across every human-readable field -------------------------------

PERSIAN = "\u0645\u06cc\u200c\u06a9\u0646\u062f"  # mi-konad, needs its ZWNJ
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # one emoji, held by ZWJ
HOSTILE = "\u202eevil\u200b\u2062\ufff9"  # RLO + hidden padding


@pytest.mark.parametrize("bound", [0, -1, -50])
def test_display_name_with_a_non_positive_bound_returns_nothing(bound):
    """A zero budget has no room for text or for the ellipsis."""
    assert display_name("x" * 50, max_length=bound) == ""


@pytest.mark.parametrize(
    "label, codepoint",
    [
        ("function application", 0x2061),
        ("invisible times", 0x2062),
        ("invisible separator", 0x2063),
        ("invisible plus", 0x2064),
        ("interlinear anchor", 0xFFF9),
        ("interlinear separator", 0xFFFA),
        ("interlinear terminator", 0xFFFB),
        ("mongolian vowel separator", 0x180E),
    ],
)
def test_fidelity_text_strips_the_remaining_hidden_characters(label, codepoint):
    """These render as nothing, so they hide text from a reader."""
    clean, _offsets = fidelity_text(f"a{chr(codepoint)}b")
    assert clean == "ab", label


@pytest.mark.parametrize("codepoint", [0x200C, 0x200D, 0x200E, 0x200F])
def test_fidelity_text_still_keeps_the_legitimate_joiners_and_marks(codepoint):
    character = chr(codepoint)
    clean, _offsets = fidelity_text(f"a{character}b")
    assert clean == f"a{character}b"


def test_reply_quote_keeps_the_readable_fragment_and_documents_its_offset():
    msg = SimpleNamespace(reply_to=SimpleNamespace(quote_text=PERSIAN + HOSTILE, quote_offset=12))

    quote = describe_reply_quote(msg)

    assert PERSIAN in quote["text"], "the ZWNJ was stripped out of the quote"
    assert "\u202e" not in quote["text"] and "\u200b" not in quote["text"]
    assert quote["offset"] == 12
    assert quote["modified"] is True
    assert "UTF-16" in quote["note"] and "replied-to message" in quote["note"]


def test_reply_quote_is_absent_for_a_whole_message_reply():
    msg = SimpleNamespace(reply_to=SimpleNamespace(quote_text=None, reply_to_msg_id=7))
    assert describe_reply_quote(msg) is None


def test_button_labels_keep_unicode_and_drop_spoofing_characters():
    msg = SimpleNamespace(
        buttons=[[SimpleNamespace(text=PERSIAN), SimpleNamespace(text="Pay\u202eDNES")]]
    )

    labels = describe_buttons(msg)

    assert labels[0] == PERSIAN
    assert "\u202e" not in labels[1], "a bidi override survived into a button label"


def test_forward_names_keep_their_unicode():
    msg = SimpleNamespace(
        fwd_from=SimpleNamespace(from_name=PERSIAN, post_author=None),
        forward=SimpleNamespace(
            chat=SimpleNamespace(title=FAMILY, first_name=None, last_name=None),
            sender=SimpleNamespace(first_name="a‮b", last_name=None),
        ),
    )
    forwarded = {"from_name": "x", "from_chat": "x", "from_user": "x", "date": 1}

    result = _fidelity_forward(msg, forwarded)

    assert result["from_name"] == PERSIAN
    assert result["from_chat"] == FAMILY
    assert "\u202e" not in result["from_user"]
    assert result["date"] == 1, "an unrelated field was rewritten"


def test_media_title_and_performer_keep_unicode_but_filenames_stay_strict():
    file = SimpleNamespace(
        name="track\u200c.mp3", title=PERSIAN, performer=FAMILY, mime_type="audio/mpeg"
    )
    msg = SimpleNamespace(media=object(), audio=object(), file=file, document=None)

    info = describe_media(msg)

    assert info["title"] == PERSIAN
    assert info["performer"] == FAMILY
    # A filename can reach a filesystem, so it keeps the strict policy.
    assert "\u200c" not in info["file_name"]


def test_poll_question_keeps_unicode():
    poll = SimpleNamespace(poll=SimpleNamespace(question=SimpleNamespace(text=PERSIAN + HOSTILE)))
    msg = SimpleNamespace(media=object(), poll=poll, file=None, document=None)

    info = describe_media(msg)

    assert PERSIAN in info["poll_question"]
    assert "\u202e" not in info["poll_question"]


# --- End-to-end: message_to_dict() -> deep_message_dict() ---------------------

FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"


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


def test_a_valid_emoji_tag_sequence_survives():
    clean, _offsets = fidelity_text(f"Team {FLAG}!")
    assert clean == f"Team {FLAG}!"


@pytest.mark.parametrize(
    "label, text",
    [
        ("lone tag letter", "a\U000e0067b"),
        ("tag run with no emoji base", "ab\U000e0067\U000e007f"),
        ("tag run with no cancel", "\U0001f3f4\U000e0067\U000e0062"),
        ("cancel on its own", "a\U000e007fb"),
    ],
)
def test_stray_tag_characters_are_removed(label, text):
    clean, _offsets = fidelity_text(text)
    assert not any(0xE0020 <= ord(ch) <= 0xE007F for ch in clean), label


# --- UTS #51 tag-sequence validation -----------------------------------------

TAG_BASE = "\U0001f3f4"
TAG_END = "\U000e007f"


def _tags(*letters):
    return "".join(chr(0xE0000 + ord(c)) for c in letters)


@pytest.mark.parametrize(
    "label, text",
    [
        ("scotland", TAG_BASE + _tags(*"gbsct") + TAG_END),
        ("wales", TAG_BASE + _tags(*"gbwls") + TAG_END),
        ("england", TAG_BASE + _tags(*"gbeng") + TAG_END),
    ],
)
def test_valid_subdivision_flags_survive(label, text):
    clean, _offsets = fidelity_text(f"Team {text}!")
    assert clean == f"Team {text}!", label


@pytest.mark.parametrize(
    "label, text",
    [
        ("cjk ext-B base", "\U00020000" + _tags(*"gb") + TAG_END),
        ("non-emoji supplementary base", "\U0001d400" + _tags(*"gb") + TAG_END),
        ("wrong emoji base", "\U0001f600" + _tags(*"gb") + TAG_END),
        ("no base at all", "text" + _tags(*"gb") + TAG_END),
        ("missing cancel", TAG_BASE + _tags(*"gbsct")),
        ("uppercase tag spec", TAG_BASE + _tags(*"GB") + TAG_END),
        ("punctuation tag spec", TAG_BASE + "\U000e0021" + TAG_END),
        ("overlong tag spec", TAG_BASE + _tags(*"abcdefgh") + TAG_END),
        ("empty spec, cancel only", TAG_BASE + TAG_END),
    ],
)
def test_invalid_tag_sequences_are_stripped(label, text):
    clean, _offsets = fidelity_text(text)
    assert not any(0xE0020 <= ord(ch) <= 0xE007F for ch in clean), label


# --- Truncation must not split a compound sequence ---------------------------


def _tail(text):
    return text[:-1] if text.endswith("…") else text


@pytest.mark.parametrize(
    "label, suffix",
    [
        ("family emoji", FAMILY),
        ("persian zwnj", PERSIAN),
        ("vs16 emoji", "\u2764\ufe0f"),
        ("combining mark", "e\u0301"),
        ("subdivision flag", TAG_BASE + _tags(*"gbsct") + TAG_END),
    ],
)
@pytest.mark.parametrize("bound", range(250, 260))
def test_truncation_never_leaves_a_dangling_half(label, suffix, bound):
    """Cutting inside a compound sequence is exactly what these helpers promise not to do."""
    import unicodedata as ud

    result = display_name("x" * 250 + suffix, max_length=bound)

    assert len(result) <= bound
    if not result.endswith("…"):
        # Nothing was cut, so the tail is whatever the input legitimately ended with.
        assert result == "x" * 250 + suffix
        return
    body = _tail(result)
    if body:
        last = body[-1]
        assert last != "\u200d", f"{label}: dangling ZWJ"
        assert not 0xFE00 <= ord(last) <= 0xFE0F, f"{label}: dangling variation selector"
        assert not 0xE0020 <= ord(last) <= 0xE007F, f"{label}: dangling tag character"
        assert last != TAG_BASE, f"{label}: flag base with no tag spec"
        assert ud.category(last) not in ("Mn", "Mc", "Me"), f"{label}: dangling combining mark"


# --- Quote and text_fidelity must not overstate ------------------------------


def test_a_quote_that_naturally_ends_in_an_ellipsis_is_not_called_truncated():
    """The old code inferred truncation from the final character."""
    natural = "این جمله تمام می\u200cشود…\u200b"  # ends with a real ellipsis + a ZWSP
    msg = SimpleNamespace(reply_to=SimpleNamespace(quote_text=natural, quote_offset=0))

    quote = describe_reply_quote(msg)

    assert quote["text"].endswith("…")
    assert quote["filtered"] is True, "the ZWSP removal should be reported"
    assert quote["truncated"] is False, "nothing was cut, only filtered"
    assert "truncated" not in quote["note"]


def test_a_genuinely_truncated_quote_reports_both_facts():
    msg = SimpleNamespace(
        reply_to=SimpleNamespace(quote_text="\u200b" + "x" * 5000, quote_offset=0)
    )

    quote = describe_reply_quote(msg)

    assert quote["filtered"] is True
    assert quote["truncated"] is True
    assert "truncated" in quote["note"]


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


# --- Truncation: whole sequences only, cut at every internal position ---------

ZWNJ_PAIR = "\u06cc\u200c\u06a9"  # a ZWNJ-joined Persian pair
REGIONAL_PAIR = "\U0001f1ee\U0001f1f7"  # a two-codepoint regional flag
SCOTLAND = "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in "gbsct") + "\U000e007f"


@pytest.mark.parametrize(
    "label, sequence",
    [
        ("zwj family", FAMILY),
        ("vs16 emoji", "\u2764\ufe0f"),
        ("base plus combining mark", "e\u0301"),
        ("persian zwnj pair", ZWNJ_PAIR),
        ("subdivision flag", SCOTLAND),
        ("regional indicator pair", REGIONAL_PAIR),
    ],
)
def test_truncation_keeps_a_sequence_whole_or_drops_it_entirely(label, sequence):
    """Cut at every position inside the sequence: a prefix fragment is never allowed.

    Checking only the final code point is not enough — a cut landing immediately
    *before* a ZWJ leaves a complete-looking man emoji that is really a third of a
    family.
    """
    prefix = "y" * 20
    text = prefix + sequence

    for bound in range(len(prefix), len(text) + 2):
        result = display_name(text, max_length=bound)
        body = result[:-1] if result.endswith("…") else result
        tail = body[len(prefix) :] if body.startswith(prefix) else body.lstrip("y")

        assert len(result) <= bound, f"{label} @ {bound}: exceeded the bound"
        assert tail in ("", sequence), f"{label} @ {bound}: fragment {tail!r}"


@pytest.mark.parametrize("sequence", [FAMILY, ZWNJ_PAIR, SCOTLAND, REGIONAL_PAIR])
def test_truncation_never_ends_on_a_joiner(sequence):
    text = "y" * 20 + sequence
    for bound in range(len(text) + 2):
        body = display_name(text, max_length=bound).rstrip("…")
        assert not body.endswith("\u200d")
        assert not body.endswith("\u200c")


# --- Emoji tag sequences: RGI only -------------------------------------------


def _tag_seq(spec):
    return "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in spec) + "\U000e007f"


@pytest.mark.parametrize("spec", ["gbeng", "gbsct", "gbwls"])
def test_rgi_subdivision_flags_survive(spec):
    text = f"Flag {_tag_seq(spec)} here"
    clean, _offsets = fidelity_text(text)
    assert clean == text


@pytest.mark.parametrize(
    "label, spec",
    [
        ("syntactically fine, not a subdivision", "us01"),
        ("not a region at all", "zzzzz"),
        ("valid region, no subdivision", "gb"),
        ("uppercase", "GBSCT"),
    ],
)
def test_well_formed_but_non_rgi_tag_sequences_are_stripped(label, spec):
    """Only RGI sequences render; anything else is invisible, so it can hide text."""
    clean, _offsets = fidelity_text(_tag_seq(spec))
    assert not any(0xE0020 <= ord(ch) <= 0xE007F for ch in clean), label


def test_an_overlong_tag_sequence_is_rejected_by_the_uts51_bound():
    """UTS #51 caps the whole emoji tag sequence at 32 code points."""
    long_spec = "a" * 40
    clean, _offsets = fidelity_text(_tag_seq(long_spec))
    assert not any(0xE0020 <= ord(ch) <= 0xE007F for ch in clean)


def test_the_rgi_set_is_the_documented_policy():
    """The narrower-than-syntax policy is explicit, not inferred."""
    from telegram_mcp.message_view import _RGI_TAG_SPECS

    assert _RGI_TAG_SPECS == {"gbeng", "gbsct", "gbwls"}


# --- UAX #29 segmentation ----------------------------------------------------

SKIN_TONE = "\U0001f44d\U0001f3fd"  # Sk modifier, missed by the old scan
PROFESSION = "\U0001f469\U0001f3fb\u200d\U0001f4bb"  # base + tone + ZWJ + object
HANGUL = "\u1100\u1161\u11a8"
DEVANAGARI = "\u0915\u094d\u0937"


@pytest.mark.parametrize(
    "label, sequence",
    [
        ("emoji modifier", SKIN_TONE),
        ("modifier inside a ZWJ sequence", PROFESSION),
        ("hangul jamo", HANGUL),
        ("indic conjunct", DEVANAGARI),
        ("zwj family", FAMILY),
        ("regional flag", REGIONAL_PAIR),
        ("subdivision flag", SCOTLAND),
        ("persian zwnj pair", ZWNJ_PAIR),
    ],
)
def test_every_grapheme_cluster_is_kept_whole_or_dropped(label, sequence):
    """The hand-rolled scan missed emoji modifiers, Hangul and Indic conjuncts."""
    prefix = "y" * 30
    text = prefix + sequence

    for bound in range(len(prefix), len(text) + 2):
        result = display_name(text, max_length=bound)
        body = result[:-1] if result.endswith("…") else result
        tail = body[len(prefix) :] if body.startswith(prefix) else body.lstrip("y")

        assert len(result) <= bound
        assert tail in ("", sequence), f"{label} @ {bound}: fragment {tail!r}"


def test_segmentation_uses_the_unicode_algorithm():
    """A heuristic keeps missing categories; the standard does not."""
    from telegram_mcp.message_view import _sequence_starts

    assert _sequence_starts(SKIN_TONE) == [0], "the skin-tone modifier was split off"
    assert _sequence_starts(PROFESSION) == [0]
    assert _sequence_starts(HANGUL) == [0]
    assert _sequence_starts(DEVANAGARI) == [0]
    # The one documented addition on top of UAX #29.
    assert _sequence_starts(ZWNJ_PAIR) == [0], "the ZWNJ bond was split"


# --- Message-level effects ---------------------------------------------------


def test_a_message_level_effect_is_reported_separately_from_the_sticker_one():
    """Telegram can play both at once; the structured view must show both."""
    msg = _plain_message(effect=5104841245755180586)

    effect = _deep(msg)["message_effect"]

    assert effect["effect_id"] == 5104841245755180586
    assert effect["kind"] == "message_effect"
    assert "distinct from a premium sticker" in effect["note"]
    assert "GetAvailableEffects" in effect["note"]


def test_no_message_effect_key_without_one():
    assert "message_effect" not in _deep(_plain_message())
