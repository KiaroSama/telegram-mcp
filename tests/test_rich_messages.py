"""Reading a table that MTProto reports as an empty message.

The fixtures are the SHAPE of a real message: a two-column, three-row table with
a merged bottom cell, a captioned link and bold runs, captured from a live
`getMessage` on a message Telethon returned as `[empty]`. Inventing a shape here
would have tested the renderer against my idea of TDLib rather than TDLib.
"""

import json

import pytest

from telegram_mcp.tdlib import TDLibError
from telegram_mcp.tools import rich_messages as rm


def _plain(text):
    return {"@type": "richTextPlain", "text": text}


def _cell(text, **kw):
    cell = {"@type": "pageTableCell", "text": text, "is_header": False}
    cell.update(kw)
    return cell


# The real message, reduced to what the renderer touches.
TABLE_MESSAGE = {
    "content": {
        "@type": "messageRichMessage",
        "message": {
            "@type": "richMessage",
            "is_rtl": False,
            "blocks": [
                {
                    "@type": "pageBlockTable",
                    "is_bordered": True,
                    "caption": {
                        "@type": "richTextUrl",
                        "text": _plain("ShaparakVPN | Services"),
                        "url": "https://t.me/shaparakvpn",
                    },
                    "cells": [
                        [
                            _cell(
                                {
                                    "@type": "richTexts",
                                    "texts": [
                                        {
                                            "@type": "richTextBold",
                                            "text": _plain("Chatgpt plus"),
                                        },
                                        _plain(" personal email"),
                                    ],
                                }
                            ),
                            _cell(_plain("v2ray residential")),
                        ],
                        [_cell(_plain("Gemini pro")), _cell(_plain("panel | multi"))],
                        [_cell(_plain("other subscriptions"), colspan=2)],
                    ],
                }
            ],
        },
    }
}


class FakeTDLib:
    def __init__(self, message=None, error=None, downloaded=None):
        self.requests = []
        self.message = message
        self.error = error
        self.downloaded = downloaded

    async def request(self, obj, timeout=30.0):
        self.requests.append(obj)
        if obj["@type"] == "getChat":
            return {"@type": "chat", "id": obj["chat_id"]}
        if self.error:
            raise self.error
        if obj["@type"] == "downloadFile":
            return self.downloaded
        return self.message

    def types(self):
        return [r["@type"] for r in self.requests]


@pytest.fixture
def wire(monkeypatch):
    def _wire(message=None, error=None, downloaded=None):
        client = FakeTDLib(message, error, downloaded)
        monkeypatch.setattr(rm, "account_label", lambda account=None: "acct")

        async def _client(label):
            return client

        monkeypatch.setattr(rm, "secret_client", _client)
        return client

    return _wire


def _results(raw):
    return json.loads(raw)["results"]


# --------------------------------------------------------------------------
# The identifier boundary
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_message_id_is_shifted_into_tdlibs_numbering(wire):
    """TDLib stores `server_id << 20`. Passing the caller's id straight through
    asks for a message roughly a million times younger - which exists, and is
    somebody else's."""
    client = wire(TABLE_MESSAGE)

    await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")

    (asked,) = [r for r in client.requests if r["@type"] == "getMessage"]
    assert asked["message_id"] == 4614680 << 20
    assert asked["message_id"] != 4614680


@pytest.mark.asyncio
async def test_the_chat_is_fetched_before_the_message(wire):
    """TDLib answers from its own database. Skipping `getChat` on a chat it has
    never seen fails with an error about the MESSAGE, which sends the reader to
    check a message id that was right all along."""
    client = wire(TABLE_MESSAGE)

    await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")

    assert client.types() == ["getChat", "getMessage"]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_cell_of_the_table_survives(wire):
    wire(TABLE_MESSAGE)

    results = _results(
        await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")
    )

    (block,) = results["blocks"]
    assert block["type"] == "pageBlockTable"
    assert block["row_count"] == 3
    assert block["column_count"] == 2
    flat = " ".join(cell["text"] for row in block["rows"] for cell in row)
    for expected in ("Chatgpt plus", "v2ray residential", "Gemini pro", "other subscriptions"):
        assert expected in flat, f"{expected!r} was lost"


@pytest.mark.asyncio
async def test_a_merged_cell_is_reported_rather_than_faked(wire):
    """Markdown cannot express a colspan. Duplicating or dropping the cell to
    make the grid rectangular would misreport what the table actually says, so
    the span is carried in the structured rows instead."""
    wire(TABLE_MESSAGE)

    results = _results(
        await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")
    )

    merged = results["blocks"][0]["rows"][2][0]
    assert merged["colspan"] == 2
    assert merged["text"] == "other subscriptions"


@pytest.mark.asyncio
async def test_nested_formatting_is_flattened_not_dropped(wire):
    """A cell is a TREE: bold wrapping plain, beside more plain. A flattener that
    only handled the outer node would return an empty cell and nothing would say
    text had been lost."""
    wire(TABLE_MESSAGE)

    results = _results(
        await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")
    )

    first = results["blocks"][0]["rows"][0][0]["text"]
    assert "Chatgpt plus" in first
    assert "personal email" in first, "the sibling text beside the bold run was dropped"


@pytest.mark.asyncio
async def test_a_captions_link_keeps_its_destination(wire):
    """ "ShaparakVPN | Services" without its URL is the half that does not
    matter."""
    wire(TABLE_MESSAGE)

    results = _results(
        await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")
    )

    assert "https://t.me/shaparakvpn" in results["blocks"][0]["caption"]


@pytest.mark.asyncio
async def test_the_markdown_view_is_a_usable_table(wire):
    wire(TABLE_MESSAGE)

    results = _results(
        await rm.read_rich_message(chat_id=-1002032650056, message_id=4614680, account="acct")
    )

    markdown = results["blocks"][0]["markdown"]
    lines = markdown.splitlines()
    assert lines[1].startswith("|"), "no separator row, so it is not a table"
    assert set(lines[1].replace("|", "").replace(" ", "")) == {"-"}
    # A pipe inside a cell would end the column early and shift every value
    # after it into the wrong header.
    assert "\\|" in markdown, "a pipe inside a cell was not escaped"


# --------------------------------------------------------------------------
# Refusing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_message_is_sent_back_to_inspect_message(wire):
    """This tool exists for one content type. Answering with an empty block list
    for anything else would read as "the message is empty", which is the very
    confusion it was built to end."""
    wire({"content": {"@type": "messageText", "text": {"text": "hello"}}})

    results = _results(await rm.read_rich_message(chat_id=1, message_id=2, account="acct"))

    assert results["content_type"] == "messageText"
    assert "inspect_message" in results["note"]
    assert "blocks" not in results


@pytest.mark.asyncio
async def test_telegrams_refusal_is_shown_not_filed_under_a_code(wire):
    wire(error=TDLibError(400, "MESSAGE_ID_INVALID"))

    answer = await rm.read_rich_message(chat_id=1, message_id=2, account="acct")

    assert "MESSAGE_ID_INVALID" in answer


def test_an_unknown_rich_text_wrapper_still_yields_its_words():
    """TDLib will add wrappers this does not know. The words matter more than
    the decoration, so an unrecognised node contributes its content rather than
    silently nothing."""
    node = {"@type": "richTextSomethingNewIn2027", "text": _plain("still here")}

    assert rm._flatten(node) == "still here"


def test_flattening_never_raises_on_a_shape_it_has_not_met():
    for odd in (None, "", [], {}, {"@type": "richTextPlain"}, 7):
        assert isinstance(rm._flatten(odd), str)


def test_a_premium_emoji_in_a_rich_block_is_not_dropped():
    """`richTextCustomEmoji`, NOT `richTextIcon` - and the difference cost a
    wrong conclusion. One real message carried 23 of these and came back with
    every one missing, which read as "rich messages cannot hold premium emoji";
    they hold them perfectly well, and two send syntaxes that had been declared
    broken turned out to have worked all along."""
    node = {
        "@type": "richTextCustomEmoji",
        "custom_emoji_id": "5814678646908003214",
        "alternative_text": "👍",
    }

    rendered = rm._flatten(node)

    assert "👍" in rendered, "the fallback glyph is the only text the emoji has"
    assert "5814678646908003214" in rendered, "without the id the emoji is not reproducible"


def test_a_custom_emoji_nested_in_bold_inside_a_cell_survives():
    """The real shape: emoji sit inside `richTexts` beside `richTextBold` runs,
    so a branch that only handles the top level still loses them."""
    cell = {
        "@type": "richTexts",
        "texts": [
            {"@type": "richTextBold", "text": {"@type": "richTextPlain", "text": "Chatgpt plus "}},
            {
                "@type": "richTextCustomEmoji",
                "custom_emoji_id": "5472246178617765188",
                "alternative_text": "🎨",
            },
        ],
    }

    rendered = rm._flatten(cell)

    assert "Chatgpt plus" in rendered
    assert "5472246178617765188" in rendered


def test_an_inline_document_is_still_reported_as_an_icon():
    """`richTextIcon` is a sticker or image, not an emoji - it has no glyph and
    no id to give back, so naming it stays the honest answer."""
    assert rm._flatten({"@type": "richTextIcon"}) == "[icon]"


# --- the composer's other formats, all of which used to come back empty ------


def test_a_blockquote_keeps_the_blocks_inside_it():
    """These containers hold NESTED BLOCKS, not rich text. Reading `text` on one
    returns nothing, which is why a quote came back as a bare `{}` with its
    whole contents missing."""
    block = {
        "@type": "pageBlockBlockQuote",
        "blocks": [
            {"@type": "pageBlockParagraph", "text": {"@type": "richTextPlain", "text": "a quote"}}
        ],
    }

    out = rm._render_block(block)

    assert out["blocks"][0]["text"] == "a quote"


def test_a_list_keeps_its_items_and_their_labels():
    block = {
        "@type": "pageBlockList",
        "items": [
            {
                "@type": "pageBlockListItem",
                "label": {"@type": "richTextPlain", "text": "1."},
                "blocks": [
                    {
                        "@type": "pageBlockParagraph",
                        "text": {"@type": "richTextPlain", "text": "first"},
                    }
                ],
            },
        ],
    }

    out = rm._render_block(block)

    assert out["item_count"] == 1
    assert out["items"][0]["label"] == "1."
    assert out["items"][0]["blocks"][0]["text"] == "first"


def test_a_checklist_item_reports_its_box_and_whether_it_is_ticked():
    """A checklist and a bullet list are the SAME block type; only these two
    flags separate "todo" from "point"."""
    block = {
        "@type": "pageBlockList",
        "items": [
            {
                "@type": "pageBlockListItem",
                "has_checkbox": True,
                "is_checked": True,
                "blocks": [
                    {
                        "@type": "pageBlockParagraph",
                        "text": {"@type": "richTextPlain", "text": "done"},
                    }
                ],
            }
        ],
    }

    item = rm._render_block(block)["items"][0]

    assert item["checkbox"] is True and item["checked"] is True


def test_a_bullet_item_has_no_checkbox_keys_at_all():
    block = {"@type": "pageBlockList", "items": [{"@type": "pageBlockListItem", "blocks": []}]}

    item = rm._render_block(block)["items"][0]

    assert "checkbox" not in item and "checked" not in item


def test_a_details_block_keeps_its_summary_body_and_open_state():
    block = {
        "@type": "pageBlockDetails",
        "header": {"@type": "richTextPlain", "text": "the summary"},
        "is_open": False,
        "blocks": [
            {
                "@type": "pageBlockParagraph",
                "text": {"@type": "richTextPlain", "text": "hidden body"},
            }
        ],
    }

    out = rm._render_block(block)

    assert out["header"] == "the summary"
    assert out["is_open"] is False
    assert out["blocks"][0]["text"] == "hidden body"


def test_a_divider_is_reported_by_its_type_and_carries_nothing():
    assert rm._render_block({"@type": "pageBlockDivider"}) == {"type": "pageBlockDivider"}


def test_every_emphasis_the_composer_offers_survives_the_round_trip():
    """Without these the words come back and the formatting does not, so an
    underlined warning and a plain sentence read identically."""
    for kind, marker in (
        ("richTextUnderline", "__"),
        ("richTextMarked", "=="),
        ("richTextSubscript", "~"),
        ("richTextSuperscript", "^"),
    ):
        node = {"@type": kind, "text": {"@type": "richTextPlain", "text": "x"}}

        assert rm._flatten(node) == f"{marker}x{marker}", kind


def test_cell_alignment_is_reported():
    """A centred table and a left-aligned one used to read back identical - a
    reproduction matched every field this tool returned and was still wrong."""
    block = {
        "@type": "pageBlockTable",
        "cells": [
            [
                {
                    "@type": "pageBlockTableCell",
                    "text": {"@type": "richTextPlain", "text": "c"},
                    "align": {"@type": "pageBlockHorizontalAlignmentCenter"},
                    "valign": {"@type": "pageBlockVerticalAlignmentTop"},
                }
            ]
        ],
    }

    cell = rm._render_block(block)["rows"][0][0]

    assert cell["align"] == "center" and cell["valign"] == "top"


def test_a_spoiler_comes_back_marked():
    """It read back as ordinary words, and that was diagnosed as "Telegram keeps
    no spoiler node" - twice wrong. `richTextSpoiler` exists and carries `text`
    like the rest of its family; the generic fallback took the words and dropped
    the marker, exactly as it once did to premium emoji."""
    node = {"@type": "richTextSpoiler", "text": {"@type": "richTextPlain", "text": "hidden"}}

    assert rm._flatten(node) == "||hidden||"


def test_an_inline_formula_keeps_its_expression():
    """The one node that does NOT hold its content under `text`. The fallback
    found no `text`, returned nothing, and an inline formula vanished from the
    paragraph with no sign it had ever been there."""
    node = {"@type": "richTextMathematicalExpression", "expression": "E=mc^2"}

    assert rm._flatten(node) == "$E=mc^2$"


def test_a_formula_block_keeps_its_expression():
    block = {"@type": "pageBlockMathematicalExpression", "expression": "a^2+b^2=c^2"}

    assert rm._render_block(block) == {
        "type": "pageBlockMathematicalExpression",
        "expression": "a^2+b^2=c^2",
    }


def test_a_photo_block_reports_the_picture_and_its_full_size():
    """A photo record has no width or height of its own - the sizes ARE the
    picture - so reading the record alone reports a photo of no dimensions."""
    block = {
        "@type": "pageBlockPhoto",
        "photo": {
            "@type": "photo",
            "sizes": [
                {"@type": "photoSize", "type": "s", "width": 90, "height": 90},
                {"@type": "photoSize", "type": "x", "width": 640, "height": 480},
            ],
        },
        "has_spoiler": True,
    }

    record = rm._render_block(block)

    assert record["media"] == {"kind": "photo", "width": 640, "height": 480}
    assert record["has_spoiler"] is True


def test_a_track_reports_what_it_is_rather_than_an_empty_record():
    """Every media block came back as a bare `{}`: its content hangs off its own
    key and is a media record, so reading `text` on one finds nothing."""
    block = {
        "@type": "pageBlockAudio",
        "audio": {
            "@type": "audio",
            "duration": 1,
            "file_name": "tone.mp3",
            "mime_type": "audio/mpeg",
            "title": "a tone",
        },
    }

    assert rm._render_block(block)["media"] == {
        "kind": "audio",
        "duration": 1,
        "file_name": "tone.mp3",
        "mime_type": "audio/mpeg",
        "title": "a tone",
    }


def test_a_map_block_reports_where_it_points():
    block = {
        "@type": "pageBlockMap",
        "location": {"@type": "location", "latitude": 35.689214, "longitude": 51.38901},
        "zoom": 12,
        "width": 800,
        "height": 400,
    }

    record = rm._render_block(block)

    assert record["location"] == {"latitude": 35.689214, "longitude": 51.38901}
    assert record["zoom"] == 12


def test_a_button_keeps_its_label_and_target():
    """A button is not text with a link: the label hangs off an `inlineButton`
    record, so nothing sits under `text` and a whole button read back as an
    empty paragraph."""
    node = {
        "@type": "richTextButton",
        "button": {
            "@type": "inlineButton",
            "text": {"@type": "richTextPlain", "text": "go"},
            "type": {"@type": "inlineKeyboardButtonTypeUrl", "url": "https://telegram.org/"},
        },
    }

    assert rm._flatten(node) == "[go]<tg-button url=https://telegram.org/>"


def test_a_button_without_a_url_is_named_by_its_type():
    """Not every button is a link, and losing the label to report nothing at all
    would be worse than saying which kind arrived."""
    node = {
        "@type": "richTextButton",
        "button": {
            "text": {"@type": "richTextPlain", "text": "pay"},
            "type": {"@type": "inlineKeyboardButtonTypeBuy"},
        },
    }

    assert rm._flatten(node) == "[pay]<tg-button url=inlineKeyboardButtonTypeBuy>"


def test_a_gallery_reports_the_pictures_inside_it():
    """A collage holds its photos as nested blocks and only its caption sits
    where the fallback looks, so two pictures read back as one word."""
    photo = {
        "@type": "pageBlockPhoto",
        "photo": {"sizes": [{"width": 64, "height": 64}]},
    }
    block = {
        "@type": "pageBlockCollage",
        "blocks": [photo, photo],
        "caption": {
            "@type": "pageBlockCaption",
            "text": {"@type": "richTextPlain", "text": "two"},
        },
    }

    record = rm._render_block(block)

    assert record["block_count"] == 2
    assert [b["type"] for b in record["blocks"]] == ["pageBlockPhoto", "pageBlockPhoto"]
    assert record["caption"] == "two"


def test_a_slideshow_is_read_the_same_way_as_a_collage():
    block = {"@type": "pageBlockSlideshow", "blocks": [{"@type": "pageBlockDivider"}]}

    assert rm._render_block(block)["block_count"] == 1


def test_a_long_cell_is_not_cut_to_a_display_name_length():
    """Cell text went through `sanitize_name`, which exists for usernames and
    chat titles: it caps at 256 characters and flattens newlines. A rich message
    is body content by definition, so a real advert's decorative emoji strip came
    back ending in `... [truncated]` and the tool quietly described a message
    that was not the one in the chat."""
    body = "x" * 400 + "\nsecond line"
    block = {
        "@type": "pageBlockTable",
        "cells": [
            [
                {
                    "@type": "pageBlockTableCell",
                    "text": {"@type": "richTextPlain", "text": body},
                }
            ]
        ],
        "caption": {"@type": "richTextPlain", "text": "y" * 400},
    }

    out = rm._render_block(block)

    assert out["rows"][0][0]["text"] == body, "the cell was cut or its newline flattened"
    assert "truncated" not in out["caption"] and len(out["caption"]) == 400


# --------------------------------------------------------------------------
# Getting the bytes out of a rich message
# --------------------------------------------------------------------------


def _photo_message(file_obj):
    return {
        "content": {
            "@type": "messageRichMessage",
            "message": {
                "blocks": [{"@type": "pageBlockPhoto", "photo": {"sizes": [{"photo": file_obj}]}}]
            },
        }
    }


def test_a_photo_blocks_file_is_found_under_its_last_size():
    """A photo has no file of its own - the sizes ARE the picture - so indexing
    the block key the way every other kind does returns nothing."""
    handle = {"id": 7, "size": 99}
    block = {
        "@type": "pageBlockPhoto",
        "photo": {"sizes": [{"photo": {"id": 1}}, {"photo": handle}]},
    }

    assert rm._block_file(block) is handle


def test_a_voice_notes_file_is_not_under_the_block_key():
    """TDLib calls it `voice`, not `voice_note`. Deriving the inner key from the
    block key silently returned None for the one kind that differs."""
    handle = {"id": 3}
    block = {"@type": "pageBlockVoiceNote", "voice_note": {"voice": handle}}

    assert rm._block_file(block) is handle


def test_the_reader_publishes_the_file_id_so_it_can_be_fetched():
    """It used to report a photo's width and height and drop the handle, leaving
    a caller able to see the picture existed and unable to ask for it."""
    block = {"@type": "pageBlockPhoto", "photo": {"sizes": [{"photo": {"id": 42}, "width": 8}]}}

    assert rm._render_block(block)["media"]["file_id"] == 42


def test_a_half_downloaded_file_reports_no_path():
    """`path` is set while a transfer is still running, so trusting it without
    `is_downloading_completed` hands back a partial file."""
    partial = {"id": 1, "local": {"path": "C:/half.jpg", "is_downloading_completed": False}}
    whole = {"id": 2, "local": {"path": "C:/whole.jpg", "is_downloading_completed": True}}

    assert (
        "local_path"
        not in rm._render_block(
            {"@type": "pageBlockPhoto", "photo": {"sizes": [{"photo": partial}]}}
        )["media"]
    )
    assert (
        rm._render_block({"@type": "pageBlockPhoto", "photo": {"sizes": [{"photo": whole}]}})[
            "media"
        ]["local_path"]
        == "C:/whole.jpg"
    )


@pytest.mark.asyncio
async def test_a_file_tdlib_already_holds_is_not_downloaded_again(wire):
    cached = {
        "id": 5,
        "size": 12,
        "local": {"path": "C:/have.jpg", "is_downloading_completed": True},
    }
    client = wire(_photo_message(cached))

    answer = _results(await rm.download_rich_media(-100123, 970, account="acct"))

    assert answer["path"] == "C:/have.jpg" and answer["file_id"] == 5
    assert "downloadFile" not in client.types(), "asked for bytes it already had"


@pytest.mark.asyncio
async def test_a_file_tdlib_lacks_is_fetched_and_its_path_returned(wire):
    client = wire(
        _photo_message({"id": 9, "local": {"is_downloading_completed": False}}),
        downloaded={"size": 77, "local": {"path": "C:/got.jpg", "is_downloading_completed": True}},
    )

    answer = _results(await rm.download_rich_media(-100123, 970, account="acct"))

    assert answer["path"] == "C:/got.jpg" and answer["size_bytes"] == 77
    assert "downloadFile" in client.types()


@pytest.mark.asyncio
async def test_an_unfinished_transfer_is_reported_rather_than_returned(wire):
    """A path from an incomplete download is a truncated file wearing the name
    of a whole one."""
    wire(
        _photo_message({"id": 9, "local": {"is_downloading_completed": False}}),
        downloaded={"local": {"path": "C:/partial.jpg", "is_downloading_completed": False}},
    )

    answer = _results(await rm.download_rich_media(-100123, 970, account="acct"))

    assert answer["saved"] is False and "timeout" in answer["reason"]


@pytest.mark.asyncio
async def test_a_block_without_media_says_so_instead_of_failing(wire):
    wire(
        {
            "content": {
                "@type": "messageRichMessage",
                "message": {"blocks": [{"@type": "pageBlockDivider"}]},
            }
        }
    )

    assert "carries media" in await rm.download_rich_media(-100123, 970, account="acct")
