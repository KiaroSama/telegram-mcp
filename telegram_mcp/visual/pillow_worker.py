#!/usr/bin/env python3
"""Decode and encode untrusted images in a child process, so a hung decode dies.

Pillow is a wrapper over native codecs, and native code called on a worker thread
cannot be interrupted by anything Python can do. That is the same problem the
.tgs renderer had, and it has the same answer: run it where a hang is a PID.

Checking the request budget between frames does not help. ``ImageSequence`` walks
- and DECODES - every frame it passes, so a single ``Image.open`` or a single
frame that does not return holds the decode past every deadline with the caller's
cancellation flag unread. The parent's ``run_bounded`` bounds a PID; nothing
bounds a thread.

Both jobs live here because both are the same untrusted decode:

* ``still`` - one image, decoded and re-encoded once.
* ``frames`` - evenly spaced frames of an animated GIF/WebP/APNG.

Protocol, on stdout::

    {"total": int, "metas": [{...}, ...]}\\n
    <encoded bytes, len(meta["bytes"]) per meta, in order>

The exit codes are separate answers rather than one failure, because the parent
does different things with them: only MISMATCH is worth handing to another
decoder, a REFUSED file has already been judged, and FAILED happened after the
file was accepted.
"""

import json
import sys

EXIT_FAILED = 1
EXIT_MISMATCH = 3  # Pillow cannot read this format at all - try the next decoder
EXIT_REFUSED = 4  # readable, but outside a documented limit - a final answer


class _Mismatch(Exception):
    """Pillow refused the file itself, before any pixel was asked for."""


class _Refused(Exception):
    """The file is readable and this build will not decode it anyway."""


def _emit(total: int, encoded: list) -> None:
    header = {"total": total, "metas": [meta for _data, meta in encoded]}
    out = sys.stdout.buffer
    out.write(json.dumps(header).encode("utf-8") + b"\n")
    for data, _meta in encoded:
        out.write(data)
    out.flush()


def _open(path: str):
    from PIL import Image

    try:
        return Image.open(path)
    except Image.DecompressionBombError as error:
        # Pillow's own ceiling, and a refusal rather than a mismatch: the file was
        # read perfectly well and its declared size was rejected. Reported as
        # "cannot read this format" it would fall through to the next decoder,
        # which is a full ffmpeg decode of the very file just refused.
        raise _Refused(f"Pillow refused this image as a decompression bomb: {error}") from error
    except Exception as error:
        raise _Mismatch(f"Pillow could not decode this media: {type(error).__name__}.") from error


def still(path: str, side: int, image_format: str, max_bytes: int) -> int:
    """One image, decoded and re-encoded to ``side``."""
    from telegram_mcp.visual.images import MAX_DECODED_PIXELS, encode_image

    image = _open(path)
    with image:
        # open() reads only the header; load(), inside encode_image, is what
        # allocates. So the refusal has to happen here, on the declared size.
        pixels = image.width * image.height
        if pixels > MAX_DECODED_PIXELS:
            raise _Refused(
                f"Image declares {pixels} pixels, above the {MAX_DECODED_PIXELS}-pixel decode "
                "limit; refusing to decode it. Use get_media_thumbnail for a bounded preview."
            )
        data, meta = encode_image(image, image_format=image_format, max_dimension=side)
    if len(data) > max_bytes:
        raise _Refused(
            f"The encoded image is {len(data)} bytes, above the {max_bytes} bytes left in "
            "this call's budget. Ask for a smaller max_dimension, or fewer items."
        )
    _emit(1, [(data, meta)])
    return 0


def animation(path: str, count: int, side: int, max_bytes: int, max_frames: int) -> int:
    """Evenly spaced frames of an animation, each encoded to ``side``."""
    from PIL import ImageSequence

    from telegram_mcp.visual.images import MAX_DECODED_PIXELS, encode_image

    source = _open(path)
    with source:
        width, height = source.size
        if width * height > MAX_DECODED_PIXELS:
            raise _Refused(
                f"Animation frames are {width}x{height} ({width * height} pixels), above "
                f"the {MAX_DECODED_PIXELS}-pixel decode limit; refusing to decode it. "
                "Use get_media_thumbnail for a static preview."
            )
        total = getattr(source, "n_frames", 1)
        if total > max_frames:
            raise _Refused(
                f"Animation declares {total} frames, above the {max_frames} limit; "
                "refusing to decode it. Use get_media_thumbnail for a static preview."
            )
        if total <= 1:
            raise _Refused("File is not animated; a single frame is all there is.")

        wanted = min(count, total)
        indexes = (
            sorted({round(i * (total - 1) / max(1, wanted - 1)) for i in range(wanted)})
            if wanted > 1
            else [0]
        )
        encoded = []
        produced = 0
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            if index not in indexes:
                continue
            # RGBA where the frame has any, RGB where it does not. A flat
            # convert("RGB") threw the alpha away: a transparent animated sticker
            # came back with every clear pixel painted black, silently, while
            # `still` above returned the SAME file with its transparency intact.
            # The ffmpeg branch of this feature names libvpx-vp9 for exactly this
            # reason, so the two halves disagreed about the same promise.
            transparent = frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info
            data, meta = encode_image(
                frame.convert("RGBA" if transparent else "RGB"),
                image_format="png",
                max_dimension=side,
            )
            produced += len(data)
            if produced > max_bytes:
                raise _Refused(
                    f"The frames total {produced} bytes, above the {max_bytes} bytes left in "
                    "this call's budget. Ask for fewer frames, or a smaller max_dimension."
                )
            meta.update({"frame_index": index, "frame_count": total, "source": "pillow"})
            encoded.append((data, meta))
            if len(encoded) >= wanted:
                break

    if not encoded:
        raise _Refused("Pillow decoded no frames from the animation.")
    _emit(total, encoded)
    return 0


def _quiet_pillow() -> None:
    """Keep Pillow's own warnings off this worker's stderr.

    stderr is a protocol channel here: the parent redacts it, truncates it to a
    few hundred characters and shows it to the caller as the reason. Pillow's
    DecompressionBombWarning is emitted BEFORE the refusal, carries the install
    path of PIL with it, and is long enough to push the real diagnosis past the
    truncation - so a correct refusal came back reading like a stack trace from
    somebody else's machine. The warning is also redundant: the checks below use
    this project's own, stricter ceiling.
    """
    import warnings

    from PIL import Image

    warnings.simplefilter("ignore", Image.DecompressionBombWarning)


def main(argv: list) -> int:
    try:
        _quiet_pillow()
        job = argv[0]
        if job == "still":
            path, side, image_format, max_bytes = argv[1], int(argv[2]), argv[3], int(argv[4])
            return still(path, side, image_format, max_bytes)
        if job == "frames":
            path, count, side, max_bytes, max_frames = (
                argv[1],
                int(argv[2]),
                int(argv[3]),
                int(argv[4]),
                int(argv[5]),
            )
            return animation(path, count, side, max_bytes, max_frames)
    except (IndexError, ValueError):
        print(
            "usage: pillow_worker.py still <path> <side> <format> <max-bytes>\n"
            "   or: pillow_worker.py frames <path> <count> <side> <max-bytes> <max-frames>",
            file=sys.stderr,
        )
        return 2
    except _Mismatch as error:
        print(str(error), file=sys.stderr)
        return EXIT_MISMATCH
    except _Refused as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - the parent turns this into its own error
        # A truncated animation opens cleanly and fails on a LATER frame, so
        # wrapping only the open was never enough: a raw OSError ("image file is
        # truncated") and a bare SyntaxError from the PNG plugin both reach here.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"unknown job {argv[0]!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
