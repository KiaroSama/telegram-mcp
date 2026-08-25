#!/usr/bin/env python3
"""Capture Telegram Desktop in a child process, so a wedged PrintWindow can be killed.

``PrintWindow`` asks the target window to redraw itself into an off-screen DC,
which means the call runs on THAT window's message loop. Microsoft documents it
as synchronous and promises nothing about it returning promptly, and a window
that is busy, hung, or hostile simply does not answer. Called on a worker thread
that is unrecoverable: Python cannot stop a thread from outside, so the capture
outlives every deadline and the cancelled caller waits for an answer no one will
deliver. Measured against the previous version: the coroutine finished on
cancellation and the thread stayed alive.

Same conclusion the .tgs renderer reached, and the same shape of fix. Here it is
a PID, and :mod:`telegram_mcp.visual.bounded_process` bounds one, kills it and
reaps it.

All the frames of one request run in a single child: the interpreter and Pillow
are imported once rather than eight times, and the parent's single timeout then
bounds the whole call rather than each frame separately.

Protocol, on stdout::

    {"window": {...}, "frames": [{...}, ...]}\\n
    <encoded bytes, len(meta["bytes"]) per frame, in order>
"""

import json
import sys
import time

EXIT_FAILED = 1
EXIT_CAPTURE_REFUSED = 3  # a CaptureError, whose message already says what to do
EXIT_IMAGE_REFUSED = 4


def run(request: dict) -> int:
    from telegram_mcp.visual.capture import (
        CAPTURE_FRAME_SECONDS,
        DEFAULT_PROCESS_NAME,
        MAX_CAPTURE_RESPONSE_BYTES,
        CaptureError,
        capture_window,
        check_response_bytes,
    )
    from telegram_mcp.visual.images import encode_image

    count = max(1, int(request.get("count", 1)))
    interval = max(0.0, float(request.get("interval_ms", 0)) / 1000.0)
    limit = int(request.get("max_bytes") or MAX_CAPTURE_RESPONSE_BYTES)
    region = request.get("region")

    payload, metas, window_dict = b"", [], None
    started = time.monotonic()
    for index in range(count):
        if index:
            time.sleep(interval)
        frame_started = time.monotonic()
        image, window, meta = capture_window(
            hwnd=request.get("hwnd"),
            method=request.get("method") or "window",
            process_name=request.get("process_name") or DEFAULT_PROCESS_NAME,
            client_only=bool(request.get("client_only")),
            region=tuple(region) if region else None,
        )
        data, image_meta = encode_image(
            image,
            image_format=request.get("image_format") or "png",
            max_dimension=request.get("max_dimension"),
            native=bool(request.get("native")),
        )
        window_dict = window.to_dict()
        meta["window"] = window_dict
        meta["image"] = image_meta
        meta["bytes"] = len(data)
        meta["index"] = index
        meta["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        payload += data
        # Checked as the frames accumulate, not once they all exist: the ceiling
        # is on what is held, and the last frame is the one that would break it.
        check_response_bytes(len(payload), limit)
        metas.append(meta)
        if time.monotonic() - frame_started > CAPTURE_FRAME_SECONDS:
            # A soft check between frames. It cannot end a capture that never
            # returns - only the parent killing this process does that - but it
            # stops seven more slow frames after the first one proved slow.
            raise CaptureError(
                f"Frame {index} took longer than the {CAPTURE_FRAME_SECONDS:g}s per-frame "
                "budget. Ask for fewer frames, or a smaller max_dimension."
            )

    out = sys.stdout.buffer
    out.write(json.dumps({"window": window_dict, "frames": metas}).encode("utf-8") + b"\n")
    out.write(payload)
    out.flush()
    return 0


def main(argv: list) -> int:
    from telegram_mcp.visual.capture import CaptureError
    from telegram_mcp.visual.images import ImageError

    if len(argv) != 1:
        print("usage: capture_worker.py <json-request>", file=sys.stderr)
        return 2
    try:
        return run(json.loads(argv[0]))
    except CaptureError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CAPTURE_REFUSED
    except ImageError as error:
        print(str(error), file=sys.stderr)
        return EXIT_IMAGE_REFUSED
    except Exception as error:  # noqa: BLE001 - the parent turns this into its own error
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
