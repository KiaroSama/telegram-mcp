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
    def __init__(self, message=None, error=None):
        self.requests = []
        self.message = message
        self.error = error

    async def request(self, obj, timeout=30.0):
        self.requests.append(obj)
        if obj["@type"] == "getChat":
            return {"@type": "chat", "id": obj["chat_id"]}
        if self.error:
            raise self.error
        return self.message

    def types(self):
        return [r["@type"] for r in self.requests]


@pytest.fixture
def wire(monkeypatch):
    def _wire(message=None, error=None):
        client = FakeTDLib(message, error)
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
