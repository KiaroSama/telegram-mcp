"""Compose labelled image grids, so one picture can index many.

A model asked to pick "the third avatar" from ten separate images pays for ten
image blocks and still has to hold which was which. One sheet with the labels drawn
on it answers the same question in one block, and the label is what the caller then
passes back to open that photo full size.

Decoding is capped. Each tile comes from a peer, so every one of them goes through
:func:`telegram_mcp.visual.images.open_image_bytes`, which refuses a declared pixel
count above ``MAX_DECODED_PIXELS`` before Pillow allocates anything.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from telegram_mcp.visual.images import ImageError, encode_image, open_image_bytes

CELL_EDGE_PIXELS = 256
LABEL_STRIP_PIXELS = 26
LABEL_FONT_PIXELS = 15
GRID_BACKGROUND = (24, 24, 24)
LABEL_BACKGROUND = (0, 0, 0)
LABEL_FOREGROUND = (255, 255, 255)
MAXIMUM_COLUMNS = 4

# A grid is bounded by construction, but the bound has to be stated: at 4 columns
# and 256px cells this is a 1024x1692 sheet, which is the largest thing worth
# sending as one block.
MAXIMUM_TILES = 24


class ContactSheetError(RuntimeError):
    """The sheet could not be composed."""


def _columns_for(tile_count: int, requested: Optional[int]) -> int:
    if requested and requested > 0:
        return min(requested, tile_count)
    return min(MAXIMUM_COLUMNS, max(1, math.ceil(math.sqrt(tile_count))))


def _fit_within_cell(image, Image):
    """Scale to the cell without ever enlarging - a 64px avatar stays 64px.

    Blowing a small avatar up to the cell edge spends tokens on interpolation and
    tells the reader the source had detail it never had.
    """
    scale = min(CELL_EDGE_PIXELS / image.width, CELL_EDGE_PIXELS / image.height, 1.0)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    if (width, height) == image.size:
        return image
    return image.resize((width, height), Image.LANCZOS)


def _load_label_font(ImageFont):
    try:
        return ImageFont.load_default(size=LABEL_FONT_PIXELS)
    except TypeError:  # pragma: no cover - Pillow below 10.1 has no size argument
        return ImageFont.load_default()


def _tile_image(data: bytes, Image):
    """One decoded, cell-sized tile, or a placeholder when the bytes will not open.

    A tile that fails to decode must not lose the sheet: the label still has to be
    there, because the caller reads positions off it to ask for a photo by name.
    """
    try:
        with open_image_bytes(data) as opened:
            return _fit_within_cell(opened.convert("RGB"), Image)
    except (ImageError, OSError, ValueError):
        return Image.new("RGB", (CELL_EDGE_PIXELS, CELL_EDGE_PIXELS), GRID_BACKGROUND)


def compose_contact_sheet(
    tiles: Sequence[Tuple[bytes, str]],
    columns: Optional[int] = None,
) -> tuple:
    """``(png_bytes, metadata)`` for a labelled grid of ``(image_bytes, label)``.

    Raises :class:`ContactSheetError` when there is nothing to compose or the grid
    would exceed ``MAXIMUM_TILES``, rather than quietly truncating - a sheet missing
    entries the caller asked for is worse than a refusal, because nothing in the
    picture says which ones are absent.
    """
    from PIL import Image, ImageDraw

    from PIL import ImageFont

    if not tiles:
        raise ContactSheetError("There are no images to compose into a contact sheet.")
    if len(tiles) > MAXIMUM_TILES:
        raise ContactSheetError(
            f"A contact sheet holds at most {MAXIMUM_TILES} images; {len(tiles)} were given. "
            "Ask for fewer."
        )

    column_count = _columns_for(len(tiles), columns)
    row_count = math.ceil(len(tiles) / column_count)
    cell_height = CELL_EDGE_PIXELS + LABEL_STRIP_PIXELS

    sheet = Image.new(
        "RGB",
        (column_count * CELL_EDGE_PIXELS, row_count * cell_height),
        GRID_BACKGROUND,
    )
    draw = ImageDraw.Draw(sheet)
    font = _load_label_font(ImageFont)

    placed: List[dict] = []
    for index, (data, label) in enumerate(tiles):
        column, row = index % column_count, index // column_count
        left, top = column * CELL_EDGE_PIXELS, row * cell_height

        thumbnail = _tile_image(data, Image)
        offset_x = left + (CELL_EDGE_PIXELS - thumbnail.width) // 2
        offset_y = top + (CELL_EDGE_PIXELS - thumbnail.height) // 2
        sheet.paste(thumbnail, (offset_x, offset_y))

        strip_top = top + CELL_EDGE_PIXELS
        draw.rectangle(
            [left, strip_top, left + CELL_EDGE_PIXELS, strip_top + LABEL_STRIP_PIXELS],
            fill=LABEL_BACKGROUND,
        )
        draw.text((left + 6, strip_top + 5), label, fill=LABEL_FOREGROUND, font=font)
        placed.append({"label": label, "column": column, "row": row})

    encoded, meta = encode_image(sheet, image_format="png")
    meta.update(
        {
            "sheet": True,
            "columns": column_count,
            "rows": row_count,
            "cells": placed,
            "cell_pixels": CELL_EDGE_PIXELS,
        }
    )
    return encoded, meta


__all__ = ["ContactSheetError", "MAXIMUM_TILES", "compose_contact_sheet"]
