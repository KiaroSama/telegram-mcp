"""Look at premium emoji quickly, and put the right one into a message.

Written for the workflow this repository keeps landing in: somebody hands over a
message, says which line wants a premium emoji, and the only way to choose one is
to SEE it. The glyph in the text is a fallback, not the picture - a `💳` in these
packs is the PayPal logo, a `✅` is a brand mark, a `😒` is Discord - so every
decision here is made from a rendered image, never from the character.

Five subcommands, all going through the running telegram-mcp HTTP server:

    sheet    a contact sheet of any ids, labelled, for choosing by eye
    gif      one animated emoji as a real GIF, when a still is not enough
    index    fingerprint a pack once, cached, so `nearest` is instant afterwards
    nearest  rank an owner's own emoji by VISUAL distance from a given one
    lines    number a message's lines and say which emoji each already carries

`insert` lives in `emoji_compose.py` with its tests, because getting a UTF-16
offset wrong corrupts a message silently and that has to be pinned down.

The server must be running (see `.ai/memory.md`: nothing starts it for you), and
since it issues a session id every call here does the full MCP handshake first -
a bare `tools/call` answers 400.
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emoji_compose import best_frame, describe_lines  # noqa: E402
import emoji_packs  # noqa: E402
import emoji_vision  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:18765/mcp"
# get_custom_emoji resolves at most this many per call; the server says so and
# refusing to notice just silently drops the tail of a batch.
BATCH = 10
# Frames to sample from an animation before choosing one. A .tgs legitimately
# begins and ends transparent, so one frame is a coin toss and two is not much
# better; six lands somewhere lit for every emoji tried here.
FRAMES = 6


class Studio:
    """One MCP session, reused across calls."""

    def __init__(self, url=DEFAULT_URL, account=None, timeout=300):
        self.url, self.account, self.timeout = url, account, timeout
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        reply, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "emoji-studio", "version": "1"},
                },
            }
        )
        session = reply.headers.get("mcp-session-id")
        if session:
            self._headers["mcp-session-id"] = session
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)

    def _post(self, payload, timeout=None):
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=self._headers
        )
        reply = urllib.request.urlopen(request, timeout=timeout or self.timeout)
        return reply, reply.read().decode("utf-8", "replace")

    def call(self, name, arguments):
        """A tool call, returning its full content list."""
        if self.account and "account" not in arguments:
            arguments = {**arguments, "account": self.account}
        _reply, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        parsed = json.loads(body.split("data: ", 1)[1] if "data: " in body else body)
        if "error" in parsed:
            raise RuntimeError(parsed["error"])
        return parsed["result"]["content"]

    def images(self, ids, size=96, frames=FRAMES):
        """`{id: PIL.Image}` - the best non-blank frame of each.

        The frame choice is the whole reason this is not two lines: rlottie
        renders an animation honestly, and its first and last frames are
        routinely empty by design. Picking one of those is how an emoji ends up
        looking like a blank box and being judged on it.
        """
        from PIL import Image

        out = {}
        ids = [str(i) for i in ids]
        for start in range(0, len(ids), BATCH):
            content = self.call(
                "get_custom_emoji",
                {
                    "document_ids": ids[start : start + BATCH],
                    "max_dimension": size,
                    "count": frames,
                },
            )
            records = json.loads(content[0]["text"])["results"]
            pictures = [c for c in content[1:] if c.get("type") == "image"]
            cursor = 0
            for record in records:
                previews = record.get("preview") or []
                mine = pictures[cursor : cursor + max(len(previews), 1)]
                cursor += max(len(previews), 1)
                if not mine:
                    continue
                chosen = mine[min(best_frame(previews), len(mine) - 1)]
                out[str(record["document_id"])] = Image.open(
                    io.BytesIO(base64.b64decode(chosen["data"]))
                ).convert("RGBA")
        return out

    def meta(self, ids):
        """`{id: record}` without images, for glyphs and pack names."""
        out = {}
        ids = [str(i) for i in ids]
        for start in range(0, len(ids), BATCH):
            content = self.call(
                "get_custom_emoji",
                {"document_ids": ids[start : start + BATCH], "max_dimension": 1, "count": 1},
            )
            for record in json.loads(content[0]["text"])["results"]:
                out[str(record["document_id"])] = record
        return out

    def pack_ids(self, short_name):
        content = self.call("inspect_sticker_set", {"short_name": short_name})
        record = json.loads(content[0]["text"])["results"][0]
        return [str(s["document_id"]) for s in record.get("stickers", [])]


# ----------------------------------------------------------------- rendering


def contact_sheet(images, order, labels=None, columns=8, cell=84, out=None):
    """A labelled grid, which is the artefact a choice actually gets made from."""
    from PIL import Image, ImageDraw

    labels = labels or {}
    gap, foot = 12, 15
    rows = max(1, (len(order) + columns - 1) // columns)
    sheet = Image.new(
        "RGBA",
        (columns * (cell + gap) + gap, rows * (cell + gap + foot) + gap),
        (255, 255, 255, 255),
    )
    pen = ImageDraw.Draw(sheet)
    for n, key in enumerate(order):
        x = gap + (n % columns) * (cell + gap)
        y = gap + (n // columns) * (cell + gap + foot)
        picture = images.get(key)
        if picture:
            sheet.alpha_composite(picture.resize((cell, cell)), (x, y))
        pen.rectangle([x - 2, y - 2, x + cell + 2, y + cell + 2], outline=(205, 205, 205, 255))
        pen.text((x, y + cell + 3), labels.get(key, str(key)[-6:]), fill=(70, 70, 70, 255))
    if out:
        sheet.convert("RGB").save(out)
    return sheet


# -------------------------------------------------------- pictures and packs


class Pictures:
    """Emoji pictures, from the local export where it has them, else Telegram.

    The export answers instantly and offline for the owner's own packs, which is
    almost every question asked here; Telegram still answers for an id from
    somebody else's pack, which is exactly the case `nearest` exists to serve.
    The Studio - and its handshake - is built only if that fallback is reached.
    """

    def __init__(self, make_studio, root=None):
        self._make_studio, self._studio = make_studio, None
        self.entries, self.from_source = {}, {}
        if root is not None:
            self.entries, problems = emoji_packs.read_catalogue(root)
            self.from_source = emoji_packs.source_map(root)
            for problem in problems:
                print(f"packs: {problem}", file=sys.stderr)

    @property
    def studio(self):
        if self._studio is None:
            self._studio = self._make_studio()
        return self._studio

    def get(self, ids, size=96):
        ids = [str(i) for i in ids]
        out, remote = {}, []
        for emoji_id in ids:
            entry = self.entries.get(emoji_id)
            thumb = entry["thumb"] if entry else None
            if thumb is not None and thumb.suffix == ".webp":
                out[emoji_id] = emoji_packs.open_picture(thumb)
            else:
                remote.append(emoji_id)
        if remote:
            out.update(self.studio.images(remote, size=size))
        return out

    def glyph(self, emoji_id):
        """The fallback character, when the export already knows it."""
        entry = self.entries.get(str(emoji_id))
        return (entry or {}).get("glyph")


# -------------------------------------------------------------- subcommands


def _studio(args):
    return Studio(url=args.url, account=args.account)


def _pictures(args):
    """A picture source, wired to the export when one was named or configured."""
    root = None
    if getattr(args, "packs_dir", None) or os.environ.get(emoji_packs.ENV_VAR):
        root = emoji_packs.find_root(getattr(args, "packs_dir", None))
    return Pictures(lambda: _studio(args), root)


def cmd_sheet(args):
    pictures = _pictures(args)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    images = pictures.get(ids, size=args.size)
    # Only an id the export does not know costs a round trip for its glyph.
    unknown = [i for i in ids if i not in pictures.entries]
    facts = pictures.studio.meta(unknown) if unknown else {}
    labels = {
        i: f"{pictures.glyph(i) or (facts.get(i) or {}).get('placeholder', '?')} {i[-6:]}"
        for i in ids
    }
    contact_sheet(images, ids, labels, columns=args.columns, out=args.out)
    print(args.out)


def cmd_gif(args):
    """A real animated preview, for the emoji a still cannot settle."""
    from PIL import Image

    studio = _studio(args)
    content = studio.call(
        "get_custom_emoji",
        {"document_ids": [args.id], "max_dimension": args.size, "count": args.frames},
    )
    pictures = [c for c in content[1:] if c.get("type") == "image"]
    if len(pictures) < 2:
        raise SystemExit(f"{args.id} rendered {len(pictures)} frame(s) - it is not animated.")
    frames = [
        Image.open(io.BytesIO(base64.b64decode(p["data"]))).convert("RGBA") for p in pictures
    ]
    flat = []
    for frame in frames:
        canvas = Image.new("RGBA", frame.size, (255, 255, 255, 255))
        canvas.alpha_composite(frame)
        flat.append(canvas.convert("P", palette=Image.ADAPTIVE))
    flat[0].save(args.out, save_all=True, append_images=flat[1:], duration=args.duration, loop=0)
    print(f"{args.out}  ({len(flat)} frames)")


def cmd_index(args):
    """Fingerprint a pack once. Everything `nearest` does afterwards is local."""
    cache = Path(args.cache)
    stored = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}

    if args.packs_dir or not args.packs:
        return _index_from_packs(args, cache, stored)

    studio = _studio(args)
    for short_name in args.packs.split(","):
        short_name = short_name.strip()
        if not short_name:
            continue
        ids = studio.pack_ids(short_name)
        fresh = [i for i in ids if i not in stored] if not args.rebuild else ids
        print(f"{short_name}: {len(ids)} emoji, {len(fresh)} to fingerprint")
        for start in range(0, len(fresh), BATCH):
            chunk = fresh[start : start + BATCH]
            # Fewer frames than a preview asks for: a fingerprint only needs one
            # lit frame, and this runs over hundreds of emoji.
            for key, picture in studio.images(chunk, size=args.size, frames=args.frames).items():
                stored[key] = {"pack": short_name, **emoji_vision.fingerprint(picture)}
            print(f"  {min(start + BATCH, len(fresh))}/{len(fresh)}", flush=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stored), encoding="utf-8")
    print(f"{len(stored)} fingerprints -> {cache}")


def _index_from_packs(args, cache, stored):
    """Re-index from the Emoji Mapper export. Offline, and cheap to repeat.

    This is the command to run after the export changes: it stats each
    thumbnail, fingerprints only what moved, and forgets what the export no
    longer has. `--rebuild` forces the lot when the fingerprint itself changes.
    """
    root = emoji_packs.find_root(args.packs_dir)
    entries, problems = emoji_packs.read_catalogue(root)
    for problem in problems:
        print(f"packs: {problem}", file=sys.stderr)
    if args.rebuild:
        stored = {k: v for k, v in stored.items() if v.get("src") != "packs"}

    print(f"{root}: {len(entries)} emoji in {len({e['pack'] for e in entries.values()})} packs")
    stored, counts = emoji_packs.refresh(stored, entries, emoji_vision.fingerprint)
    for problem in counts["problems"]:
        print(f"packs: {problem}", file=sys.stderr)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stored), encoding="utf-8")
    print(
        f"added {counts['added']}, updated {counts['updated']}, "
        f"unchanged {counts['unchanged']}, removed {counts['removed']}, "
        f"skipped {counts['skipped']}"
    )
    print(f"{len(stored)} fingerprints -> {cache}")


def cmd_nearest(args):
    """Rank the owner's own emoji by how much they LOOK like a given one.

    The ranking is a shortlist, never a verdict. Glyph equality already proved
    worthless here - a same-glyph search once offered the Netflix logo for a red
    circle - and a fingerprint is only a better guess, not a different KIND of
    evidence. So this always writes a comparison sheet: query on the left, top
    candidates to its right, and the choice is made by looking at it.
    """
    source = _pictures(args)
    cache = Path(args.cache)
    if not cache.exists():
        raise SystemExit(f"No fingerprints at {cache}. Run `index` first.")
    stored = json.loads(cache.read_text(encoding="utf-8"))

    # The export knows exactly which of the owner's emoji a copied one became.
    # An exact answer outranks any ranking, and printing it FIRST is the whole
    # point: three emoji were reported as having no equivalent while this table
    # held their ids.
    exact = source.from_source.get(str(args.id))
    if exact:
        entry = source.entries.get(exact, {})
        print(
            f"EXACT  {args.id} was copied into {exact}  "
            f"{entry.get('glyph') or ''} ({entry.get('title') or '?'})"
        )
        if entry.get("link"):
            print(f"       {entry['link']}")
        print("       ranking below is only for comparison.")

    query = source.get([args.id], size=args.size).get(str(args.id))
    if query is None:
        raise SystemExit(f"No picture for {args.id}, locally or from Telegram.")
    target = emoji_vision.fingerprint(query)
    ranked = emoji_vision.rank(target, stored, k=args.k, exclude={str(args.id)})

    top = [k for _d, k in ranked]
    pictures = source.get(top, size=args.size)
    pictures[str(args.id)] = query
    order = [str(args.id)] + top
    labels = {str(args.id): "QUERY"}
    for distance, key in ranked:
        # Distance and id only: the cell is ~14 characters wide and the default
        # bitmap font has no emoji, so a glyph here draws as an empty box. The
        # glyph, pack and link go to the terminal below, where they render.
        labels[key] = f"{distance:.3f} {key[-6:]}"
    contact_sheet(pictures, order, labels, columns=len(order), out=args.out)
    for score, key in ranked:
        parts = emoji_vision.distance(target, stored[key], breakdown=True)
        entry = stored[key]
        where = entry.get("title") or entry.get("pack", "?")
        glyph = entry.get("glyph") or ""
        print(
            f"{score:.4f}  {key}  {glyph} ({where})  "
            f"silhouette={parts['silhouette']:.3f} colour={parts['colour']:.3f} "
            f"structure={parts['structure']:.3f}"
        )
        if entry.get("link"):
            print(f"          {entry['link']}")
    print(args.out)


def cmd_lines(args):
    """Number the lines of a message and say what is already on each."""
    studio = _studio(args)
    content = studio.call("inspect_message", {"chat_id": args.chat, "message_id": args.message})
    record = json.loads(content[0]["text"])["results"][0]
    for row in describe_lines(record.get("text") or "", record.get("entities")):
        mark = ("  <- " + ", ".join(e[-6:] for e in row["emoji"])) if row["emoji"] else ""
        print(f"{row['line']:>3} | {row['text'][:70]}{mark}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--account", default=None)
    parser.add_argument(
        "--cache", default=str(Path.home() / ".cache" / "telegram-mcp" / "emoji-index.json")
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def packs_dir(target):
        """Subcommand-level so `index --packs-dir X` reads the way it is typed."""
        target.add_argument(
            "--packs-dir",
            default=None,
            help=f"Emoji Mapper export directory (default: ${emoji_packs.ENV_VAR})",
        )

    sheet = sub.add_parser("sheet", help="contact sheet of the given ids")
    packs_dir(sheet)
    sheet.add_argument("--ids", required=True, help="comma separated")
    sheet.add_argument("--out", default="sheet.png")
    sheet.add_argument("--size", type=int, default=96)
    sheet.add_argument("--columns", type=int, default=8)
    sheet.set_defaults(func=cmd_sheet)

    gif = sub.add_parser("gif", help="animated preview of one emoji")
    gif.add_argument("--id", required=True)
    gif.add_argument("--out", default="emoji.gif")
    gif.add_argument("--size", type=int, default=128)
    gif.add_argument("--frames", type=int, default=10)
    gif.add_argument("--duration", type=int, default=80)
    gif.set_defaults(func=cmd_gif)

    index = sub.add_parser("index", help="fingerprint packs for `nearest`")
    packs_dir(index)
    index.add_argument(
        "--packs",
        default=None,
        help="comma separated short names, asked of Telegram; omit to use the export",
    )
    index.add_argument("--size", type=int, default=64)
    index.add_argument("--frames", type=int, default=3)
    index.add_argument("--rebuild", action="store_true")
    index.set_defaults(func=cmd_index)

    nearest = sub.add_parser("nearest", help="visually closest emoji in your packs")
    packs_dir(nearest)
    nearest.add_argument("--id", required=True)
    nearest.add_argument("--k", type=int, default=8)
    nearest.add_argument("--size", type=int, default=96)
    nearest.add_argument("--out", default="nearest.png")
    nearest.set_defaults(func=cmd_nearest)

    lines = sub.add_parser("lines", help="numbered lines of a message")
    lines.add_argument("--chat", required=True)
    lines.add_argument("--message", type=int, required=True)
    lines.set_defaults(func=cmd_lines)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
