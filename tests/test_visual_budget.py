"""Bounding a decode: the request budget, cancellation, and size ceilings.

Split from `test_visual_media.py`, which had grown past 1400 lines carrying two
different subjects. That file is about what the decoders PRODUCE; this one is
about what stops them - a shared deadline rather than a per-call timeout, a
cancelled request taking its child process with it, and the ceilings applied
before any frame is decoded.

The .tgs renderer went the same way, into `test_lottie_renderer.py`: proving
that an uninterruptible native render is a PROCESS rather than a thread needs
the whole parent/worker protocol alongside it, and that is a subject of its own.
"""

import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from telegram_mcp.visual import bounded_process, decode_budget, frames
from telegram_mcp.visual.frames import extract_frames
from helpers_visual import _animated_gif, _transparent_vp9

# A function reads a global out of ITS OWN module's namespace. `_run` and the
# ceilings it clamps against live in decode_budget now, while every decoder that
# CALLS `_run` still lives in frames - so patching one side alone succeeds
# against a name nobody reads, and the test goes green having tested nothing.
_MODULES = (frames, decode_budget)


def _patch_both(monkeypatch, name, value):
    for module in _MODULES:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


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
    _patch_both(monkeypatch, "MAX_DECODER_OUTPUT_BYTES", 64 * 1024)
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

    # A real file, because the decode now runs in a child process where a patched
    # PIL.Image.open in this interpreter would not exist. 177 bytes on disk that
    # DECLARE 100 megapixels is exactly the shape of the attack: the logical
    # screen descriptor is four bytes and nothing validates it against the frames.
    data = bytearray(_animated_gif(3))
    data[6:10] = (10000).to_bytes(2, "little") + (10000).to_bytes(2, "little")
    assert len(data) < 4096, "the point is that the file is tiny"

    with pytest.raises(frames.FrameExtractionError, match="pixel decode limit"):
        extract_frames(bytes(data), ".gif", count=2)


def test_the_ffmpeg_command_scales_before_it_encodes_a_png():
    """ffmpeg encoded at the SOURCE resolution and the tool layer downscaled after,
    so an 8K video built an 8K PNG per frame purely to throw it away."""
    seen = {}

    def _fake_run(command, timeout, deadline=None, cancelled=None, max_output_bytes=None):
        seen["command"] = command
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no frame")

    original_run, original_probe = frames._run, frames._probe
    original_available = frames.ffmpeg_available
    # Stubbed with everything else. This test is about the command that gets
    # BUILT, and it fakes the probe and the run - but it still asked the host
    # whether ffmpeg was installed, so on a runner without it the function
    # refused before reaching the fake and the assertion died on a KeyError
    # instead of failing on its own subject.
    frames.ffmpeg_available = lambda: True
    frames._run = _fake_run
    frames._probe = lambda path, deadline=None, cancelled=None: (1.0, 25.0, "h264")
    try:
        with pytest.raises(frames.FrameExtractionError):
            frames._frames_with_ffmpeg("clip.mp4", count=1)
    finally:
        frames._run, frames._probe = original_run, original_probe
        frames.ffmpeg_available = original_available

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


# --- the requested size has to lower the cost, not just the output -------------


def test_the_requested_side_never_exceeds_the_hard_ceiling_or_falls_below_useful():
    """A ceiling that a caller can raise is not a ceiling, and one they can drive to
    zero produces an image nothing can read."""
    ceiling = decode_budget.FFMPEG_MAX_EMITTED_SIDE

    assert decode_budget._emitted_side(None) == ceiling
    assert decode_budget._emitted_side(0) == ceiling
    assert decode_budget._emitted_side(-100) == ceiling
    assert decode_budget._emitted_side(99999) == ceiling
    assert decode_budget._emitted_side(1) == decode_budget.MIN_EMITTED_SIDE
    assert decode_budget._emitted_side(256) == 256


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


# --- a decoder must not quietly change what it decoded -------------------------


def _transparent_apng(directory):
    """A two-frame APNG whose right half is fully transparent.

    The Pillow decoder ladder, not ffmpeg: .png/.apng/.gif/.webp animations never
    reach ffmpeg, so the alpha promise has to be kept twice.
    """
    frames_out = []
    for shade in (255, 128):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.paste((shade, 0, 0, 255), (0, 0, 32, 64))
        frames_out.append(image)
    path = directory / "alpha.png"
    frames_out[0].save(path, save_all=True, append_images=frames_out[1:], duration=100, loop=0)
    return path.read_bytes()


def test_a_transparent_animation_keeps_its_alpha_through_the_pillow_decoder(tmp_path):
    """The Pillow frame path flattened every transparent pixel onto black.

    `frame.convert("RGB")` in pillow_worker dropped the alpha channel outright, so
    a transparent animated sticker came back as an opaque square - while
    `extract_still`, in the SAME worker and for the SAME file, returned it with
    transparency intact. Measured before the fix: mode=RGB and alpha 255
    everywhere; the still of the same bytes was mode=RGBA with alpha 0.
    """
    data = _transparent_apng(tmp_path)

    still, _meta = frames.extract_still(data)
    assert Image.open(io.BytesIO(still)).mode == "RGBA", "the still path lost its alpha"

    extracted = extract_frames(data, ".png", 2)

    assert extracted, "no frame came back"
    for encoded, meta in extracted:
        rendered = Image.open(io.BytesIO(encoded))
        assert (
            rendered.mode == "RGBA"
        ), f"frame {meta.get('frame_index')} lost its alpha: mode={rendered.mode}"
        assert (
            rendered.convert("RGBA").getpixel((48, 32))[3] == 0
        ), f"frame {meta.get('frame_index')} painted the clear half opaque"


@pytest.mark.skipif(not frames.ffmpeg_available(), reason="ffmpeg is not on PATH")
def test_the_codec_is_read_from_the_video_stream_not_from_the_first_one(tmp_path):
    """A container may list its audio track first, and one did.

    `_probe` took `codec_name` off `streams[0]` while taking the frame rate from
    whichever stream actually had one - so the two could describe different
    tracks. Muxing sound in front of an unchanged VP9 video stream turned the
    reported codec into "pcm_s16le"/"opus", which dropped the `-c:v libvpx-vp9`
    that carries the separate alpha layer: measured, the same video came back
    mode=RGBA on its own and mode=RGB with a soundtrack, silently opaque.
    """
    clip = _transparent_vp9(tmp_path)
    muxed = tmp_path / "with-sound.mkv"
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-i",
            str(clip),
            "-t",
            "1",
            # Audio FIRST, which is the whole point: it becomes streams[0].
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c:a",
            "pcm_s16le",
            "-c:v",
            "copy",
            str(muxed),
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not muxed.exists():
        pytest.skip("this ffmpeg could not mux the audio-first sample")

    assert frames._probe(str(muxed))[2] == "vp9", "the audio track answered for the video one"

    extracted = frames._frames_with_ffmpeg(str(muxed), 1)
    assert extracted, "no frame came back"
    rendered = Image.open(io.BytesIO(extracted[0][0]))
    assert rendered.mode == "RGBA", f"a soundtrack cost the clip its alpha: mode={rendered.mode}"
