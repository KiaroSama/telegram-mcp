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
# Frames sampled from an animation before one is chosen. An animation
# legitimately begins and ends transparent, so one frame is a coin toss.
FRAME_SAMPLES = 6
# A `.webm` emoji is at most three seconds at a low frame rate, so this reads
# the whole of a short one and a fair spread of a longer one.
VIDEO_FRAMES = 30


def ffmpeg_path():
    """Where ffmpeg is, or None. Looked up per call so a test can remove it."""
    import shutil

    return shutil.which("ffmpeg")


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
    """The picture to fingerprint, preferring the ANIMATION over the still.

    The export writes `<id>_still.webp` beside an animated `<id>.webp`, and that
    still is whatever frame the exporter picked. 66 of this export's 375 are
    under 600 bytes - all but empty - and one of them is the shopping cart the
    owner knew was in their pack while this index served a blank square for it.
    Scanning the animation costs six frames and cannot make that mistake, so the
    still is only a fallback for an emoji that has no animation at all.
    """
    thumbs = root / THUMBS
    for name in (f"{emoji_id}.webp", f"{emoji_id}_still.webp", f"{emoji_id}.webm"):
        candidate = thumbs / name
        if candidate.is_file():
            return candidate
    return None


def _weight(frame):
    """Total visible alpha - how much of the frame is actually drawn.

    `histogram()` rather than `getdata()`: same total, and getdata is deprecated
    for removal in Pillow 14.
    """
    return sum(level * count for level, count in enumerate(frame.getchannel("A").histogram()))


def _video_frames(path, limit=VIDEO_FRAMES):
    """Frames of a `.webm` emoji as RGBA, with its transparency intact.

    **`-c:v libvpx-vp9` must come BEFORE `-i`.** VP9 keeps alpha in a separate
    layer that ffmpeg's default decoder silently discards, so the emoji decodes
    as an opaque square - which then ranks confidently and wrongly. The same
    flag is needed to MEASURE it: probing with the default decoder reports every
    VP9 emoji as opaque, including the correct ones. Both facts were paid for in
    the sibling Emoji Mapper project; see `.ai/LESSON.md`.
    """
    import subprocess
    import tempfile

    from PIL import Image

    if not ffmpeg_path():
        return []
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run(
            [
                ffmpeg_path(),
                "-v",
                "error",
                "-c:v",
                "libvpx-vp9",
                "-i",
                str(path),
                "-frames:v",
                str(limit),
                "-fps_mode",
                "passthrough",
                str(Path(scratch) / "%03d.png"),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or b"").decode("utf-8", "replace").strip()[:200])
        # Loaded eagerly: the directory is gone by the time the caller looks.
        return [Image.open(f).convert("RGBA") for f in sorted(Path(scratch).glob("*.png"))]


def open_picture(path):
    """One RGBA frame, choosing the fullest when the file is animated.

    An animation legitimately begins and ends transparent, so frame 0 is a coin
    toss - the same reason `emoji_compose.best_frame` exists for the Telegram
    path. Sampling is capped because this runs over thousands of files.
    """
    from PIL import Image

    if path.suffix == ".webm":
        frames = _video_frames(path)
        if not frames:
            raise RuntimeError("no ffmpeg, or it produced no frames")
        return max(frames, key=_weight)

    picture = Image.open(path)
    count = getattr(picture, "n_frames", 1)
    if count <= 1:
        return picture.convert("RGBA")

    step = max(1, count // FRAME_SAMPLES)
    best, best_weight = None, -1
    for number in range(0, count, step):
        picture.seek(number)
        frame = picture.convert("RGBA")
        weight = _weight(frame)
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


def source_map(root):
    """`{id it had in its ORIGINAL pack: the owner's id}` from the export.

    This is an EXACT answer where it has one, and it outranks anything the
    visual ranker can offer. Skipping it is how a shopping cart, a megaphone and
    a red circle were all reported as "no equivalent in your packs" while the
    export knew precisely which emoji each had become. Only emoji the owner
    COPIED from somewhere have a row here - about 800 of 6739 - so a miss means
    "not copied", never "not present".
    """
    index = json.loads((root / INDEX).read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (index.get("by_source_id") or {}).items()}


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
        if thumb is None or (thumb.suffix == ".webm" and not ffmpeg_path()):
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
