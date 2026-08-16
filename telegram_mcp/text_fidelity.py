"""Telegram text made safe to show an agent, without destroying what it says.

Upstream's sanitizer is built for filenames: it strips anything unusual, which
also strips the ZWJ out of a family emoji and the ZWNJ out of a Persian word,
so a name stops rendering as itself. This module keeps everything that renders
and removes only what hides or spoofs — and reports offsets so a caller can map
Telegram's UTF-16 entity positions onto the cleaned text.

No Telethon here on purpose: these are string rules, testable without a client
and without a message. :mod:`telegram_mcp.message_view` layers the Telegram
shapes on top.
"""

from __future__ import annotations

import re
import unicodedata

import regex
from typing import Optional

_UNSAFE_INVISIBLES = frozenset(
    "​"  # ZERO WIDTH SPACE
    "⁠"  # WORD JOINER
    "﻿"  # ZERO WIDTH NO-BREAK SPACE / BOM
    "᠎"  # MONGOLIAN VOWEL SEPARATOR - zero width in modern Unicode
    "‪‫‬‭‮"  # LRE RLE PDF LRO RLO
    "⁦⁧⁨⁩"  # LRI RLI FSI PDI
    "⁡⁢⁣⁤"  # invisible maths operators: function application,
    # times, separator, plus - render as nothing at all
    "￹￺￻"  # interlinear annotation anchor/separator/terminator, which
    # hide the text between them from the reader
)

# Deliberately NOT removed: ZWNJ (U+200C) and ZWJ (U+200D) are ordinary letters'
# business in Persian/Arabic ("می‌کند") and in emoji sequences ("👨‍👩‍👧"), and the
# LRM/RLM marks (U+200E/U+200F) are how mixed-direction text is written. Stripping
# them, as the generic sanitizer does, corrupts legitimate Telegram messages.


# Everything Unicode treats as a line break. CRLF first so it collapses to one
# space rather than two.
_LINE_SEPARATORS = ("\r\n", "\r", "\n", "\t", "\v", "\f", "\x85", " ", " ")


# Multi-line fields need the opposite of _LINE_SEPARATORS: keep the break, do not
# flatten it to a space. CR, NEL, VT and FF are all Cc, so the fidelity pass would
# DELETE them and glue the words on either side together ("line one\rline two" ->
# "line onelinetwo"). Map them onto the one break character that pass keeps. CRLF
# first, so it collapses to one break rather than two. U+2028/U+2029 are Zl/Zp and
# survive the pass untouched, so they need no mapping.
_BREAK_SEPARATORS = ("\r\n", "\r", "\x85", "\v", "\f")


def _normalize_breaks(raw: Optional[str]) -> str:
    text = raw or ""
    for separator in _BREAK_SEPARATORS:
        text = text.replace(separator, "\n")
    return text


def _is_unsafe_char(char: str) -> bool:
    if char in _UNSAFE_INVISIBLES:
        return True
    if char in ("\n", "\t"):
        return False
    return unicodedata.category(char) == "Cc"


# Emoji tag sequences, per UTS #51 ED-14a:
#     emoji_tag_sequence := tag_base tag_spec tag_end
# tag_base is the black flag and tag_end is TAG CANCEL. UTS #51 defines tag_spec
# broadly and leaves which sequences are actually valid to Annex C / CLDR, so
# syntax alone proves nothing: "us01" is well formed and is not a subdivision.
# The bounds are enforced below, then membership of the RGI set. Anywhere else a
# tag character is invisible, which makes it a place to hide text.
_TAG_BASE = "\U0001f3f4"
_TAG_END = "\U000e007f"
# UTS #51 well-formedness: tag_spec is one or more characters from U+E0020..U+E007E
# and the whole emoji tag sequence is limited to 32 code points.
_TAG_SPEC_CODES = frozenset(range(0xE0020, 0xE007F))
_TAG_SEQUENCE_MAX = 32

# Well-formed is not the same as displayable. Only RGI ("recommended for general
# interchange") tag sequences render as a flag anywhere; everything else is
# invisible, which makes it a place to hide text. This fork therefore accepts the
# RGI set only, listed explicitly rather than guessed from syntax: a syntactically
# perfect "us01" is not a subdivision and renders as nothing. Extend this set when
# Unicode adds a sequence — deliberately narrower than the syntax allows.
_RGI_TAG_SPECS = frozenset({"gbeng", "gbsct", "gbwls"})


def _is_tag_char(char: str) -> bool:
    return 0xE0020 <= ord(char) <= 0xE007F


def _stray_tag_indexes(raw: str) -> set[int]:
    """Indexes of tag characters that are not part of a valid emoji tag sequence."""
    stray: set[int] = set()
    index = 0
    length = len(raw)
    while index < length:
        if not _is_tag_char(raw[index]):
            index += 1
            continue
        start = index
        while index < length and _is_tag_char(raw[index]):
            index += 1
        if not _is_valid_tag_sequence(raw, start, index):
            stray.update(range(start, index))
    return stray


def _is_valid_tag_sequence(raw: str, start: int, end: int) -> bool:
    """Is ``raw[start:end]`` an RGI emoji tag sequence following its tag_base?

    Two gates. First UTS #51 well-formedness: the black flag base, a tag_spec of
    characters from U+E0020..U+E007E, TAG CANCEL, and at most 32 code points
    overall. Then RGI membership, because only those actually render — see
    :data:`_RGI_TAG_SPECS`.
    """
    if start == 0 or raw[start - 1] != _TAG_BASE:
        return False
    run = raw[start:end]
    if not run.endswith(_TAG_END):
        return False
    if 1 + len(run) > _TAG_SEQUENCE_MAX:  # the base counts toward the limit
        return False
    spec = run[:-1]
    if not spec or not all(ord(char) in _TAG_SPEC_CODES for char in spec):
        return False
    decoded = "".join(chr(ord(char) - 0xE0000) for char in spec)
    return decoded in _RGI_TAG_SPECS


def fidelity_text(raw: Optional[str]) -> tuple[str, list[int]]:
    """Return ``(text, offset_map)`` preserving Telegram's own character positions.

    The generic ``sanitize_user_content`` strips every Cf character, collapses runs
    of newlines and truncates — all of which change the string's length, so entity
    offsets computed against Telegram's raw text no longer index the text the
    caller actually sees. This keeps the text intact apart from genuinely unsafe
    invisibles, and returns a map so the offsets can be rebased onto the result.

    ``offset_map[i]`` is the UTF-16 index in the returned text corresponding to
    UTF-16 index ``i`` in ``raw``; it has one extra trailing entry so a slice end
    can be mapped too.
    """
    raw = raw or ""
    kept: list[str] = []
    offset_map: list[int] = []
    clean_units = 0
    stray_tags = _stray_tag_indexes(raw)

    for index, char in enumerate(raw):
        units = 2 if ord(char) > 0xFFFF else 1  # non-BMP occupies a surrogate pair
        if index in stray_tags or _is_unsafe_char(char):
            offset_map.extend([clean_units] * units)
            continue
        offset_map.extend(clean_units + step for step in range(units))
        kept.append(char)
        clean_units += units

    offset_map.append(clean_units)
    return "".join(kept), offset_map


_ZWJ = "‍"
_ZWNJ = "‌"


def _sequence_starts(text: str) -> list[int]:
    r"""Indices where a new user-perceived sequence begins.

    UAX #29 extended grapheme clusters via ``regex``'s ``\X``, which is the only
    way to get this right: a hand-rolled scan kept missing whole categories — emoji
    modifiers (``👍🏽`` is one character, and the skin tone is ``Sk``), Hangul jamo,
    Indic conjuncts.

    One documented addition on top of the standard: UAX #29 attaches a ZWNJ to the
    cluster before it but starts a new cluster after it, so ``می‌کند`` would still be
    splittable at the joiner. Telegram displays that as one word, so a cluster
    ending in ZWNJ is merged with the next.
    """
    clusters = regex.findall(r"\X", text)
    starts: list[int] = []
    position = 0
    for cluster in clusters:
        # Merge across a ZWNJ: the joiner binds both sides for display purposes.
        if not (position and text[position - 1] == _ZWNJ):
            starts.append(position)
        position += len(cluster)
    return starts


def _bounded(text: str, max_length: int) -> tuple[str, bool]:
    """Return ``(text, truncated)`` cut on a sequence boundary, ellipsis included.

    A raw slice happily lands inside a family emoji, between a base letter and its
    combining mark, or halfway through a subdivision flag's tag sequence — which
    is exactly what these helpers promise not to do. The result therefore contains
    whole sequences only: a sequence that does not fit is dropped entirely.
    """
    if max_length <= 0:
        return "", bool(text)
    if len(text) <= max_length:
        return text, False

    budget = max_length - 1  # the ellipsis occupies one
    cut = 0
    for start in _sequence_starts(text):
        if start > budget:
            break
        cut = start
    return text[:cut].rstrip() + "…", True


def display_name(raw: Optional[str], max_length: int = 256) -> str:
    """Single-line display text that keeps compound Unicode intact.

    The same job as the generic ``sanitize_name`` — strip control characters,
    force one line, bound the length — without its habit of deleting every ``Cf``
    character. That habit breaks a family emoji into three separate people, turns
    Persian ``می‌کند`` into two words, and reduces a regional flag to nothing,
    because ZWJ, ZWNJ and the tag characters behind flags are all ``Cf``.

    Use this for chat titles, window titles and emoji placeholders — anything the
    caller is meant to read back as the user wrote it.
    """
    # Every line separator becomes a space *before* the fidelity pass. CR and NEL
    # are Cc, so leaving them would delete the separator and glue two words
    # together; LINE SEPARATOR and PARAGRAPH SEPARATOR are Zl/Zp and would survive
    # untouched, leaving a "single-line" name that still renders on two lines.
    text = raw or ""
    for separator in _LINE_SEPARATORS:
        text = text.replace(separator, " ")
    text, _offsets = fidelity_text(text)
    text = re.sub(r" {2,}", " ", text).strip()
    bounded, _truncated = _bounded(text, max_length)
    return bounded


def display_text_status(raw: Optional[str], max_length: int = 4096) -> tuple[str, bool]:
    """``(text, truncated)`` — the same cleaning as :func:`display_text`.

    Callers that must report truncation need it stated, not guessed: a quote that
    genuinely ends in an ellipsis is indistinguishable from a truncated one by
    looking at the result.
    """
    filtered, _offsets = fidelity_text(_normalize_breaks(raw))
    return _bounded(filtered, max_length)


def display_text(raw: Optional[str], max_length: int = 4096) -> str:
    """Multi-line display text that keeps compound Unicode intact.

    :func:`display_name` for values that are legitimately more than one line — a
    poll question, a quoted fragment, a button label. Line breaks survive; the
    unsafe invisibles do not.
    """
    text, _truncated = display_text_status(raw, max_length)
    return text
