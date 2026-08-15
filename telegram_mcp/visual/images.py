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
        image.load()
        return image
    except Exception as error:
        raise ImageError(f"Could not decode image data ({len(data)} bytes): {error}") from error


def fit_image(image, max_dimension: Optional[int] = MAX_IMAGE_DIMENSION):
    """Shrink ``image`` so its longest side is at most ``max_dimension``.

    Never upscales: a 96x96 thumbnail stays 96x96 rather than being blown up into
    a blurry, token-expensive rectangle.
    """
    # A caller-supplied 0/negative must not become "unlimited": that is how a tool
    # argument turns into a 4K screenshot and tens of thousands of tokens. Only an
    # explicit None (internal callers) disables the cap.
    if max_dimension is None:
        return image, False
    max_dimension = max(
        MIN_IMAGE_DIMENSION, min(MAX_IMAGE_DIMENSION, int(max_dimension) or MAX_IMAGE_DIMENSION)
    )
    longest = max(image.width, image.height)
    if longest <= max_dimension:
        return image, False

    from PIL import Image

    ratio = max_dimension / longest
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
    resized = False
    if not native:
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
