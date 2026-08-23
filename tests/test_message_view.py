"""The description layer, one Telethon field at a time.

Every message here is a stub: the module reads plain attributes off the Telethon
object and never calls the API, so no client and no network are involved.

Split off from this file: the end-to-end route through upstream's
``message_to_dict`` (``test_message_view_pipeline.py``), the string rules these
descriptions lean on (``test_text_fidelity.py``), and what ``import *`` does to the
tool package (``test_tool_registry.py``, formerly the merge policy).
"""

from types import SimpleNamespace

import pytest

from helpers_unicode import FAMILY, HOSTILE, PERSIAN
from sanitize import sanitize_name, sanitize_user_content
from telegram_mcp.message_view import (
    deep_message_dict,
    fidelity_text,
    describe_custom_emoji,
    describe_entities,
    describe_media,
    describe_media_label,
    describe_reactions,
    describe_topic,
    _fidelity_forward,
    describe_buttons,
    describe_reply_quote,
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


def test_the_deep_view_walks_the_message_text_exactly_once():
    """Three independent recomputations of the string the entity offsets index
    into is three chances for them to disagree — and 150 full text passes to
    build one 50-message page, all of them on the event loop."""
    import telegram_mcp.message_view as message_view

    calls = []
    original = message_view.fidelity_text

    def _counted(raw):
        calls.append(raw)
        return original(raw)

    message_view.fidelity_text = _counted
    try:
        msg = SimpleNamespace(
            id=1,
            message=EMOJI_TEXT,
            entities=[_entity("CustomEmoji", offset=0, length=2, document_id=99)],
        )
        data = deep_message_dict(msg, {"id": 1, "text": EMOJI_TEXT})
    finally:
        message_view.fidelity_text = original

    assert len(calls) == 1, f"fidelity_text ran {len(calls)} times: {calls}"
    assert data["entities"][0]["custom_emoji_id"] == 99
    assert data["custom_emoji"] == [{"document_id": 99, "placeholder": "🎉", "offset": 0}]


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
        # A marked channel ID: abs() strips the sign and % 10**10 cuts the -100
        # prefix. Without this case both operations are dead code — every other id
        # here is positive and 10 digits, so abs(id) % 10**10 == id.
        (
            SimpleNamespace(username=None, id=-1001234567890, broadcast=True),
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


@pytest.mark.parametrize("offset, length", [(0, -5), (2, -5), (0, -1)])
def test_a_negative_entity_length_is_passed_through_not_sliced(offset, length):
    """offset_map[negative] indexes from the end, so an unchecked negative length
    produced a confident slice of text the entity never covered."""
    msg = SimpleNamespace(
        message="abcdefghij", entities=[_entity("Bold", offset=offset, length=length)]
    )

    item = describe_entities(msg)[0]

    assert item["offset"] == offset
    assert item["length"] == length
    assert "text" not in item


def test_deep_message_dict_exposes_fidelity_text_when_it_differs():
    raw = "\u0645\u06cc\u200c\u06a9\u0646\u062f"
    message = SimpleNamespace(message=raw, entities=[])
    data = deep_message_dict(message, {"id": 1, "text": sanitize_user_content(raw)})

    assert data["text_fidelity"] == raw
    assert data["text"] != raw, "precondition: the sanitized text really does differ"
    assert "ntity offsets index into this field" in data["text_fidelity_note"]


def test_premium_sticker_effect_is_reported_when_present():
    """A VideoSize of type "f" — get_media_frames(premium_effect=True) samples it."""
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


def test_a_document_label_sanitizes_its_filename_strictly():
    """A filename can reach a filesystem, so it keeps sanitize_name \u2014 including the
    ZWNJ that display_name deliberately preserves, and the bidi override an attacker
    would use to disguise an extension."""
    name = "cl\u200cip\u202e.mp4"
    msg = SimpleNamespace(document=object(), file=SimpleNamespace(name=name))

    assert describe_media_label(msg) == "document: clip.mp4"
    # Guard the premise: display_name is the wrong helper here and keeps the ZWNJ.
    assert "\u200c" in display_name(name), "display_name no longer keeps the ZWNJ"


def test_a_sticker_label_keeps_the_alt_glyph_intact():
    """The alt is read by a human, so it keeps display_name: sanitize_name would
    break this family emoji into three separate people."""
    sticker = SimpleNamespace(attributes=[SimpleNamespace(alt=FAMILY)])
    msg = SimpleNamespace(sticker=sticker)

    assert describe_media_label(msg) == f"sticker {FAMILY}"
    # Guard the premise: this is exactly what the generic helper gets wrong.
    assert sanitize_name(FAMILY) != FAMILY, "sanitize_name no longer breaks this"


def test_a_sticker_alt_containing_a_colon_is_still_cleaned():
    """The branch must come from the KIND, never from the payload.

    Splitting at the first ": " asked the attacker-controlled value which format
    it was in. A sticker alt containing ": " — a pack creator picks that text —
    took the "document: <name>" path, and everything before the colon, the whole
    alt included, was returned verbatim with its bidi override intact.
    """
    alt = "‮fdp.exe: report"
    msg = SimpleNamespace(sticker=SimpleNamespace(attributes=[SimpleNamespace(alt=alt)]))

    label = describe_media_label(msg)

    assert "‮" not in label, "the direction override survived the label"
    assert label.startswith("sticker ")


def test_an_animation_is_labelled_gif_not_video():
    """Telethon sets .gif AND .video for an animation; upstream checks video first.

    So the label a caller reads first said "video" for a GIF while describe_media,
    in the same payload, reported kind="gif". Confirmed against a real animation
    in a live chat: attributes carried DocumentAttributeAnimated and
    media_details.kind was already "gif" while media said "video".
    """
    document = SimpleNamespace(id=1, mime_type="video/mp4", size=47979, attributes=[], thumbs=[])
    msg = SimpleNamespace(
        web_preview=None,
        sticker=None,
        photo=None,
        voice=None,
        video_note=None,
        video=document,
        audio=None,
        gif=document,
        document=document,
        contact=None,
        geo=None,
        poll=None,
        media=document,
    )

    assert describe_media_label(msg) == "gif"
    assert describe_media(msg)["kind"] == "gif", "the two answers must not diverge again"


def test_a_plain_video_is_still_a_video():
    """The correction above must not relabel every video."""
    document = SimpleNamespace(id=1, mime_type="video/mp4", size=100, attributes=[], thumbs=[])
    msg = SimpleNamespace(
        web_preview=None,
        sticker=None,
        photo=None,
        voice=None,
        video_note=None,
        video=document,
        audio=None,
        gif=None,
        document=document,
        contact=None,
        geo=None,
        poll=None,
        media=document,
    )

    assert describe_media_label(msg) == "video"


def test_a_message_with_no_media_has_no_label():
    assert describe_media_label(SimpleNamespace()) is None


def test_poll_question_keeps_unicode():
    poll = SimpleNamespace(poll=SimpleNamespace(question=SimpleNamespace(text=PERSIAN + HOSTILE)))
    msg = SimpleNamespace(media=object(), poll=poll, file=None, document=None)

    info = describe_media(msg)

    assert PERSIAN in info["poll_question"]
    assert "\u202e" not in info["poll_question"]


@pytest.mark.parametrize(
    "label, raw, expected",
    [
        ("bare cr", "line one\rline two", "line one\nline two"),
        ("crlf", "line one\r\nline two", "line one\nline two"),
        ("next line", "line one\x85line two", "line one\nline two"),
        ("vertical tab", "line one\vline two", "line one\nline two"),
        ("form feed", "line one\fline two", "line one\nline two"),
    ],
)
def test_a_poll_question_keeps_the_break_instead_of_gluing_the_words(label, raw, expected):
    """CR, NEL, VT and FF are Cc: deleting them would join the words either side."""
    poll = SimpleNamespace(poll=SimpleNamespace(question=SimpleNamespace(text=raw)))
    msg = SimpleNamespace(media=object(), poll=poll, file=None, document=None)

    assert describe_media(msg)["poll_question"] == expected, label


@pytest.mark.parametrize(
    "label, raw, expected",
    [
        ("bare cr", "line one\rline two", "line one\nline two"),
        ("crlf", "line one\r\nline two", "line one\nline two"),
    ],
)
def test_a_reply_quote_keeps_the_break_instead_of_gluing_the_words(label, raw, expected):
    msg = SimpleNamespace(reply_to=SimpleNamespace(quote_text=raw, quote_offset=0))

    quote = describe_reply_quote(msg)

    assert quote["text"] == expected, label
    assert quote["modified"] is True, "the quote is no longer what Telegram reported"


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


def test_an_offset_that_could_not_be_rebased_says_so():
    """describe_entities promises an offset indexes `text_fidelity`.

    An offset it cannot rebase keeps Telegram's raw number, deliberately — but
    published under the same key with no marker, a caller could not tell the two
    coordinate spaces apart. The only hint was an absent "text", which also goes
    missing for a legitimate offset landing mid-surrogate.
    """
    msg = SimpleNamespace(
        message="ab",
        entities=[
            SimpleNamespace(offset=0, length=1, document_id=111, url=None, user_id=None),
            SimpleNamespace(offset=99, length=1, document_id=222, url=None, user_id=None),
        ],
    )

    emoji = describe_custom_emoji(msg)

    assert emoji[0].get("offset_is_raw") is None, "a rebased offset was marked raw"
    assert emoji[1]["offset_is_raw"] is True, "a raw offset was published as a rebased one"
