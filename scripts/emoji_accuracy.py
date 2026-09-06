"""Measure how good `nearest` actually is, against pairs already known correct.

A similarity ranking is the kind of thing that looks convincing and is wrong, so
this scores it instead of trusting it. The ground truth is free: the Emoji Mapper
catalog records, for each republished emoji, the `premium-id:` it was copied FROM.
Those pairs are the right answer by construction.

The measurement: fingerprint the ORIGINAL, rank it against the owner's whole
index, and ask where its known counterpart landed. Reported as top-1, top-3 and
top-8 hit rates plus the median rank, which is what says whether a shortlist of
eight is worth a person's time.

Not a pytest file: it needs the running server and a built index, so it is a
tool that reports a number rather than a test that fails a build.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emoji_vision  # noqa: E402
from emoji_studio import Studio  # noqa: E402


def known_pairs(catalog: str) -> dict:
    """`{original_id: republished_id}` from the catalog's own keywords."""
    connection = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    pairs = {}
    for own, keywords in connection.execute(
        "select custom_emoji_id, keywords from items where custom_emoji_id is not null"
    ):
        for keyword in json.loads(keywords):
            if keyword.startswith("premium-id:"):
                original = keyword.split(":", 1)[1]
                if original != own:
                    pairs[original] = str(own)
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", required=True)
    parser.add_argument(
        "--cache", default=str(Path.home() / ".cache" / "telegram-mcp" / "emoji-index.json")
    )
    parser.add_argument("--account", default=None)
    parser.add_argument("--sample", type=int, default=40, help="pairs to measure")
    parser.add_argument("--size", type=int, default=64)
    args = parser.parse_args(argv)

    index = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    pairs = {o: t for o, t in known_pairs(args.catalog).items() if t in index}
    sample = sorted(pairs)[: args.sample]
    print(
        f"index: {len(index)} emoji   usable known pairs: {len(pairs)}   measuring: {len(sample)}"
    )

    studio = Studio(account=args.account)
    ranks, misses = [], []
    for start in range(0, len(sample), 10):
        chunk = sample[start : start + 10]
        for original, picture in studio.images(chunk, size=args.size, frames=3).items():
            target = emoji_vision.fingerprint(picture)
            ordered = emoji_vision.rank(target, index, k=len(index))
            wanted = pairs[original]
            position = next((n for n, (_s, key) in enumerate(ordered, 1) if key == wanted), None)
            if position is None:
                misses.append(original)
            else:
                ranks.append(position)
        print(f"  {min(start + 10, len(sample))}/{len(sample)}", flush=True)

    if not ranks:
        print("no pair could be scored")
        return
    for cut in (1, 3, 8, 20):
        hit = sum(1 for r in ranks if r <= cut)
        print(f"top-{cut:<2}: {hit}/{len(ranks)}  ({100 * hit / len(ranks):.0f}%)")
    print(f"median rank: {statistics.median(ranks):.0f}   worst: {max(ranks)}")
    if misses:
        print(f"unresolvable originals: {len(misses)}")


if __name__ == "__main__":
    main()
