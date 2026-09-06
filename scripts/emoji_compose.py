"""Putting a premium emoji into a message, and choosing a frame worth looking at.

The two pieces of this workflow that are pure functions of their input, kept
apart from the network so they can be tested without one. Both fail SILENTLY
when they are wrong, which is the reason they live here rather than inline in a
CLI:

* An entity `offset` counts UTF-16 code units into the message. Insert a glyph
  and every entity starting after it has to move. Telegram accepts a message with
  stale offsets without complaint and simply renders the bold run, the link or the
  next emoji over the wrong characters.
* An animated `.tgs` legitimately begins and ends transparent - ``visual/frames.py``
  measured Telegram's own fire effect at zero visible pixels in frame 0 of 181 and
  eight in frame 180 - so frame 0 and the last frame are both bad choices for a
  preview, and the emoji ends up judged as an empty box.
"""

from typing import Iterable, Optional

# Entities that wrap a whole block of text rather than marking a span inside it.
# The distinction only matters for a run that ends exactly where a glyph is being
# inserted: these take it in, everything else leaves it outside. See `insert_emoji`.
BLOCK_ENTITIES = {"blockquote", "pre", "code"}


def u16_len(text: str) -> int:
    """Length in UTF-16 code units, which is the unit Telegram counts offsets in.

    `len()` is not it: a non-BMP glyph is one Python character and TWO of these.
    """
    return len(text.encode("utf-16-le")) // 2


def insert_emoji(
    text: str,
    entities: Iterable[dict],
    line: int,
    glyph: str,
    document_id,
    where: str = "end",
) -> tuple:
    """`(text, entities)` with `glyph` added to `line`, carrying a custom emoji.

    Args:
        text: The whole message.
        entities: Its current entities, in the shape the reading tools publish.
        line: 1-based. Refused by name when the message has no such line, because
            an off-by-one here silently decorates the wrong sentence.
        glyph: The emoji's own fallback character, from `get_custom_emoji`'s
            `placeholder`. It is what a non-Premium reader sees, so it is not
            cosmetic.
        document_id: The custom emoji. Returned as a STRING whatever comes in -
            these ids exceed 2**53 and a JSON number turns one into a different,
            existing emoji rather than into an error.
        where: `"end"` (default) or `"start"` of that line.

    Every existing entity is adjusted: one starting at or after the insertion
    point moves by the glyph's width, one straddling it grows instead, one ending
    before it is left alone.

    A run ending EXACTLY at the insertion point is the ambiguous case, and block
    and inline entities want opposite answers. A blockquote around the line must
    take the emoji in, or it visibly stops one glyph short of its own last line.
    A `text_url` must not, or the emoji becomes part of the link and a reader can
    tap it. So the two are split by kind rather than by one rule that is wrong
    half the time.
    """
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        raise ValueError(
            f"The message has {len(lines)} line(s); line {line} does not exist. "
            "Lines are 1-based and split on newlines."
        )

    before = "\n".join(lines[: line - 1])
    prefix_len = u16_len(before + "\n") if line > 1 else 0
    at = prefix_len + (0 if where == "start" else u16_len(lines[line - 1]))
    width = u16_len(glyph)

    moved = []
    for item in entities or []:
        item = dict(item)
        start = item.get("offset")
        length = item.get("length")
        if start is not None and length is not None:
            end = start + length
            if start >= at:
                item["offset"] = start + width
            elif end > at or (end == at and item.get("type") in BLOCK_ENTITIES):
                # Straddles the insertion, or is a block that must take it in.
                item["length"] = length + width
        moved.append(item)

    moved.append(
        {
            "type": "custom_emoji",
            "offset": at,
            "length": width,
            "custom_emoji_id": str(document_id),
        }
    )
    moved.sort(key=lambda e: (e.get("offset") if e.get("offset") is not None else 0))
    return text[: _py_index(text, at)] + glyph + text[_py_index(text, at) :], moved


def _py_index(text: str, u16_offset: int) -> int:
    """A Python string index for a UTF-16 offset.

    The two agree only while every character is BMP, and this workflow is entirely
    about emoji, which are not.
    """
    if u16_offset <= 0:
        return 0
    encoded = text.encode("utf-16-le")[: u16_offset * 2]
    return len(encoded.decode("utf-16-le", "ignore"))


def best_frame(frames: Iterable[dict]) -> int:
    """Index of the frame actually worth showing.

    Preference order: not blank, then largest encoded size, which stands in for
    "most going on" without decoding anything. `frames.py` sets `blank` itself by
    scanning the alpha channel, so this trusts that flag rather than guessing from
    the bytes alone.

    Never raises and never returns None: an emoji whose frames are all blank still
    gets an index, so a caller renders an honest empty box instead of crashing a
    contact sheet over one bad animation.
    """
    frames = list(frames or [])
    if not frames:
        return 0
    lit = [i for i, f in enumerate(frames) if not f.get("blank")]
    if not lit:
        return 0
    return max(lit, key=lambda i: frames[i].get("bytes") or 0)


def line_of_offset(text: str, u16_offset: int) -> int:
    """The 1-based line an offset falls on - the inverse lookup, for reporting
    which line an existing emoji sits on."""
    return text[: _py_index(text, u16_offset)].count("\n") + 1


def describe_lines(text: str, entities: Optional[Iterable[dict]] = None) -> list:
    """One record per line: its number, its text, and the emoji already on it.

    What a caller reads before deciding where to add one.
    """
    on_line: dict = {}
    for item in entities or []:
        if item.get("type") == "custom_emoji" and item.get("offset") is not None:
            on_line.setdefault(line_of_offset(text, item["offset"]), []).append(
                str(item.get("custom_emoji_id"))
            )
    return [
        {"line": n, "text": body, "emoji": on_line.get(n, [])}
        for n, body in enumerate(text.split("\n"), 1)
    ]
