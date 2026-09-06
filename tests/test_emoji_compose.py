"""Putting a premium emoji into an existing message without breaking it.

Two things here are load-bearing and fail SILENTLY when they are wrong.

An entity's `offset` is a count of UTF-16 code units into the message. Inserting a
glyph shifts every entity that starts after it, and getting that wrong does not
raise: Telegram accepts the message and renders the bold run, the link or the
next emoji over the wrong characters. So the shift is tested per entity class -
before, after, and straddling the insertion point.

Frame choice is the other one. rlottie renders a .tgs honestly, and an animation
that begins and ends transparent legitimately yields blank frames at both ends -
`frames.py` says so and flags them. Picking frame 0, or the last frame, is how an
emoji ends up looking like an empty box in a contact sheet and gets judged on it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import emoji_compose  # noqa: E402


def _u16(text):
    return len(text.encode("utf-16-le")) // 2


# --------------------------------------------------------------------- insert


def test_an_emoji_lands_at_the_end_of_the_line_it_was_asked_for():
    text, ents = emoji_compose.insert_emoji(
        "first line\nsecond line\nthird line", [], line=2, glyph="✅", document_id="123"
    )

    assert text == "first line\nsecond line✅\nthird line"
    assert ents == [
        {
            "type": "custom_emoji",
            "offset": _u16("first line\nsecond line"),
            "length": 1,
            "custom_emoji_id": "123",
        }
    ]


def test_an_emoji_can_lead_the_line_instead():
    text, ents = emoji_compose.insert_emoji(
        "alpha\nbeta", [], line=2, glyph="🔥", document_id="9", where="start"
    )

    assert text == "alpha\n🔥beta"
    assert ents[0]["offset"] == _u16("alpha\n")
    assert ents[0]["length"] == 2, "a non-BMP glyph is TWO UTF-16 units"


def test_entities_before_the_insertion_do_not_move():
    before = [{"type": "bold", "offset": 0, "length": 5}]

    _text, ents = emoji_compose.insert_emoji(
        "alpha\nbeta", before, line=2, glyph="🔥", document_id="9"
    )

    assert {"type": "bold", "offset": 0, "length": 5} in ents


def test_entities_after_the_insertion_shift_by_the_glyph_width():
    # A bold run on line 3, with the emoji going onto line 1.
    text = "a\nb\ncc"
    before = [{"type": "bold", "offset": _u16("a\nb\n"), "length": 2}]

    _new, ents = emoji_compose.insert_emoji(text, before, line=1, glyph="🔥", document_id="9")

    bold = next(e for e in ents if e["type"] == "bold")
    assert bold["offset"] == _u16("a\nb\n") + 2, "the run did not follow the text it marks"
    assert bold["length"] == 2, "length must not change - only the start moved"


def test_an_entity_spanning_the_insertion_point_grows_instead_of_moving():
    """A run whose middle the glyph lands in grows; it does not slide.

    The emoji goes to the START of line 2 (offset 6) while the run covers 0-8, so
    the insertion is genuinely inside it. Sliding the whole run would move it off
    the text it marks; only growing keeps it over the same words.
    """
    text = "alpha\nbeta"
    quote = [{"type": "blockquote", "offset": 0, "length": _u16("alpha\nbe")}]

    _new, ents = emoji_compose.insert_emoji(
        text, quote, line=2, glyph="🔥", document_id="9", where="start"
    )

    q = next(e for e in ents if e["type"] == "blockquote")
    assert q["offset"] == 0, "the run slid instead of growing"
    assert q["length"] == _u16("alpha\nbe") + 2, "the quote no longer covers its own text"


def test_a_block_entity_ending_exactly_at_the_insertion_point_still_grows():
    """The commonest shape of all: a quote around the line, emoji appended to it.

    The run ends exactly where the glyph goes. A quote that stops one emoji short
    of its own last line is visibly wrong, so block-level entities take it in.
    """
    text = "alpha\nbeta"
    quote = [{"type": "blockquote", "offset": 0, "length": _u16(text)}]

    _new, ents = emoji_compose.insert_emoji(text, quote, line=2, glyph="🔥", document_id="9")

    q = next(e for e in ents if e["type"] == "blockquote")
    assert q["length"] == _u16(text) + 2


def test_an_inline_entity_ending_at_the_insertion_point_does_NOT_grow():
    """The opposite call, and the reason the two cannot be treated alike.

    Extending a `text_url` over the new glyph would put the emoji inside the link,
    where a reader can click it. Nothing about "add an emoji to this line" asks
    for that, and the mistake is invisible until somebody taps it.
    """
    text = "alpha\nbeta"
    link = [
        {
            "type": "text_url",
            "offset": _u16("alpha\n"),
            "length": 4,
            "url": "https://example.invalid",
        }
    ]

    _new, ents = emoji_compose.insert_emoji(text, link, line=2, glyph="🔥", document_id="9")

    anchor = next(e for e in ents if e["type"] == "text_url")
    assert anchor["length"] == 4, "the emoji was swallowed into the link"


def test_a_line_that_does_not_exist_is_refused_by_name():
    with pytest.raises(ValueError, match="2 line"):
        emoji_compose.insert_emoji("a\nb", [], line=9, glyph="x", document_id="1")


def test_every_id_leaves_as_a_string():
    """Through a JSON number these ids come back as a DIFFERENT emoji."""
    _t, ents = emoji_compose.insert_emoji(
        "a", [], line=1, glyph="✅", document_id=5776121630275149735
    )

    assert ents[0]["custom_emoji_id"] == "5776121630275149735"


# ---------------------------------------------------------------- best frame


def test_the_fullest_frame_wins_not_the_first():
    frames = [{"blank": True}, {"blank": False, "bytes": 900}, {"blank": False, "bytes": 4200}]

    assert emoji_compose.best_frame(frames) == 2


def test_a_blank_first_and_last_frame_are_both_skipped():
    """Measured on Telegram's own fire effect: frame 0 of 181 has zero visible
    pixels and frame 180 has eight. Both ends are legitimately empty."""
    frames = [{"blank": True}, {"blank": False, "bytes": 3000}, {"blank": True}]

    assert emoji_compose.best_frame(frames) == 1


def test_all_blank_still_returns_something_rather_than_failing():
    assert emoji_compose.best_frame([{"blank": True}, {"blank": True}]) == 0


def test_a_still_image_has_exactly_one_frame_and_it_is_chosen():
    assert emoji_compose.best_frame([{"bytes": 120}]) == 0
