"""Unit tests for the visual helpers that need neither Telegram nor a window.

Window capture itself needs a live Telegram Desktop, so only its platform and
argument guards are exercised here; the image and frame helpers are covered end
to end with images built in memory by Pillow.
"""

import importlib.util
import io
import sys
import tempfile

import pytest
from PIL import Image

from telegram_mcp.visual import capture
from telegram_mcp.visual.frames import (
    FrameExtractionError,
    extract_frames,
    ffmpeg_available,
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


def test_extract_frames_points_tgs_stickers_at_the_thumbnail_tool():
    with pytest.raises(FrameExtractionError, match="get_media_thumbnail"):
        extract_frames(b"gzipped-lottie", ".tgs")


def test_ffmpeg_available_reports_a_bool():
    assert isinstance(ffmpeg_available(), bool)


def test_capture_methods_are_stable():
    assert capture.CAPTURE_METHODS == ("window", "screen")


def test_capture_window_rejects_an_unknown_method():
    expected = (
        "Unknown capture method" if sys.platform == "win32" else "only implemented on Windows"
    )

    with pytest.raises(capture.CaptureError, match=expected):
        capture.capture_window(method="hologram")


def test_default_process_name_honours_the_env_override(monkeypatch):
    """Executed as a throwaway copy: reloading the real module in place would
    rebuild CaptureError, and every ``except CaptureError`` imported elsewhere
    (telegram_mcp/tools/visual.py) would stop catching it for the rest of the
    session."""
    untouched = capture.DEFAULT_PROCESS_NAME
    monkeypatch.setenv("TELEGRAM_DESKTOP_PROCESS", "PortableTelegram.exe")
    spec = importlib.util.spec_from_file_location("_capture_env_probe", capture.__file__)
    probe = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module's string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, probe)

    spec.loader.exec_module(probe)

    assert probe.DEFAULT_PROCESS_NAME == "PortableTelegram.exe"
    assert capture.DEFAULT_PROCESS_NAME == untouched
    assert capture.CaptureError is sys.modules["telegram_mcp.visual"].CaptureError


def test_looks_blank_separates_a_flat_capture_from_a_rendered_one():
    blank = Image.new("RGB", (64, 64), "black")
    rendered = Image.new("RGB", (64, 64), "black")
    rendered.paste(Image.new("RGB", (32, 64), "white"), (0, 0))

    assert capture._looks_blank(blank) is True
    assert capture._looks_blank(rendered) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_window_enumeration_is_empty_for_an_unknown_process():
    assert capture.describe_windows("no-such-telegram-build.exe") == []

    with pytest.raises(capture.CaptureError, match="No visible"):
        capture.find_target_window(process_name="no-such-telegram-build.exe")
