"""The owner's own emoji, read off disk instead of asked from Telegram.

`Emoji Mapper` exports every pack it builds: one JSON per set beside a `.thumbs`
directory holding a rendered picture per emoji, with an `index.json` naming the
sets. That is the same material `emoji_studio index` used to collect by calling
`get_custom_emoji` about eighty times, and it is better material - the export
carries the glyph, the pack title, the `addemoji` link and the role of each
emoji, none of which a rendered picture can tell you.

The export is REGENERATED as packs change, so nothing here may assume the
previous run's answer is still true. Every emoji carries a `stamp` - its
thumbnail's size and modification time - and `refresh` fingerprints only the
files whose stamp moved, forgets the ones that disappeared, and reports all four
counts. Re-running it after a pack changes costs one `stat` per file.

Pure Pillow and the standard library, matching `emoji_vision`: this repository
has no numpy and none of this needs it.
"""

import json
import os
from pathlib import Path

# Set by whoever runs the export; there is no sensible default path to guess.
ENV_VAR = "TELEGRAM_MCP_EMOJI_PACKS"
THUMBS = ".thumbs"
INDEX = "index.json"
# An animated emoji is exported with a companion still, already picked. Only
# when that is missing does anything here scan frames, and then not all of them.
FRAME_SAMPLES = 6


def find_root(explicit=None):
    """The export directory, from the argument or the environment.

    Refuses rather than guesses: pointing the index at the wrong directory
    silently produces a catalogue of somebody else's emoji.
    """
    candidate = explicit or os.environ.get(ENV_VAR)
    if not candidate:
        raise SystemExit(
            f"No packs directory. Pass --packs-dir or set {ENV_VAR} to the "
            "Emoji Mapper export folder."
        )
    root = Path(candidate)
    if not (root / INDEX).is_file():
        raise SystemExit(f"{root} has no {INDEX} - that is not an Emoji Mapper export.")
    return root


def stamp_of(path):
    """Identity of a thumbnail as far as re-fingerprinting is concerned."""
    info = path.stat()
    return f"{info.st_size}:{info.st_mtime_ns}"


def thumb_for(root, emoji_id):
    """The picture to fingerprint, preferring the export's own chosen still.

    `.webm` is returned when it is all that exists so the caller can report the
    emoji as skipped rather than silently absent; Pillow cannot open one.
    """
    thumbs = root / THUMBS
    for name in (f"{emoji_id}_still.webp", f"{emoji_id}.webp", f"{emoji_id}.webm"):
        candidate = thumbs / name
        if candidate.is_file():
            return candidate
    return None


def open_picture(path):
    """One RGBA frame, choosing the fullest when the file is animated.

    An animation legitimately begins and ends transparent, so frame 0 is a coin
    toss - the same reason `emoji_compose.best_frame` exists for the Telegram
    path. Sampling is capped because this runs over thousands of files.
    """
    from PIL import Image

    picture = Image.open(path)
    count = getattr(picture, "n_frames", 1)
    if count <= 1:
        return picture.convert("RGBA")

    step = max(1, count // FRAME_SAMPLES)
    best, best_weight = None, -1
    for number in range(0, count, step):
        picture.seek(number)
        frame = picture.convert("RGBA")
        # `histogram()` rather than `getdata()`: same total, and getdata is
        # deprecated for removal in Pillow 14.
        weight = sum(
            level * count for level, count in enumerate(frame.getchannel("A").histogram())
        )
        if weight > best_weight:
            best, best_weight = frame, weight
    return best


def read_catalogue(root):
    """Every emoji in the export, keyed by id, with its pack and its picture.

    Returns `(entries, problems)`. A pack listed in `index.json` whose JSON is
    missing or unreadable is a problem, not an exception: the export is written
    while packs are being rebuilt and half of it is still worth indexing.
    """
    index = json.loads((root / INDEX).read_text(encoding="utf-8"))
    entries, problems = {}, []

    for pack in index.get("packs", []):
        set_name = pack.get("set_name")
        if not set_name:
            continue
        source = root / f"{set_name}.json"
        try:
            detail = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as problem:
            problems.append(f"{set_name}: {problem}")
            continue

        for emoji in detail.get("emoji", []):
            # Ids cross JSON as strings on purpose: 64-bit emoji ids do not
            # survive a float, and one wrong digit is a different picture.
            emoji_id = str(emoji.get("custom_emoji_id") or "").strip()
            if not emoji_id:
                continue
            thumb = thumb_for(root, emoji_id)
            entries[emoji_id] = {
                "id": emoji_id,
                "pack": set_name,
                "title": pack.get("title"),
                "family": pack.get("family"),
                "link": pack.get("link"),
                "glyph": emoji.get("glyph"),
                "role": emoji.get("role"),
                "name": emoji.get("name"),
                "format": emoji.get("format"),
                "thumb": thumb,
                "stamp": stamp_of(thumb) if thumb else None,
            }

    return entries, problems


def refresh(stored, entries, fingerprint):
    """Bring a fingerprint cache in line with the export, doing the least work.

    `stored` is mutated in place and also returned. `fingerprint` takes an open
    picture and returns the fingerprint dict, so this stays testable without
    Pillow doing anything real.

    Only entries this function owns (`src == "packs"`) are ever dropped -
    fingerprints collected from Telegram for somebody else's emoji are none of
    its business and survive untouched.
    """
    added = updated = unchanged = skipped = 0
    problems = []

    for emoji_id, entry in entries.items():
        thumb = entry["thumb"]
        if thumb is None or thumb.suffix != ".webp":
            skipped += 1
            continue
        was = stored.get(emoji_id)
        if was is not None and was.get("stamp") == entry["stamp"]:
            unchanged += 1
            continue
        try:
            picture = open_picture(thumb)
        except Exception as problem:  # a truncated export file, mid-rebuild
            problems.append(f"{emoji_id}: {problem}")
            skipped += 1
            continue
        stored[emoji_id] = {
            "src": "packs",
            "pack": entry["pack"],
            "title": entry["title"],
            "link": entry["link"],
            "glyph": entry["glyph"],
            "name": entry["name"],
            "stamp": entry["stamp"],
            **fingerprint(picture),
        }
        if was is None:
            added += 1
        else:
            updated += 1

    gone = [
        key for key, value in stored.items() if value.get("src") == "packs" and key not in entries
    ]
    for key in gone:
        del stored[key]

    return stored, {
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "removed": len(gone),
        "skipped": skipped,
        "problems": problems,
    }
