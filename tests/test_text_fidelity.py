"""The string rules, tested without a message.

These moved out of ``test_message_view.py`` with the code they cover: they build
raw strings and assert on cleaned strings, so nothing here needs a Telethon
object. The Telegram-shaped tests stayed behind.
"""

import pytest

FAMILY = "👨‍👩‍👧"  # one emoji, held by ZWJ
PERSIAN = "\u0645\u06cc\u200c\u06a9\u0646\u062f"  # mi-konad, needs its ZWNJ

from telegram_mcp.text_fidelity import (
    _bounded,
    _is_valid_tag_sequence,
    _sequence_starts,
    display_name,
    display_text,
    display_text_status,
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
