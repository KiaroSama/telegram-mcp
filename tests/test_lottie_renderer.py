"""The .tgs renderer, on both sides of the process boundary it runs across.

Split from `test_visual_budget.py`, which had grown past 800 lines carrying two
subjects. That file is about what STOPS a decode - a shared deadline, a caller
that went away, a ceiling reached before a frame is decoded. This one is about
the single decoder that had to be moved into a child process to be stoppable at
all, and about the protocol that move created.

rlottie is native code. Called in-process it runs on a worker thread, and Python
cannot interrupt a thread from outside - so one frame that does not return held
the whole decode past every deadline with the cancellation event never read, and
a 332-byte .tgs buys twelve seconds of exactly that. As a child process it is a
PID, which is a thing a deadline can end.

What the move bought has to be paid for in a header, a stride and three exit
codes, and both halves have to agree about all of them. So the tests come in
pairs: the parent turning a worker REPLY into frames - which needs no renderer,
and is therefore the half the coverage job can actually measure - and the worker
writing one.
"""

import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import pytest
from PIL import Image

from telegram_mcp.visual import frames
from telegram_mcp.visual.frames import FrameExtractionError, lottie_available
from helpers_visual import _animated_tgs

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
    """Swap the child process for a reply this test writes.

    `_run` is DEFINED in decode_budget and patched here on frames, which is
    correct only for as long as `_frames_with_lottie` stays in frames: a function
    reads a global out of its own module's namespace. Move the caller and this
    patch keeps succeeding against a name nobody reads any more.
    """
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
