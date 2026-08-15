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
    assert "entity offsets index into this field" in data["text_fidelity_note"]


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
