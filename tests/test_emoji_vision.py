"""The visual fingerprint that decides which emoji looks like which.

Matching on the GLYPH was tried first and is worthless here - a same-glyph search
offered the Netflix logo for a red circle - so the ranking has to come from the
pictures. These tests pin the properties that make such a ranking trustworthy,
because a similarity score is exactly the kind of thing that looks plausible
while being wrong.

The emoji are drawn rather than downloaded: a shape whose colour, size and
padding can be varied one at a time is what isolates each property, and none of
it needs a network or a real pack.
"""

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import emoji_vision  # noqa: E402


def _disc(colour=(220, 30, 30, 255), size=128, radius=0.40, offset=(0, 0)):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(canvas)
    r = size * radius
    cx, cy = size / 2 + offset[0], size / 2 + offset[1]
    pen.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    return canvas


def _square(colour=(220, 30, 30, 255), size=128, side=0.7):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(canvas)
    half = size * side / 2
    pen.rectangle(
        [size / 2 - half, size / 2 - half, size / 2 + half, size / 2 + half], fill=colour
    )
    return canvas


def _d(a, b):
    return emoji_vision.distance(emoji_vision.fingerprint(a), emoji_vision.fingerprint(b))


def test_an_emoji_is_identical_to_itself():
    assert _d(_disc(), _disc()) == pytest.approx(0.0, abs=1e-9)


def test_the_score_stays_between_zero_and_one():
    for left, right in ((_disc(), _square()), (_disc(), _disc((20, 20, 220, 255)))):
        assert 0.0 <= _d(left, right) <= 1.0


def test_the_score_is_symmetric():
    a, b = _disc(), _square((30, 90, 200, 255))
    assert _d(a, b) == pytest.approx(_d(b, a))


def test_the_same_shape_in_a_different_colour_is_not_a_match():
    """The failure that started this: a red circle ranked against a blue diamond.
    Colour has to carry real weight or every round blob matches every other."""
    same_colour = _d(_disc(), _disc())
    other_colour = _d(_disc((220, 30, 30, 255)), _disc((30, 30, 220, 255)))

    assert other_colour > same_colour + 0.15, "colour barely moved the score"


def test_a_different_shape_in_the_same_colour_is_not_a_match_either():
    assert _d(_disc(), _square()) > 0.05, "shape barely moved the score"


def test_the_same_emoji_drawn_smaller_still_matches():
    """Normalisation, and it is not cosmetic: pack art is not centred or scaled
    consistently, so without it a small glyph on a big canvas never matches the
    same glyph drawn full-bleed."""
    big, small = _disc(radius=0.45), _disc(radius=0.18)

    assert _d(big, small) < 0.12, "the same shape at another size looked like a stranger"


def test_the_same_emoji_shifted_off_centre_still_matches():
    assert _d(_disc(), _disc(offset=(22, -17))) < 0.12


def test_a_fully_transparent_emoji_does_not_crash_the_comparison():
    """A blank render is a real thing here - an animation's first frame often is -
    and it must score badly rather than raise."""
    blank = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    assert 0.0 <= _d(blank, _disc()) <= 1.0
    assert _d(blank, blank) == pytest.approx(0.0, abs=1e-9)


def test_the_score_breaks_down_into_readable_parts():
    """A single number cannot be argued with. The parts say WHY something ranked
    where it did, which is what makes a shortlist reviewable."""
    parts = emoji_vision.distance(
        emoji_vision.fingerprint(_disc()),
        emoji_vision.fingerprint(_square((30, 30, 220, 255))),
        breakdown=True,
    )

    assert set(parts) >= {"total", "silhouette", "colour", "structure"}
    assert all(0.0 <= v <= 1.0 for v in parts.values())


def test_a_fingerprint_survives_a_json_round_trip():
    """The index is cached on disk, so anything not JSON-safe silently degrades
    into a different comparison on the second run."""
    import json

    original = emoji_vision.fingerprint(_disc())
    restored = json.loads(json.dumps(original))

    assert emoji_vision.distance(original, restored) == pytest.approx(0.0, abs=1e-9)
