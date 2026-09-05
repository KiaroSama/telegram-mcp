"""Sample media the visual tests decode, built in memory.

Shared because the visual suite is split in two - what the decoders PRODUCE
(`test_visual_media.py`) and how a decode is BOUNDED (`test_visual_budget.py`)
- and both halves need the same three samples. Kept as one module rather than
duplicated so a fixture fixed in one place is fixed for both.
"""

import io
import subprocess

import pytest
from PIL import Image

# ffmpeg is a child process like any other: bounded, so a wedged encoder fails
# the helper instead of stalling whichever suite asked for the sample.
FFMPEG_TIMEOUT_SECONDS = 120


def _animated_gif(frame_count=3):
    """A real multi-frame GIF, so the Pillow path runs without ffmpeg."""
    frames = [
        Image.new("RGB", (32, 32), color)
        for color in ("red", "green", "blue", "yellow")[:frame_count]
    ]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=40)
    return buffer.getvalue()


def _animated_tgs():
    """A real .tgs: one square whose opacity animates, so frames differ."""
    import gzip
    import json

    lottie = {
        "v": "5.5.7",
        "fr": 30,
        "ip": 0,
        "op": 30,
        "w": 64,
        "h": 64,
        "nm": "t",
        "ddd": 0,
        "assets": [],
        "layers": [
            {
                "ddd": 0,
                "ind": 1,
                "ty": 1,
                "nm": "solid",
                "sr": 1,
                "sc": "#ff0000",
                "sw": 64,
                "sh": 64,
                "ks": {
                    "o": {
                        "a": 1,
                        "k": [
                            {
                                "t": 0,
                                "s": [100],
                                "i": {"x": [1], "y": [1]},
                                "o": {"x": [0], "y": [0]},
                            },
                            {"t": 29, "s": [0]},
                        ],
                    },
                    "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [32, 32, 0]},
                    "a": {"a": 0, "k": [32, 32, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
                "ao": 0,
                "ip": 0,
                "op": 30,
                "st": 0,
                "bm": 0,
            }
        ],
    }
    return gzip.compress(json.dumps(lottie).encode())


def _transparent_vp9(directory):
    """A one-second VP9 clip whose right half is fully transparent."""
    source = directory / "alpha.png"
    image = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    for x in range(32, 64):
        for y in range(64):
            image.putpixel((x, y), (0, 0, 0, 0))
    image.save(source)

    clip = directory / "alpha.webm"
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(source),
            "-t",
            "1",
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",
            str(clip),
        ],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not clip.exists():
        pytest.skip("this ffmpeg cannot encode VP9 with alpha")
    return clip
