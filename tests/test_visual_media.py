"""Unit tests for the visual helpers that decode media bytes in memory.

Everything here runs from images and animations built in memory by Pillow, so it
needs neither Telegram nor a window: image encode/fit/open on one side, frame
extraction and its decoder ladder (Pillow, rlottie, ffmpeg) on the other. The
Win32 window capture guards live in test_visual_capture.py.
"""

import gzip
import io
import os
import subprocess
import tempfile

import pytest
from PIL import Image

from telegram_mcp.visual import frames, images
from telegram_mcp.visual.frames import (
    FrameExtractionError,
    extract_frames,
    ffmpeg_available,
    lottie_available,
)
from telegram_mcp.visual.images import (
    MAX_IMAGE_DIMENSION,
    ImageError,
    encode_image,
    fit_image,
    open_image_bytes,
)


def _animated_gif(frame_count=3):
    """A real multi-frame GIF, so the Pillow path runs without ffmpeg."""
    frames = [
        Image.new("RGB", (32, 32), color)
        for color in ("red", "green", "blue", "yellow")[:frame_count]
    ]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=40)
    return buffer.getvalue()


def _animated_webp(frame_count=3):
    """Animated WebP rather than GIF: .gif falls through to ffmpeg on error."""
    frames_ = [
        Image.new("RGB", (32, 32), color) for color in ("red", "green", "blue")[:frame_count]
    ]
    buffer = io.BytesIO()
    frames_[0].save(buffer, format="WEBP", save_all=True, append_images=frames_[1:], duration=40)
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


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("png", "image/png"), ("jpeg", "image/jpeg"), ("webp", "image/webp")],
)
def test_encode_image_round_trips_supported_formats(image_format, mime_type):
    data, meta = encode_image(Image.new("RGB", (120, 60), "red"), image_format=image_format)

    assert meta == {
        "format": image_format,
        "mime_type": mime_type,
        "width": 120,
        "height": 60,
        "bytes": len(data),
    }
    assert open_image_bytes(data).size == (120, 60)


def test_encode_image_rejects_unknown_format():
    with pytest.raises(ImageError, match="Unsupported image format"):
        encode_image(Image.new("RGB", (8, 8)), image_format="tiff")


def test_encode_image_reports_the_downscale_it_applied():
    data, meta = encode_image(Image.new("RGB", (3000, 1000)))

    assert meta["width"] == MAX_IMAGE_DIMENSION
    assert meta["downscaled"] is True
    assert (meta["original_width"], meta["original_height"]) == (3000, 1000)
    assert open_image_bytes(data).width == MAX_IMAGE_DIMENSION


def test_fit_image_downscales_the_long_side_only():
    fitted, resized = fit_image(Image.new("RGB", (3000, 1000)), MAX_IMAGE_DIMENSION)

    assert resized is True
    assert fitted.size == (1568, 523)


def test_fit_image_never_upscales():
    small = Image.new("RGB", (40, 40))
    fitted, resized = fit_image(small, MAX_IMAGE_DIMENSION)

    assert resized is False
    assert fitted is small


def test_open_image_bytes_rejects_garbage():
    with pytest.raises(ImageError, match="Could not decode image data"):
        open_image_bytes(b"definitely not an image")


def test_extract_frames_samples_an_animated_gif_with_pillow():
    frames = extract_frames(_animated_gif(), ".gif", count=2)

    assert len(frames) == 2
    assert [meta["frame_index"] for _, meta in frames] == [0, 2]
    for data, meta in frames:
        assert data.startswith(b"\x89PNG")
        assert meta["frame_count"] == 3
        assert meta["source"] == "pillow"
        assert meta["mime_type"] == "image/png"


def test_extract_frames_deletes_the_file_it_spooled_to_disk(monkeypatch, tmp_path):
    """The media is written to a temp file; the finally-block must remove it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    assert len(extract_frames(_animated_gif(), ".gif", count=2)) == 2

    assert list(tmp_path.iterdir()) == []


def test_extract_frames_points_tgs_at_the_fallbacks_when_rlottie_is_absent(monkeypatch):
    """Without the optional renderer the error must name every way forward."""
    monkeypatch.setattr("telegram_mcp.visual.frames.lottie_available", lambda: False)

    with pytest.raises(FrameExtractionError) as raised:
        extract_frames(b"gzipped-lottie", ".tgs")

    message = str(raised.value)
    assert "telegram-mcp[lottie]" in message
    assert "get_media_thumbnail" in message
    assert "get_telegram_frames" in message


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_extract_frames_reports_an_undecodable_tgs_clearly():
    """With the renderer present, garbage must still surface as FrameExtractionError."""
    with pytest.raises(FrameExtractionError, match="could not open this .tgs"):
        extract_frames(b"not gzipped lottie at all", ".tgs")


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_extract_frames_renders_distinct_frames_from_a_real_lottie():
    """A moving shape must produce different frames, not the same image N times."""
    frames = extract_frames(_animated_tgs(), ".tgs", count=3)

    assert len(frames) == 3
    assert len({png for png, _ in frames}) == 3, "every frame rendered identically"
    for png, meta in frames:
        assert png[1:4] == b"PNG", "not a PNG frame"
        assert meta["source"] == "rlottie"
        assert meta["animation_format"] == "lottie_tgs"
        assert meta["timestamp_seconds"] >= 0


def test_ffmpeg_available_reports_a_bool():
    assert isinstance(ffmpeg_available(), bool)


# --- Regressions for sender-controlled bytes and metadata --------------------


def test_safe_stderr_redacts_a_temp_path_containing_spaces():
    """The old regex stopped at the first space, leaking the surname and the
    temp filename into the model's context."""
    path = r"C:\Users\John Smith\AppData\Local\Temp\tmpab12.webm"
    stderr = f"{path}: Invalid data found when processing input".encode()

    text = frames._safe_stderr(stderr, path)

    assert "Smith" not in text
    assert "tmpab12" not in text
    assert "AppData" not in text
    assert "<temp-file>" in text
    # The diagnostic itself must survive; redaction must not swallow it.
    assert "Invalid data found" in text


def test_ffmpeg_failure_reports_the_first_seek_not_the_last(monkeypatch, tmp_path):
    """`result` after the loop is the furthest-past-EOF seek, i.e. the least
    informative message; and its stderr still carries our temp path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(frames, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(frames, "_probe", lambda path: (None, None))
    messages = iter(
        [
            b"first: moov atom not found",
            b"second: Output file is empty",
            b"third: Output file is empty",
            b"fourth: Output file is empty",
        ]
    )

    def _fake_run(command, timeout):
        # The temp path ffmpeg was handed is the one that must not reach the model.
        spooled = command[command.index("-i") + 1]
        return subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=next(messages) + b" " + spooled.encode()
        )

    monkeypatch.setattr(frames, "_run", _fake_run)

    with pytest.raises(FrameExtractionError) as raised:
        extract_frames(b"not really a video", ".mp4", count=4)

    message = str(raised.value)
    assert "moov atom not found" in message
    assert "Output file is empty" not in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize("hostile", [".webm:ads", ".hta", "." + "a" * 300])
def test_extract_frames_refuses_a_suffix_it_cannot_decode(monkeypatch, hostile):
    """The suffix comes from the sender's mime_type/filename and becomes both a
    real temp filename and the decoder selector."""
    seen = {}

    def _fake_ffmpeg(path, count):
        seen["path"] = path
        return [(b"\x89PNG", {"frame_index": 0})]

    monkeypatch.setattr(frames, "_frames_with_ffmpeg", _fake_ffmpeg)

    extract_frames(b"whatever", hostile, count=1)

    assert seen["path"].endswith(".bin")
    assert "ads" not in seen["path"]


def test_open_image_bytes_refuses_an_oversized_image(monkeypatch):
    """Pillow only warns below 2x its own limit, so ~178M pixels would decode
    and allocate roughly 700 MB in a worker thread."""
    monkeypatch.setattr(images, "MAX_DECODED_PIXELS", 100)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buffer, format="PNG")

    with pytest.raises(ImageError, match="refusing to decode"):
        open_image_bytes(buffer.getvalue())


def test_extract_frames_refuses_an_animation_with_too_many_frames(monkeypatch):
    monkeypatch.setattr(frames, "MAX_ANIMATION_FRAMES", 2)

    with pytest.raises(FrameExtractionError, match="refusing to decode"):
        extract_frames(_animated_webp(3), ".webp", count=2)


# --- the decoder fallback is for capability, not for policy ------------------


def _gif_of_frames(count=4, size=(32, 32)):
    from PIL import Image

    buffer = io.BytesIO()
    images = [Image.new("RGB", size, (index * 60 % 255, 0, 0)) for index in range(count)]
    images[0].save(
        buffer, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0
    )
    return buffer.getvalue()


def _apng_of_frames(count=4, size=(64, 64)):
    from PIL import Image

    buffer = io.BytesIO()
    images = [Image.new("RGBA", size, (index * 60 % 255, 10, 10, 255)) for index in range(count)]
    images[0].save(buffer, format="PNG", save_all=True, append_images=images[1:], duration=100)
    return buffer.getvalue()


def test_the_frame_limit_refusal_is_not_downgraded_into_an_ffmpeg_decode(monkeypatch):
    """.gif is the one suffix Pillow and ffmpeg both claim.

    Catching FrameExtractionError to decide the fallback treated "I refuse to
    decode this" as "I cannot read this format", so ffmpeg decoded the very file
    the guard had just refused — leaving MAX_ANIMATION_FRAMES dead for the most
    common animated format there is.
    """
    monkeypatch.setattr(frames, "MAX_ANIMATION_FRAMES", 2)

    with pytest.raises(frames.FrameExtractionError, match="above the 2 limit"):
        frames.extract_frames(_gif_of_frames(count=4), ".gif", 4)


def test_a_static_gif_keeps_pillows_accurate_diagnosis():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buffer, format="GIF")

    with pytest.raises(frames.FrameExtractionError, match="not animated"):
        frames.extract_frames(buffer.getvalue(), ".gif", 4)


def test_a_truncated_animation_fails_as_a_frame_error_not_a_raw_oserror():
    """The failure happens while decoding a LATER frame, outside Image.open.

    Both a raw OSError and a bare SyntaxError from Pillow's PNG plugin escaped
    every handler in the tool layer. The suffix comes from the sender's mime
    type, so the sender chooses which decoder runs.
    """
    whole = _apng_of_frames()

    for fraction in (0.5, 0.8):
        with pytest.raises(frames.FrameExtractionError):
            frames.extract_frames(whole[: int(len(whole) * fraction)], ".png", 4)


@pytest.mark.skipif(not frames.lottie_available(), reason="rlottie not installed")
def test_an_unparseable_tgs_is_refused_rather_than_rendered_blank():
    """rlottie returns totalframe=0 for garbage instead of raising.

    `or 1` turned that into a legitimate-looking one-frame animation and
    rendered a fully transparent canvas, which the tool layer then labelled a
    successful frame — a blank picture presented as the emoji's real content.
    """
    for payload in (b"nonsense", b"{}"):
        with pytest.raises(frames.FrameExtractionError, match="zero frames"):
            frames.extract_frames(gzip.compress(payload), ".tgs", 3)


# --- the sampling ladder must land on frames that exist ----------------------


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")
def test_every_requested_frame_is_delivered_for_a_short_clip(tmp_path):
    """A container's duration includes the last frame's DISPLAY time.

    Spreading the ladder over the whole duration put the final sample past the
    last frame's PTS, so that seek returned nothing and the caller silently got
    one frame fewer than it asked for, with no metadata naming the loss.

    This must be a real video: a valid .gif is decoded by Pillow and never
    reaches the ffmpeg ladder at all.
    """
    clip = tmp_path / "clip.mp4"
    made = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1.2:size=32x32:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        capture_output=True,
        timeout=60,
    )
    if made.returncode != 0 or not clip.exists():
        pytest.skip("this ffmpeg build cannot synthesise a test clip")

    data = clip.read_bytes()
    for wanted in (4, 8):
        got = frames.extract_frames(data, ".mp4", wanted)
        assert len(got) == wanted, f"asked for {wanted} frames, got {len(got)}"


def test_probe_reports_no_duration_for_the_suffixes_extract_frames_can_produce():
    """The bogus-duration case is unreachable, and this is what keeps it so.

    ffprobe invents a duration for text through demuxers it reaches by extension
    alone - `tty` for `.txt`, and an image demuxer for `.png`. Neither can reach
    the ffmpeg ladder: extract_frames rewrites any unknown suffix to `.bin`, and
    only FFMPEG_SUFFIXES route to _frames_with_ffmpeg at all (`.png` is
    Pillow-only and re-raises rather than falling through). This pins that, so
    widening either set fails here instead of feeding the ladder a guess.
    """
    if not frames.ffmpeg_available():
        pytest.skip("ffprobe ships with ffmpeg")

    text = b"this is not media at all. " * 44
    for suffix in sorted(frames.FFMPEG_SUFFIXES | {".bin"}):
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(text)
            path = handle.name
        try:
            assert frames.probe_duration(path) is None, f"{suffix} produced a guessed duration"
        finally:
            os.unlink(path)
