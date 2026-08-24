#!/usr/bin/env python3
"""Render .tgs frames in a child process, so a hung render can actually be killed.

rlottie is native code. Called in-process it runs on a worker thread, and Python
cannot interrupt a thread from outside - so a single ``render_pillow_frame`` that
does not return holds the decode forever, past every deadline, with the caller's
cancellation event unread. A frame-boundary check cannot help: control never
reaches the boundary.

That is reachable from the wire. A Lottie *repeater* multiplies its group at
render time rather than in the file, and repeaters nest, so cost grows as
``copies ** depth`` while the file stays tiny. Measured on this machine with
rlottie 1.3.8: a **332-byte** .tgs with three nested 150x repeaters (3,375,000
instances) held one 512x512 frame for 12.1 seconds; a fourth level puts it near
half an hour. Telegram hands .tgs bytes to whoever asks for the sticker.

In a child process the same hang is just a PID, and ``_run`` already knows how to
bound one and kill it. This script therefore imports rlottie and Pillow ONLY - it
must not import ``telegram_mcp``, so that spawning it costs nothing beyond the
renderer itself and cannot trip the install guard.

Protocol, on stdout:

    {"total": int, "framerate": float, "indexes": [int, ...]}\n
    <raw RGBA bytes, size*size*4 per index, in order>

Raw RGBA rather than PNG because the parent re-encodes anyway: at 512x512 a frame
is exactly 1 MiB, so ten frames are 10 MiB - comfortably inside the caller's
output ceiling, and with no second encode to pay for.
"""

import json
import sys

# Distinct exit codes because the two failures mean different things to a caller:
# a file rlottie cannot PARSE is bad input, while a failure mid-render is a broken
# or hostile animation that already got past parsing. The parent reports them
# differently, so collapsing them here would lose that.
EXIT_CANNOT_OPEN = 3
EXIT_RENDER_FAILED = 1


class _CannotOpen(Exception):
    """rlottie refused the file itself, before any frame was asked for."""


def render(path: str, count: int, size: int) -> int:
    from rlottie_python import LottieAnimation

    try:
        animation = LottieAnimation.from_tgs(path)
    except Exception as error:
        raise _CannotOpen(f"{type(error).__name__}: {error}") from error
    total = animation.lottie_animation_get_totalframe() or 0
    # Reported verbatim; deciding what a zero-frame animation means is the
    # parent's job, and it already has that error text.
    if total <= 0:
        sys.stdout.buffer.write(
            json.dumps({"total": 0, "framerate": 0.0, "indexes": []}).encode("utf-8") + b"\n"
        )
        sys.stdout.buffer.flush()
        return 0

    wanted = max(1, min(count, total))
    indexes = (
        sorted({round(i * (total - 1) / max(1, wanted - 1)) for i in range(wanted)})
        if wanted > 1
        else [0]
    )
    header = {
        "total": total,
        "framerate": float(animation.lottie_animation_get_framerate() or 0),
        "indexes": indexes,
    }
    out = sys.stdout.buffer
    out.write(json.dumps(header).encode("utf-8") + b"\n")
    for index in indexes:
        image = animation.render_pillow_frame(frame_num=index, width=size, height=size)
        out.write(image.convert("RGBA").tobytes())
    out.flush()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: lottie_worker.py <path> <count> <size>", file=sys.stderr)
        return 2
    path, count, size = argv[0], int(argv[1]), int(argv[2])
    try:
        return render(path, count, size)
    except _CannotOpen as error:
        print(str(error), file=sys.stderr)
        return EXIT_CANNOT_OPEN
    except Exception as error:  # noqa: BLE001 - the parent turns this into its own error
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_RENDER_FAILED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
