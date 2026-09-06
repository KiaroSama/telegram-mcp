"""What a premium emoji LOOKS like, as something two of them can be compared by.

The glyph in a message is a fallback character and says nothing about the picture:
in these packs a `💳` is the PayPal logo, a `✅` is a brand mark, a `😒` is Discord.
A same-glyph search once offered the Netflix logo as the nearest match for a plain
red circle. So "which of my own emoji is closest to this one" has to be answered
from the rendered images, and this is the part that answers it.

Three signals, because each one alone ranks confidently and wrongly:

* **silhouette** - the alpha channel's shape. Emoji art is mostly a coloured form
  on transparency, and the form is the strongest identity signal.
* **colour** - a spatial grid of mean colour over the visible area. Without it
  every round blob matches every other round blob.
* **structure** - local gradient energy, which separates a flat disc from a
  detailed face that happens to share an outline and a palette.

Everything is normalised first: cropped to the visible bounding box, then fitted
into a canonical square. Pack art is not centred or scaled consistently, and
without that step the same emoji drawn small never matches itself drawn large.

Pure Pillow on purpose - this repository has no numpy, and none of this needs it.
Fingerprints are plain lists so they survive the JSON cache unchanged.
"""

from typing import Optional

GRID = 12  # silhouette and structure resolution
COLOUR_GRID = 6  # spatial colour resolution
CANON = 64  # canonical square every emoji is fitted into

# Silhouette leads because shape is the strongest identity signal in emoji art,
# but colour is close behind: the red-circle/blue-diamond failure was a shape-only
# match. Structure is the tie-breaker between a flat form and a detailed one.
WEIGHTS = {"silhouette": 0.40, "colour": 0.40, "structure": 0.20}


def _normalise(picture):
    """Crop to what is actually drawn, then fit a canonical square.

    Aspect ratio is preserved and the remainder padded, so a wide emoji stays wide
    instead of being stretched into a match it does not deserve.
    """
    from PIL import Image

    if picture.mode != "RGBA":
        picture = picture.convert("RGBA")
    box = picture.getchannel("A").getbbox()
    if box:
        picture = picture.crop(box)
    if not picture.width or not picture.height:
        return Image.new("RGBA", (CANON, CANON), (0, 0, 0, 0))

    scale = CANON / max(picture.width, picture.height)
    fitted = picture.resize(
        (max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (CANON, CANON), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, ((CANON - fitted.width) // 2, (CANON - fitted.height) // 2))
    return canvas


def _cells(band, grid):
    """Mean value per cell of a `grid x grid` division, as a flat list.

    `get_flattened_data` where Pillow has it - `getdata()` is deprecated for
    removal in Pillow 14 - falling back so this keeps working on older ones.
    """
    small = band.resize((grid, grid))
    flatten = getattr(small, "get_flattened_data", None)
    if flatten is not None:
        data = list(flatten())
        if data and not isinstance(data[0], tuple):
            bands = len(small.getbands())
            if bands > 1:
                return [tuple(data[i : i + bands]) for i in range(0, len(data), bands)]
        return data
    return list(small.getdata())


def fingerprint(picture) -> dict:
    """A JSON-safe description of one emoji's appearance."""
    from PIL import Image, ImageFilter

    canon = _normalise(picture)
    alpha = canon.getchannel("A")

    # Silhouette: coverage per cell. A plain occupancy map beats a difference hash
    # here because emoji shapes are solid regions, not textures.
    silhouette = [v / 255 for v in _cells(alpha, GRID)]

    # Colour over the VISIBLE area only, composited on mid grey so a light and a
    # dark emoji do not both drift towards whatever background was chosen.
    ground = Image.new("RGBA", canon.size, (128, 128, 128, 255))
    ground.alpha_composite(canon)
    flat = ground.convert("RGB")
    colour = [v / 255 for cell in _cells(flat, COLOUR_GRID) for v in cell]

    # Structure: edge energy, which is what tells a flat disc from a drawn face.
    edges = flat.convert("L").filter(ImageFilter.FIND_EDGES)
    structure = [v / 255 for v in _cells(edges, GRID)]

    return {"silhouette": silhouette, "colour": colour, "structure": structure}


def _mean_abs(left, right) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def distance(left: dict, right: dict, breakdown: bool = False):
    """How different two fingerprints are, in `[0, 1]`, 0 being identical.

    Symmetric by construction. `breakdown=True` returns the parts as well as the
    total, because a single number cannot be argued with and a shortlist has to be
    reviewable: seeing that a candidate scored well on colour and badly on
    silhouette is what makes it obvious the match is a coincidence.
    """
    parts = {name: _mean_abs(left.get(name), right.get(name)) for name in WEIGHTS}
    total = sum(parts[name] * weight for name, weight in WEIGHTS.items())
    total = max(0.0, min(1.0, total))
    if breakdown:
        return {"total": total, **parts}
    return total


def rank(target: dict, index: dict, k: int = 8, exclude: Optional[set] = None) -> list:
    """The `k` closest entries of `{id: fingerprint}`, nearest first.

    A shortlist, never a verdict: the caller renders these beside the query and
    decides by eye. That is the whole discipline this module exists to support.
    """
    exclude = exclude or set()
    scored = [(distance(target, value), key) for key, value in index.items() if key not in exclude]
    scored.sort(key=lambda pair: pair[0])
    return scored[:k]
