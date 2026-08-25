"""Unit tests for the visual helpers that decode media bytes in memory.

Everything here runs from images and animations built in memory by Pillow, so it
needs neither Telegram nor a window: image encode/fit/open on one side, frame
extraction and its decoder ladder (Pillow, rlottie, ffmpeg) on the other. The
Win32 window capture guards live in test_visual_capture.py.
"""

import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from telegram_mcp.visual import bounded_process, frames, images
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
def test_a_transparent_lottie_frame_says_it_is_the_content_not_a_failure():
    """A message effect begins and ends on an empty canvas.

    Measured on Telegram's own fire effect: frame 0 of 181 had zero visible
    pixels while the middle frames had 19-45%. Handed a blank image beside full
    ones with nothing to explain it, a caller concludes the render failed - the
    wrong conclusion about a correct render.
    """
    import gzip
    import json as _json

    # A one-layer animation whose opacity is 0 at the first keyframe.
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
                "ao": 0,
                "ip": 0,
                "op": 30,
                "st": 0,
                "bm": 0,
                "ks": {
                    "o": {
                        "a": 1,
                        "k": [
                            {
                                "t": 0,
                                "s": [0],
                                "i": {"x": [1], "y": [1]},
                                "o": {"x": [0], "y": [0]},
                            },
                            {"t": 29, "s": [100]},
                        ],
                    },
                    "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [32, 32, 0]},
                    "a": {"a": 0, "k": [32, 32, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
            }
        ],
    }
    payload = gzip.compress(_json.dumps(lottie).encode())

    got = extract_frames(payload, ".tgs", count=3)

    first = got[0][1]
    assert first["blank"] is True, "an empty first frame was reported as ordinary content"
    assert "not a failed render" in first["blank_note"]
    # The frames that do carry content must NOT be flagged.
    assert not any(
        meta.get("blank") for _data, meta in got[1:]
    ), "a visible frame was flagged blank"


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
    # (duration, frame_rate, codec) - the codec joined when the extractor had to
    # decide whether to name the VP9 decoder.
    monkeypatch.setattr(
        frames, "_probe", lambda path, deadline=None, cancelled=None: (None, None, None)
    )
    messages = iter(
        [
            b"first: moov atom not found",
            b"second: Output file is empty",
            b"third: Output file is empty",
            b"fourth: Output file is empty",
        ]
    )

    def _fake_run(command, timeout, deadline=None, cancelled=None):
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

    def _fake_ffmpeg(path, count, budget=None, max_side=None):
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
    )
    if result.returncode != 0 or not clip.exists():
        pytest.skip("this ffmpeg cannot encode VP9 with alpha")
    return clip


@pytest.mark.skipif(not frames.ffmpeg_available(), reason="ffmpeg is not on PATH")
def test_a_transparent_video_sticker_keeps_its_alpha(tmp_path):
    """ffmpeg's DEFAULT vp9 decoder drops the separate WebM alpha layer, silently.

    Measured before the fix: the extractor returned mode=RGB with zero transparent
    pixels for this clip - a transparent sticker previewed as an opaque square and
    nothing anywhere reported a problem. `-c:v libvpx-vp9` before `-i` keeps it.
    """
    clip = _transparent_vp9(tmp_path)

    extracted = frames._frames_with_ffmpeg(str(clip), 1)

    assert extracted, "no frame came back"
    rendered = Image.open(io.BytesIO(extracted[0][0]))
    assert rendered.mode == "RGBA", f"alpha was dropped: mode={rendered.mode}"
    transparent = sum(
        1 for value in rendered.convert("RGBA").getchannel("A").get_flattened_data() if value == 0
    )
    assert transparent == 32 * 64, f"expected the right half clear, got {transparent} px"


@pytest.mark.skipif(not frames.ffmpeg_available(), reason="ffmpeg is not on PATH")
def test_naming_the_vp9_decoder_does_not_break_an_h264_source(tmp_path):
    """`-c:v` applies to every input, so it must not be passed unconditionally.

    Video notes are h264; libvpx-vp9 cannot decode them at all, so an unguarded
    flag would trade one silent defect for a loud one.
    """
    import subprocess

    clip = tmp_path / "note.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )

    assert frames._probe(str(clip))[2] == "h264"
    assert len(frames._frames_with_ffmpeg(str(clip), 2)) == 2


# --- one request, one decoding budget ------------------------------------------


# Real children rather than a fake Popen. What these assert is that nothing
# SURVIVES a bound, and a double can only show that kill() was called - never
# that the process is gone. The mechanics now live in
# telegram_mcp.visual.bounded_process (the window capture shares them); these
# check that frames._run still translates each bound into the error type the tool
# layer already handles.
_HANGS = "import time\nwhile True: time.sleep(0.05)\n"


def _hanging_child():
    return [sys.executable, "-c", _HANGS]


def _watch_children(monkeypatch):
    """Record every child bounded_process starts, so the test can check it died."""
    created = []
    original = bounded_process.subprocess.Popen

    def _factory(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(bounded_process.subprocess, "Popen", _factory)
    return created


def test_a_decode_is_bounded_by_the_request_budget_not_just_its_own_timeout(monkeypatch):
    """Ten frames at 30s each, after a 15s probe, is 315 seconds one caller can ask
    for. A per-call timeout bounds a subprocess; only a shared deadline bounds the
    request, so the smaller of the two has to win."""
    created = _watch_children(monkeypatch)

    started = time.monotonic()
    with pytest.raises(frames.FrameExtractionError, match="timed out"):
        frames._run(_hanging_child(), timeout=30, deadline=time.monotonic() + 0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"waited {elapsed:.1f}s: the 30s per-call timeout won over the budget"
    assert created[0].poll() is not None, "the child outlived the call that started it"


def test_a_call_with_no_deadline_still_honours_its_own_timeout(monkeypatch):
    created = _watch_children(monkeypatch)

    started = time.monotonic()
    with pytest.raises(frames.FrameExtractionError, match="timed out"):
        frames._run(_hanging_child(), timeout=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"waited {elapsed:.1f}s for a 0.3s timeout"
    assert created[0].poll() is not None


def test_an_exhausted_budget_refuses_instead_of_starting_another_decoder(monkeypatch):
    """Past the deadline nothing should be launched at all."""
    created = _watch_children(monkeypatch)

    with pytest.raises(frames.FrameExtractionError, match="budget"):
        frames._run(["ffmpeg"], timeout=30, deadline=time.monotonic() - 1)

    assert created == [], "a decoder was started after the budget was already gone"


def test_a_cancelled_decode_terminates_its_subprocess(monkeypatch):
    """Cancelling the coroutine cannot kill the worker thread the decode runs on, so
    the thread has to look for itself. Without this the process kept burning CPU
    until its own timeout fired - up to the whole request budget after everyone had
    stopped waiting for the answer."""
    created = _watch_children(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()

    started = time.monotonic()
    with pytest.raises(frames.DecodingCancelled, match="cancelled"):
        frames._run(
            _hanging_child(), timeout=600, deadline=time.monotonic() + 600, cancelled=cancelled
        )
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"took {elapsed:.1f}s to notice a cancellation already set"
    assert created[0].poll() is not None, "the subprocess survived the cancellation"
    assert created[0].stdout.closed, "the killed child was never reaped"


def test_a_decoder_that_floods_the_pipe_is_stopped_while_it_writes(monkeypatch):
    """The byte ceiling has to bound the BUFFER, not describe it afterwards. With
    communicate() the whole reply arrived first and the length was checked second,
    so a decoder writing 256 MB inside its time limit had already been given it."""
    created = _watch_children(monkeypatch)
    monkeypatch.setattr(frames, "MAX_DECODER_OUTPUT_BYTES", 64 * 1024)
    flood = (
        "import sys\n"
        "block = b'x' * 65536\n"
        "for _ in range(4096):\n"
        "    sys.stdout.buffer.write(block)\n"
    )

    started = time.monotonic()
    with pytest.raises(frames.FrameExtractionError, match="ceiling"):
        frames._run([sys.executable, "-c", flood], timeout=60)

    assert time.monotonic() - started < 30
    assert created[0].poll() is not None


def test_an_uncancelled_decode_is_untouched():
    """The event is optional and absent everywhere it is not wired up yet."""
    result = frames._run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PNG')"], timeout=30
    )

    assert result.returncode == 0 and result.stdout == b"PNG"


def test_a_cancellation_is_still_a_frame_extraction_error():
    """Every tool-layer handler catches FrameExtractionError. A cancellation type
    outside that hierarchy would become a new uncaught escape path."""
    assert issubclass(frames.DecodingCancelled, frames.FrameExtractionError)


# --- decoders get a size ceiling too, not only a time one ----------------------


def test_an_oversized_animation_is_refused_before_any_frame_is_decoded(tmp_path, monkeypatch):
    """Frame COUNT was bounded; frame SIZE was not. This path opens the file
    directly rather than through open_image_bytes, so that module's
    MAX_DECODED_PIXELS ceiling never applied here and a 20000x20000 animated WebP
    decoded at 400 megapixels per frame."""

    class _HugeAnimation:
        size = (20000, 20000)
        n_frames = 10

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # frames.py imports PIL inside the function, so the patch has to land on the
    # real module rather than on a module-level name that does not exist.
    monkeypatch.setattr("PIL.Image.open", lambda path: _HugeAnimation())

    with pytest.raises(frames.FrameExtractionError, match="pixel decode limit"):
        frames._frames_with_pillow("ignored.webp", count=2)


def test_the_ffmpeg_command_scales_before_it_encodes_a_png():
    """ffmpeg encoded at the SOURCE resolution and the tool layer downscaled after,
    so an 8K video built an 8K PNG per frame purely to throw it away."""
    seen = {}

    def _fake_run(command, timeout, deadline=None, cancelled=None):
        seen["command"] = command
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no frame")

    original_run, original_probe = frames._run, frames._probe
    frames._run = _fake_run
    frames._probe = lambda path, deadline=None, cancelled=None: (1.0, 25.0, "h264")
    try:
        with pytest.raises(frames.FrameExtractionError):
            frames._frames_with_ffmpeg("clip.mp4", count=1)
    finally:
        frames._run, frames._probe = original_run, original_probe

    command = seen["command"]
    assert "-vf" in command, f"no filter chain: {command}"
    scale = command[command.index("-vf") + 1]
    assert "scale=" in scale
    assert "min(iw," in scale and "min(ih," in scale, (
        "the box must be clamped to the source, or force_original_aspect_ratio=decrease "
        f"upscales a small sticker to fill it: {scale}"
    )


def test_the_batch_ceiling_leaves_room_for_the_rest_of_the_machine():
    """Ten custom emoji at the 200 MiB per-asset limit fitted inside the old 2 GiB
    ceiling, so one request could hold 2 GiB of raw bytes before a decoder had
    allocated anything."""
    from telegram_mcp.media_transfer import MAX_BATCH_BYTES, MAX_FRAME_SOURCE_BYTES, batch_width

    assert MAX_BATCH_BYTES <= 512 * 1024 * 1024
    assert (
        batch_width(10, MAX_FRAME_SOURCE_BYTES) <= 2
    ), "a ten-item batch at the per-asset ceiling must not run ten wide"
    assert batch_width(10, 1024) == 10, "a small batch must still run fully concurrent"


# --- an uninterruptible native render is a process, not a thread ---------------


def _repeater_bomb_tgs(copies=150, nest=3):
    """A sub-KB .tgs whose ONE frame renders for ~12 seconds.

    A Lottie *repeater* multiplies its group at render time rather than in the
    file, and repeaters nest, so the rasteriser's work grows as ``copies ** nest``
    while the JSON stays a few hundred bytes. This is the shape that makes the
    render cost attacker-chosen: Telegram hands .tgs bytes to whoever asks for the
    sticker, and nothing in the file's size hints at what it costs to draw.
    """
    import json as _json

    group = {
        "ty": "gr",
        "it": [
            {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [220, 220]}},
            {
                "ty": "st",
                "c": {"a": 0, "k": [0.9, 0.2, 0.4, 1]},
                "o": {"a": 0, "k": 60},
                "w": {"a": 0, "k": 40},
                "lc": 2,
                "lj": 2,
            },
            {
                "ty": "tr",
                "p": {"a": 0, "k": [0, 0]},
                "a": {"a": 0, "k": [0, 0]},
                "s": {"a": 0, "k": [100, 100]},
                "r": {"a": 0, "k": 0},
                "o": {"a": 0, "k": 100},
            },
        ],
    }
    for _ in range(nest):
        group = {
            "ty": "gr",
            "it": [
                group,
                {
                    "ty": "rp",
                    "c": {"a": 0, "k": copies},
                    "o": {"a": 0, "k": 0},
                    "m": 1,
                    "tr": {
                        "p": {"a": 0, "k": [1, 1]},
                        "a": {"a": 0, "k": [0, 0]},
                        "s": {"a": 0, "k": [99, 99]},
                        "r": {"a": 0, "k": 3},
                        "so": {"a": 0, "k": 100},
                        "eo": {"a": 0, "k": 100},
                        "o": {"a": 0, "k": 100},
                    },
                },
                {
                    "ty": "tr",
                    "p": {"a": 0, "k": [256, 256]},
                    "a": {"a": 0, "k": [0, 0]},
                    "s": {"a": 0, "k": [100, 100]},
                    "r": {"a": 0, "k": 0},
                    "o": {"a": 0, "k": 100},
                },
            ],
        }
    document = {
        "v": "5.5.7",
        "fr": 60,
        "ip": 0,
        "op": 60,
        "w": 512,
        "h": 512,
        "nm": "bomb",
        "layers": [
            {
                "ddd": 0,
                "ind": 1,
                "ty": 4,
                "nm": "L",
                "sr": 1,
                "ks": {
                    "o": {"a": 0, "k": 100},
                    "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [256, 256, 0]},
                    "a": {"a": 0, "k": [0, 0, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
                "ao": 0,
                "ip": 0,
                "op": 60,
                "st": 0,
                "bm": 0,
                "shapes": [group],
            }
        ],
    }
    return gzip.compress(_json.dumps(document).encode())


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_a_tgs_that_renders_far_past_its_budget_is_killed_not_waited_out():
    """rlottie is native code. Called in-process it runs on a worker thread, and
    Python cannot interrupt a thread from outside - so a single render that does
    not return held the decode past every deadline, with the caller's cancellation
    event never read. Checking the budget between frames cannot help when control
    never reaches the next frame.

    Measured with rlottie 1.3.8 on this machine: this 332-byte payload holds one
    512x512 frame for ~12 seconds, and one more nesting level reaches half an
    hour. As a child process it is just a PID, and the deadline can end it.
    """
    payload = _repeater_bomb_tgs()
    assert len(payload) < 1024, f"the point is that the FILE is tiny: {len(payload)} bytes"

    with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as handle:
        handle.write(payload)
        path = handle.name

    budget = frames._Budget(deadline=time.monotonic() + 2.0)
    started = time.monotonic()
    try:
        with pytest.raises(FrameExtractionError):
            frames._frames_with_lottie(path, 3, budget)
        elapsed = time.monotonic() - started
    finally:
        os.unlink(path)

    # Well under the ~12s a single frame of this costs, which is what proves the
    # render was ended rather than allowed to finish.
    assert elapsed < 9, f"the render ran to completion instead of being killed ({elapsed:.1f}s)"


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_a_caller_that_stops_waiting_stops_the_tgs_render_too():
    """The event is the only way a decode on a worker thread learns that the
    coroutine awaiting it was cancelled. Before the render moved into a child
    process there was nothing to tell: the native call held the thread.
    """
    payload = _repeater_bomb_tgs()
    with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as handle:
        handle.write(payload)
        path = handle.name

    cancelled = threading.Event()
    threading.Timer(1.0, cancelled.set).start()
    budget = frames._Budget(deadline=time.monotonic() + 60, cancelled=cancelled)

    started = time.monotonic()
    try:
        with pytest.raises(frames.DecodingCancelled):
            frames._frames_with_lottie(path, 3, budget)
        elapsed = time.monotonic() - started
    finally:
        os.unlink(path)

    assert elapsed < 9, f"cancellation did not reach the renderer ({elapsed:.1f}s)"


# --- the worker itself, in-process ---------------------------------------------
#
# It only ever RUNS in a child process, which is the whole point, and coverage
# cannot see into one. Calling it directly is not a workaround for that: the
# protocol between the two halves is real logic, and a byte-count or exit-code
# mistake in it would otherwise only ever show up as the parent's generic
# "the render was cut short".


def _write_tgs(tmp_path, payload, name="anim.tgs"):
    path = tmp_path / name
    path.write_bytes(payload)
    return str(path)


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_the_worker_emits_a_header_and_exactly_one_frame_of_bytes_per_index(
    tmp_path, capsysbinary
):
    from telegram_mcp.visual import lottie_worker
    from telegram_mcp.visual.frames import LOTTIE_RENDER_SIZE

    assert lottie_worker.render(_write_tgs(tmp_path, _animated_tgs()), 3, LOTTIE_RENDER_SIZE) == 0

    stdout = capsysbinary.readouterr().out
    header_line, _, payload = stdout.partition(b"\n")
    header = json.loads(header_line)

    assert header["total"] > 1
    assert len(header["indexes"]) == 3
    # The parent slices this by a fixed stride and refuses a short reply, so an
    # off-by-one frame here has to fail loudly rather than silently drop a frame.
    assert len(payload) == LOTTIE_RENDER_SIZE * LOTTIE_RENDER_SIZE * 4 * 3


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_the_worker_reports_an_unopenable_file_with_its_own_exit_code(tmp_path, capsysbinary):
    """The parent turns exit 3 into "could not open" and anything else into "could
    not render". Collapsing the two would tell a caller the animation is broken
    when in fact the bytes were never a Lottie at all."""
    from telegram_mcp.visual import lottie_worker

    path = _write_tgs(tmp_path, b"not gzipped lottie at all")

    assert lottie_worker.main([path, "3", "512"]) == lottie_worker.EXIT_CANNOT_OPEN
    assert capsysbinary.readouterr().err.strip(), "the failure carried no diagnostic"


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_the_worker_reports_a_zero_frame_animation_rather_than_rendering_a_blank(
    tmp_path, capsysbinary
):
    """rlottie does not raise on garbage that still gunzips: it reports totalframe=0.
    The worker forwards that verbatim and lets the parent decide, because the parent
    is where the error text explaining it lives."""
    from telegram_mcp.visual import lottie_worker

    path = _write_tgs(tmp_path, gzip.compress(b'{"not":"a lottie"}'))

    assert lottie_worker.main([path, "3", "512"]) == 0
    stdout = capsysbinary.readouterr().out
    assert json.loads(stdout.partition(b"\n")[0])["total"] == 0
    assert stdout.partition(b"\n")[2] == b"", "a zero-frame reply must carry no frames"


def test_the_worker_refuses_the_wrong_number_of_arguments(capsysbinary):
    from telegram_mcp.visual import lottie_worker

    assert lottie_worker.main(["only-a-path"]) == 2
    assert b"usage:" in capsysbinary.readouterr().err


# --- the requested size has to lower the cost, not just the output -------------


def test_the_requested_side_never_exceeds_the_hard_ceiling_or_falls_below_useful():
    """A ceiling that a caller can raise is not a ceiling, and one they can drive to
    zero produces an image nothing can read."""
    assert frames._emitted_side(None) == frames.FFMPEG_MAX_EMITTED_SIDE
    assert frames._emitted_side(0) == frames.FFMPEG_MAX_EMITTED_SIDE
    assert frames._emitted_side(-100) == frames.FFMPEG_MAX_EMITTED_SIDE
    assert frames._emitted_side(99999) == frames.FFMPEG_MAX_EMITTED_SIDE
    assert frames._emitted_side(1) == frames.MIN_EMITTED_SIDE
    assert frames._emitted_side(256) == 256


@pytest.mark.skipif(not frames.ffmpeg_available(), reason="ffmpeg is not on PATH")
def test_ffmpeg_decodes_to_the_requested_side_not_the_hard_ceiling(tmp_path):
    """Extraction used to emit at FFMPEG_MAX_EMITTED_SIDE whatever the caller asked
    for, and the tool layer shrank the result afterwards. A 128px preview therefore
    paid for a 2048px decode, a 2048px PNG encode and a 2048px image held in a list,
    then discarded almost all of it.
    """
    clip = _transparent_vp9(tmp_path)

    small = frames._frames_with_ffmpeg(str(clip), 1, max_side=64)

    assert small, "no frame came back"
    rendered = Image.open(io.BytesIO(small[0][0]))
    assert max(rendered.size) <= 64, f"decoded at {rendered.size} for a 64px request"


@pytest.mark.skipif(not lottie_available(), reason="rlottie is not installed")
def test_a_tgs_renders_at_the_requested_side(tmp_path):
    """The worker renders at the size it is told and the parent slices its reply at
    the SAME stride - get that pair wrong and every frame after the first is read
    from the wrong offset, which is silent corruption rather than an error.
    """
    path = _write_tgs(tmp_path, _animated_tgs())

    small = frames._frames_with_lottie(path, 2, max_side=128)

    assert len(small) == 2
    for data, _meta in small:
        rendered = Image.open(io.BytesIO(data))
        assert max(rendered.size) <= 128, f"rendered at {rendered.size} for a 128px request"


def test_a_pillow_animation_is_encoded_at_the_requested_side(tmp_path):
    """Pillow decodes at the source's own size - that part is not ours to bound -
    but encoding a full-size PNG per frame and shrinking afterwards is."""
    source = tmp_path / "big.gif"
    frames_in = [Image.new("RGB", (600, 400), colour) for colour in ("red", "green", "blue")]
    frames_in[0].save(source, save_all=True, append_images=frames_in[1:], duration=40, loop=0)

    small = frames._frames_with_pillow(str(source), 2, max_side=96)

    assert len(small) == 2
    for data, _meta in small:
        rendered = Image.open(io.BytesIO(data))
        assert max(rendered.size) <= 96, f"encoded at {rendered.size} for a 96px request"


# --- the parent half of the .tgs split, without needing the renderer -----------
#
# The coverage job runs without the optional rlottie extra, so every test gated on
# `lottie_available()` skips there and the whole parent-side protocol - header
# parsing, stride arithmetic, exit-code mapping, blank detection - went unmeasured
# on the platform CI actually gates on. None of it needs the renderer: it needs a
# worker REPLY, which is a bytes object.


def _worker_reply(indexes, total=31, framerate=30.0, side=None, colour=(10, 20, 30, 255)):
    """A reply shaped exactly as lottie_worker writes one."""
    from telegram_mcp.visual.frames import LOTTIE_RENDER_SIZE

    side = side or LOTTIE_RENDER_SIZE
    header = json.dumps({"total": total, "framerate": framerate, "indexes": list(indexes)})
    body = bytes(colour) * (side * side) * len(indexes)
    return header.encode("utf-8") + b"\n" + body


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def _fake_worker(monkeypatch):
    """Swap the child process for a reply this test writes."""
    monkeypatch.setattr(frames, "lottie_available", lambda: True)

    def _install(result):
        monkeypatch.setattr(frames, "_run", lambda *a, **k: result)

    return _install


def test_the_parent_turns_a_worker_reply_into_frames(_fake_worker):
    _fake_worker(_FakeCompleted(stdout=_worker_reply([0, 15, 30])))

    extracted = frames._frames_with_lottie("ignored.tgs", 3)

    assert len(extracted) == 3
    assert [meta["frame_index"] for _data, meta in extracted] == [0, 15, 30]
    assert all(meta["source"] == "rlottie" for _data, meta in extracted)
    assert all(meta["animation_format"] == "lottie_tgs" for _data, meta in extracted)
    # framerate 30 => frame 15 is half a second in.
    assert extracted[1][1]["timestamp_seconds"] == 0.5


def test_a_short_reply_is_refused_rather_than_sliced_into_garbage(_fake_worker):
    """The parent slices the body at a fixed stride. A reply cut short would still
    slice - into frames made of the next frame's pixels, or into empty strings -
    and produce images that look like a successful render of nothing."""
    truncated = _worker_reply([0, 15])[:-500]
    _fake_worker(_FakeCompleted(stdout=truncated))

    with pytest.raises(FrameExtractionError, match="cut short"):
        frames._frames_with_lottie("ignored.tgs", 2)


def test_a_reply_the_parent_cannot_read_is_reported_as_such(_fake_worker):
    _fake_worker(_FakeCompleted(stdout=b"not json at all\nbody"))

    with pytest.raises(FrameExtractionError, match="cannot read"):
        frames._frames_with_lottie("ignored.tgs", 1)


def test_a_zero_frame_animation_is_refused_not_rendered_blank(_fake_worker):
    """rlottie does not raise on garbage that still gunzips - it reports zero frames.
    Rendering that would produce a transparent canvas the tool layer would then
    label a successful frame."""
    _fake_worker(_FakeCompleted(stdout=_worker_reply([], total=0)))

    with pytest.raises(FrameExtractionError, match="zero frames"):
        frames._frames_with_lottie("ignored.tgs", 1)


def test_the_two_worker_failures_stay_distinguishable(_fake_worker):
    """Exit 3 means rlottie refused the FILE; anything else means it failed while
    rendering. Collapsing them tells a caller their animation is broken when the
    bytes were never a Lottie at all."""
    _fake_worker(_FakeCompleted(returncode=frames.LOTTIE_EXIT_CANNOT_OPEN, stderr=b"BadGzipFile"))
    with pytest.raises(FrameExtractionError, match="could not open"):
        frames._frames_with_lottie("ignored.tgs", 1)

    _fake_worker(_FakeCompleted(returncode=1, stderr=b"RuntimeError: boom"))
    with pytest.raises(FrameExtractionError, match="could not render"):
        frames._frames_with_lottie("ignored.tgs", 1)


def test_a_fully_transparent_frame_is_flagged_not_treated_as_a_failure(_fake_worker):
    """A message effect BEGINS and ENDS transparent, so an evenly spaced ladder
    legitimately lands on blank canvases. Without the flag a caller sees a blank
    image beside full ones and concludes the render broke."""
    _fake_worker(_FakeCompleted(stdout=_worker_reply([0], colour=(0, 0, 0, 0))))

    ((_data, meta),) = frames._frames_with_lottie("ignored.tgs", 1)

    assert meta["blank"] is True
    assert "not a failed render" in meta["blank_note"]


def test_a_frame_with_content_is_not_flagged_blank(_fake_worker):
    _fake_worker(_FakeCompleted(stdout=_worker_reply([0], colour=(255, 0, 0, 255))))

    ((_data, meta),) = frames._frames_with_lottie("ignored.tgs", 1)

    assert "blank" not in meta


def _fake_rlottie(monkeypatch, total=31, framerate=30.0):
    """A stand-in for rlottie_python, so the worker's own protocol code is testable
    where the optional renderer is not installed - which is every CI job that
    measures coverage.
    """
    import sys as _sys
    import types as _types

    class _Animation:
        @staticmethod
        def from_tgs(path):
            if b"bad" in Path(path).read_bytes():
                raise ValueError("Unknown compression method")
            return _Animation()

        def lottie_animation_get_totalframe(self):
            return total

        def lottie_animation_get_framerate(self):
            return framerate

        def render_pillow_frame(self, frame_num, width, height):
            return Image.new("RGBA", (width, height), (frame_num % 256, 0, 0, 255))

    module = _types.ModuleType("rlottie_python")
    module.LottieAnimation = _Animation
    monkeypatch.setitem(_sys.modules, "rlottie_python", module)


def test_the_worker_writes_a_header_then_exactly_one_frame_per_index(
    tmp_path, capsysbinary, monkeypatch
):
    """The contract the parent slices against, checked from the worker's side."""
    from telegram_mcp.visual import lottie_worker

    _fake_rlottie(monkeypatch)
    path = tmp_path / "anim.tgs"
    path.write_bytes(b"gzipped-lottie")

    assert lottie_worker.render(str(path), 3, 64) == 0

    header_line, _, body = capsysbinary.readouterr().out.partition(b"\n")
    header = json.loads(header_line)
    assert header["total"] == 31
    assert header["framerate"] == 30.0
    assert len(header["indexes"]) == 3
    assert len(body) == 64 * 64 * 4 * 3


def test_the_worker_exit_code_for_an_unopenable_file_needs_no_renderer(
    tmp_path, capsysbinary, monkeypatch
):
    from telegram_mcp.visual import lottie_worker

    _fake_rlottie(monkeypatch)
    path = tmp_path / "bad.tgs"
    path.write_bytes(b"bad payload")

    assert lottie_worker.main([str(path), "3", "64"]) == lottie_worker.EXIT_CANNOT_OPEN
    assert b"Unknown compression method" in capsysbinary.readouterr().err


def test_the_worker_forwards_a_zero_frame_animation_verbatim(tmp_path, capsysbinary, monkeypatch):
    """Deciding what zero frames MEANS is the parent's job - it owns the error text."""
    from telegram_mcp.visual import lottie_worker

    _fake_rlottie(monkeypatch, total=0)
    path = tmp_path / "empty.tgs"
    path.write_bytes(b"gzipped-lottie")

    assert lottie_worker.main([str(path), "3", "64"]) == 0
    out = capsysbinary.readouterr().out
    assert json.loads(out.partition(b"\n")[0])["total"] == 0
    assert out.partition(b"\n")[2] == b"", "a zero-frame reply must carry no frames"


def test_a_render_that_fails_midway_is_its_own_exit_code(tmp_path, capsysbinary, monkeypatch):
    """Distinct from "cannot open": the file parsed, the drawing broke."""
    from telegram_mcp.visual import lottie_worker

    _fake_rlottie(monkeypatch)
    import rlottie_python

    def _boom(self, frame_num, width, height):
        raise RuntimeError("rasteriser gave up")

    monkeypatch.setattr(rlottie_python.LottieAnimation, "render_pillow_frame", _boom)
    path = tmp_path / "anim.tgs"
    path.write_bytes(b"gzipped-lottie")

    assert lottie_worker.main([str(path), "2", "64"]) == lottie_worker.EXIT_RENDER_FAILED
    assert b"rasteriser gave up" in capsysbinary.readouterr().err
