"""Image encoding helpers shared by the visual and preview tools.

Every image returned over MCP is base64-encoded into the model's context, so the
cost is paid in tokens, not bytes. These helpers keep that cost bounded and
predictable while leaving the pixels themselves untouched whenever the image
already fits.
"""

from __future__ import annotations

import io
from typing import Any, Optional

# Downscale beyond this and text in a Telegram screenshot stops being readable;
# stay under it and a full-window capture costs roughly 1-3k tokens.
MAX_IMAGE_DIMENSION = 1568
MIN_IMAGE_DIMENSION = 64

# The ceiling ``native=True`` is measured against. "Do not downscale to
# max_dimension" is a reasonable thing to ask for; "no limit at all" is not, and
# that is what the flag used to mean - an 8K window came back untouched at
# roughly 20k+ tokens of base64, and get_telegram_frames multiplies that by up to
# eight. 4096 keeps the documented use (full detail on a region, or on a normal
# window) exact while putting an end on the pathological one.
MAX_NATIVE_DIMENSION = 4096

# Pillow only *warns* between 1x and 2x its MAX_IMAGE_PIXELS and raises above 2x
# (~179M pixels on 12.x), so a few-KB PNG declaring ~178M pixels decodes and
# allocates roughly 700 MB in a worker thread. The download cap upstream bounds
# compressed bytes, not decoded pixels, and output is capped at
# MAX_IMAGE_DIMENSION anyway — nothing above this is usable. Telegram's largest
# photo is 2560px on the long side (~6.5 MP).
MAX_DECODED_PIXELS = 50_000_000

IMAGE_FORMATS = {
    "png": ("PNG", "image/png"),
    "jpeg": ("JPEG", "image/jpeg"),
    "jpg": ("JPEG", "image/jpeg"),
    "webp": ("WEBP", "image/webp"),
}


class ImageError(RuntimeError):
    """Raised when an image cannot be decoded or encoded."""


def _require_pillow():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise ImageError(
            "Pillow is required for image tools. Install it with: pip install 'telegram-mcp[visual]'"
        ) from error


def open_image_bytes(data: bytes):
    """Decode raw bytes into a PIL image."""
    _require_pillow()
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        # open() only reads the header; load() is what allocates the pixels.
        pixels = image.width * image.height
        if pixels > MAX_DECODED_PIXELS:
            raise ImageError(
                f"Image declares {pixels} pixels, above the {MAX_DECODED_PIXELS} limit; "
                "refusing to decode it. Use get_media_thumbnail for a bounded preview."
            )
        image.load()
        return image
    except ImageError:
        # Our own guard must not be re-wrapped by the handler below, which would
        # bury the real reason inside a generic "could not decode" message.
        raise
    except Exception as error:
        raise ImageError(f"Could not decode image data ({len(data)} bytes): {error}") from error


def bounded_dimension(max_dimension) -> int:
    """The longest side a caller may actually ask for, clamped to what is usable.

    Its own function because it is the ceiling on every image this server returns,
    and the decoders now emit at it directly rather than emitting large and being
    shrunk afterwards. Two statements of one rule is how they drift apart, and the
    one that drifts upward is the one that costs the caller tokens.

    A caller-supplied 0 or negative must not become "unlimited": that is how a
    tool argument turns into a 4K screenshot and tens of thousands of tokens.
    """
    try:
        wanted = int(max_dimension)
    except (TypeError, ValueError):
        wanted = MAX_IMAGE_DIMENSION
    return max(MIN_IMAGE_DIMENSION, min(MAX_IMAGE_DIMENSION, wanted or MAX_IMAGE_DIMENSION))


def fit_image(image, max_dimension: Optional[int] = MAX_IMAGE_DIMENSION):
    """Shrink ``image`` so its longest side is at most ``max_dimension``.

    Never upscales: a 96x96 thumbnail stays 96x96 rather than being blown up into
    a blurry, token-expensive rectangle.
    """
    # Only an explicit None (internal callers) disables the cap.
    if max_dimension is None:
        return image, False
    return _fit_to(image, bounded_dimension(max_dimension))


def _fit_to(image, longest_side: int):
    """``(image, was_resized)`` with the long side at most ``longest_side``."""
    longest = max(image.width, image.height)
    if longest <= longest_side:
        return image, False

    from PIL import Image

    ratio = longest_side / longest
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(size, Image.LANCZOS), True


def encode_image(
    image,
    image_format: str = "png",
    max_dimension: Optional[int] = MAX_IMAGE_DIMENSION,
    quality: int = 85,
    native: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Encode a PIL image, returning ``(bytes, metadata)``.

    PNG is the default because Telegram screenshots are mostly text and UI edges,
    where JPEG artifacts hurt readability the most.

    ``native=True`` is the explicit opt-out of the size cap: the image is encoded
    at whatever resolution it already has, ``max_dimension`` is ignored, and the
    metadata carries ``native_resolution`` so the caller knows no downscale was
    applied. It exists because the clamp inside ``fit_image`` is deliberately
    unescapable through ``max_dimension`` alone. It says nothing about the codec:
    a native JPEG or WEBP is still lossy at ``quality``, so PNG is the format to
    ask for when the pixels themselves have to survive.
    """
    _require_pillow()
    key = (image_format or "png").lower().lstrip(".")
    if key not in IMAGE_FORMATS:
        raise ImageError(
            f"Unsupported image format {image_format!r}. Expected one of: {', '.join(sorted(IMAGE_FORMATS))}."
        )
    pil_format, mime_type = IMAGE_FORMATS[key]

    original_size = (image.width, image.height)
    if native:
        # Still a ceiling, just a much higher one. The flag means "keep the real
        # pixels" and it used to mean "no limit", which is how one tool argument
        # turned an 8K window into an unbounded reply.
        image, resized = _fit_to(image, MAX_NATIVE_DIMENSION)
    else:
        image, resized = fit_image(image, max_dimension)

    if pil_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif pil_format in ("PNG", "WEBP") and image.mode not in ("RGB", "RGBA", "L", "P"):
        image = image.convert("RGBA")

    buffer = io.BytesIO()
    save_kwargs: dict[str, Any] = {}
    if pil_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = max(1, min(100, int(quality)))
    try:
        image.save(buffer, format=pil_format, **save_kwargs)
    except Exception as error:
        raise ImageError(f"Could not encode image as {pil_format}: {error}") from error

    data = buffer.getvalue()
    meta = {
        "format": key if key != "jpg" else "jpeg",
        "mime_type": mime_type,
        "width": image.width,
        "height": image.height,
        "bytes": len(data),
    }
    if resized:
        meta["original_width"], meta["original_height"] = original_size
        meta["downscaled"] = True
    if native:
        meta["native_resolution"] = True
    return data, meta
