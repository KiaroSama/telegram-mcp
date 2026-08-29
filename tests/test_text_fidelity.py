"""The string rules, tested without a message.

These moved out of ``test_message_view.py`` with the code they cover: they build
raw strings and assert on cleaned strings, so nothing here needs a Telethon
object. The Telegram-shaped tests stayed behind.
"""

import pytest

from helpers_unicode import FAMILY, FLAG, PERSIAN
from sanitize import sanitize_name
from telegram_mcp.text_fidelity import (
    _sequence_starts,
    display_name,
    fidelity_text,
)


def _tags(*letters):
    return "".join(chr(0xE0000 + ord(c)) for c in letters)


def _tail(text):
    return text[:-1] if text.endswith("…") else text


def _tag_seq(spec):
    return "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in spec) + "\U000e007f"


# --- UTS #51 tag-sequence validation -----------------------------------------

TAG_BASE = "\U0001f3f4"
TAG_END = "\U000e007f"


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
    from telegram_mcp.text_fidelity import _RGI_TAG_SPECS

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
    assert _sequence_starts(SKIN_TONE) == [0], "the skin-tone modifier was split off"
    assert _sequence_starts(PROFESSION) == [0]
    assert _sequence_starts(HANGUL) == [0]
    assert _sequence_starts(DEVANAGARI) == [0]
    # The one documented addition on top of UAX #29.
    assert _sequence_starts(ZWNJ_PAIR) == [0], "the ZWNJ bond was split"


# --- The public surface: fidelity_text and display_name ----------------------
#
# These moved from test_message_view.py, which reached the same two functions
# through message_view's re-export.


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
