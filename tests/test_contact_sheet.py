"""One picture that indexes many, composed from real images.

The point of a sheet is that the labels on it are what the caller passes back, so
every test here reads the composed result rather than trusting the call.
"""

import io

import pytest
from PIL import Image

from telegram_mcp.contact_sheet import (
    CELL_EDGE_PIXELS,
    LABEL_STRIP_PIXELS,
    MAXIMUM_TILES,
    ContactSheetError,
    compose_contact_sheet,
)


def _png(size, colour="red"):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _opened(data):
    return Image.open(io.BytesIO(data))


def test_a_grid_is_composed_with_a_label_strip_under_every_cell():
    tiles = [(_png((512, 512)), f"id={n}") for n in range(4)]

    data, meta = compose_contact_sheet(tiles)

    assert meta["columns"] == 2 and meta["rows"] == 2
    assert _opened(data).size == (
        2 * CELL_EDGE_PIXELS,
        2 * (CELL_EDGE_PIXELS + LABEL_STRIP_PIXELS),
    )
    assert [cell["label"] for cell in meta["cells"]] == ["id=0", "id=1", "id=2", "id=3"]


def test_every_cell_reports_where_it_sits():
    """The caller reads a position off the picture and an id out of the metadata;
    they have to agree or the sheet is a decoration."""
    data, meta = compose_contact_sheet([(_png((64, 64)), str(n)) for n in range(5)], columns=3)

    assert meta["columns"] == 3 and meta["rows"] == 2
    placed = {(cell["row"], cell["column"]): cell["label"] for cell in meta["cells"]}
    assert placed[(0, 0)] == "0" and placed[(0, 2)] == "2" and placed[(1, 1)] == "4"


def test_a_small_image_is_not_blown_up_into_invented_detail():
    """Upscaling a 64px avatar to the cell edge spends tokens on interpolation and
    tells the reader the source had detail it never had."""
    data, _meta = compose_contact_sheet([(_png((64, 64), "green"), "small")])

    sheet = _opened(data)
    assert sheet.size == (CELL_EDGE_PIXELS, CELL_EDGE_PIXELS + LABEL_STRIP_PIXELS)
    # The tile is centred at its own size, so the cell corner stays background.
    assert sheet.getpixel((2, 2)) != (0, 128, 0)


def test_a_tile_that_will_not_decode_keeps_its_label():
    """Losing the sheet over one bad tile loses every label on it, and the labels
    are the only way back to the other photos."""
    tiles = [(_png((128, 128)), "good"), (b"not an image", "id=404")]

    _data, meta = compose_contact_sheet(tiles)

    assert [cell["label"] for cell in meta["cells"]] == ["good", "id=404"]


def test_a_hostile_pixel_count_is_refused_before_it_is_decoded(monkeypatch):
    """Tiles come from a peer. `open_image_bytes` enforces MAX_DECODED_PIXELS, and
    a refusal has to become a placeholder rather than an exception that takes the
    whole sheet down with it."""
    from telegram_mcp import contact_sheet
    from telegram_mcp.visual.images import ImageError

    def _refuse(_data):
        raise ImageError("declares more pixels than the decode limit allows")

    monkeypatch.setattr(contact_sheet, "open_image_bytes", _refuse)

    _data, meta = compose_contact_sheet([(_png((64, 64)), "huge")])

    assert [cell["label"] for cell in meta["cells"]] == ["huge"]


def test_an_empty_sheet_is_refused_rather_than_returned_blank():
    with pytest.raises(ContactSheetError, match="no images"):
        compose_contact_sheet([])


def test_too_many_tiles_is_refused_rather_than_silently_truncated():
    """A sheet missing entries the caller asked for is worse than a refusal: nothing
    in the picture says which ones are absent."""
    tiles = [(_png((8, 8)), str(n)) for n in range(MAXIMUM_TILES + 1)]

    with pytest.raises(ContactSheetError) as caught:
        compose_contact_sheet(tiles)

    assert str(MAXIMUM_TILES) in str(caught.value)
    assert str(MAXIMUM_TILES + 1) in str(caught.value)
