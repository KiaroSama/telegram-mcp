"""What ``get_media_info`` hands back for a message's media.

No network: the client is a fake that answers one ``get_messages`` call, because
the only thing under test is the encoding of the answer. This tool used to return
Telethon's pretty-printed debug dump of the media object, which is prose built
out of sender-controlled fields -- a web page's title and description, a
document's filename, a sticker's alt text -- with no cleaning and no length
bound.

The two halves of the contract guarded here: the result is the JSON envelope that
marks a value as untrusted data rather than something a model should read as
instructions, and a media shape that cannot be rendered degrades to one honest
sentence instead of a traceback.
"""

import json

import pytest

from telegram_mcp.tools import media as mod
from telegram_mcp.tools.media import get_media_info

RLO = "‮"  # right-to-left override: makes text read as something else
ZWSP = "​"  # zero-width padding


class _Media:
    """Stands in for a Telethon media object that renders."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class MessageMediaUnsupported:
    """Stands in for a media type whose ``to_dict`` blows up."""

    def to_dict(self):
        raise TypeError("no serializer for this shape")


class _Message:
    def __init__(self, media):
        self.media = media


class _Client:
    """Answers the single get_messages call this tool makes."""

    def __init__(self, message):
        self._message = message
        self.calls = []

    async def get_messages(self, entity, ids=None):
        self.calls.append((entity, ids))
        return self._message


@pytest.fixture
def _wire(monkeypatch):
    def wire(message):
        client = _Client(message)
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve(chat_id, _client):
            return object()

        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


async def _info(wire, media):
    wire(_Message(media))
    return await get_media_info(1, 5, account="a")


# --------------------------------------------------------------------------
# the encoding


@pytest.mark.asyncio
async def test_media_info_is_json_not_prose(_wire):
    result = await _info(_wire, _Media({"_": "MessageMediaPhoto", "spoiler": False}))

    # The old dump could not be parsed at all; the envelope names its payload.
    assert json.loads(result)["results"] == [{"_": "MessageMediaPhoto", "spoiler": False}]


@pytest.mark.asyncio
async def test_no_media_still_answers_in_plain_words(_wire):
    _wire(_Message(None))

    assert await get_media_info(1, 5, account="a") == "No media found in the specified message."


# --------------------------------------------------------------------------
# the cleaning


@pytest.mark.asyncio
async def test_a_hostile_string_does_not_survive_at_any_depth(_wire):
    result = await _info(
        _wire,
        _Media(
            {
                "_": "MessageMediaWebPage",
                "webpage": {"title": f"{RLO}Invoice{ZWSP}", "site_name": f"pay{RLO}me"},
            }
        ),
    )

    assert RLO not in result
    assert ZWSP not in result
    page = json.loads(result)["results"][0]["webpage"]
    assert page["title"] == "Invoice"
    assert page["site_name"] == "payme"


@pytest.mark.asyncio
async def test_an_over_long_value_is_bounded(_wire):
    result = await _info(_wire, _Media({"description": "h" * 60000}))

    description = json.loads(result)["results"][0]["description"]
    assert 0 < len(description) < 60000


@pytest.mark.asyncio
async def test_a_benign_document_name_round_trips_unchanged(_wire):
    # A cleaner that mangles legitimate content is its own bug.
    result = await _info(_wire, _Media({"attributes": [{"file_name": "report.pdf"}]}))

    assert json.loads(result)["results"][0]["attributes"] == [{"file_name": "report.pdf"}]


@pytest.mark.asyncio
async def test_non_string_scalars_keep_their_types(_wire):
    result = await _info(_wire, _Media({"size": 4096, "spoiler": True, "ttl": None}))

    record = json.loads(result)["results"][0]
    assert record == {"size": 4096, "spoiler": True, "ttl": None}


# --------------------------------------------------------------------------
# the shape that cannot be rendered


@pytest.mark.asyncio
async def test_an_unrenderable_media_degrades_to_one_honest_sentence(_wire):
    result = await _info(_wire, MessageMediaUnsupported())

    # Names the class so the caller knows what was skipped, and stays prose --
    # a traceback or a half-written envelope would be worse than a plain answer.
    assert "MessageMediaUnsupported" in result
    assert "Could not render" in result
    assert "Traceback" not in result
    with pytest.raises(json.JSONDecodeError):
        json.loads(result)
