"""Saved GIFs: which request goes out, and what a caller has to carry between calls.

No network. The point of these assertions is the `file_reference`: a GIF is an
ordinary document, so Telegram issues a per-fetch reference and refuses an
`InputDocument` built without it. Every tool here reads the live object first,
and a refactor that "simplifies" that away by trusting a caller-supplied id would
work in a fake and fail against Telegram with FILE_REFERENCE_EXPIRED.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import saved_gifs as mod


def _gif(doc_id=5934007978150595964, mime="video/mp4", reference=b"ref-1"):
    return SimpleNamespace(
        id=doc_id,
        access_hash=99,
        file_reference=reference,
        mime_type=mime,
        size=1234,
        attributes=[
            SimpleNamespace(
                file_name="cat.mp4", __class__=type("DocumentAttributeFilename", (), {})
            )
        ],
    )


class _Client:
    """Answers the saved-GIF requests and records every one of them."""

    def __init__(self, saved=(), message=None):
        self.requests = []
        self.saved = list(saved)
        self.message = message

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetSavedGifsRequest":
            return SimpleNamespace(gifs=list(self.saved))
        if name == "SaveGifRequest":
            if request.unsave:
                self.saved = [d for d in self.saved if d.id != request.id.id]
            elif all(d.id != request.id.id for d in self.saved):
                self.saved.insert(0, _gif(request.id.id))
            return True
        raise AssertionError(f"unexpected request {name}")

    async def get_messages(self, entity, ids=None):
        return self.message

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def wire(monkeypatch):
    def _wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(target, _client):
            return SimpleNamespace(id=int(target) if str(target).lstrip("-").isdigit() else 1)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return _wire


def _records(answer):
    return json.loads(answer)["results"]


@pytest.mark.asyncio
async def test_a_gif_id_comes_back_as_a_string(wire):
    """A document id exceeds 2^53 and JSON has no integer that wide: through a
    float, 5934007978150595964 becomes 5934007978150595584 - a different GIF.
    The same rounding already made get_custom_emoji unusable once."""
    wire(_Client(saved=[_gif()]))

    answer = await mod.list_saved_gifs(account="a")

    assert _records(answer)[0]["document_id"] == "5934007978150595964"


@pytest.mark.asyncio
async def test_saving_sends_the_documents_real_file_reference(wire):
    """The assertion that matters. A GIF is an ordinary document, so Telegram
    refuses an InputDocument whose file_reference is empty - unlike a sticker
    already inside a set the account owns, where b"" is accepted. Building one
    from a caller-supplied id alone passes any fake and fails on the wire."""
    message = SimpleNamespace(document=_gif(reference=b"fresh-reference"))
    client = wire(_Client(message=message))

    await mod.save_gif(chat_id=-100123, message_id=7, account="a")

    sent = client.sent("SaveGifRequest")
    assert sent.unsave is False
    assert sent.id.file_reference == b"fresh-reference", "the reference was dropped"
    assert sent.id.access_hash == 99


@pytest.mark.asyncio
async def test_a_message_with_no_document_is_refused_and_names_why(wire):
    """get_gif_search hands back an inline-bot handle, not a document, and the
    commonest mistake is passing one here. Saying so beats "no document"."""
    client = wire(_Client(message=SimpleNamespace(document=None)))

    answer = await mod.save_gif(chat_id=-100123, message_id=7, account="a")

    assert "get_gif_search" in answer
    assert "SaveGifRequest" not in client.names, "a save went out for nothing"


@pytest.mark.asyncio
async def test_a_photo_is_refused_rather_than_saved_as_a_gif(wire):
    """Telegram will not show a non-animation in the GIF row. It does not error
    either, so the message would look saved and never appear."""
    client = wire(_Client(message=SimpleNamespace(document=_gif(mime="image/jpeg"))))

    answer = await mod.save_gif(chat_id=-100123, message_id=7, account="a")

    assert "image/jpeg" in answer
    assert "Nothing was saved" in answer
    assert "SaveGifRequest" not in client.names


@pytest.mark.asyncio
async def test_removing_looks_the_document_up_rather_than_trusting_the_caller(wire):
    """unsave takes an id only, so the live document - and its live reference -
    has to come from the saved list. A read before the write is the tool."""
    client = wire(_Client(saved=[_gif(doc_id=11, reference=b"live-ref"), _gif(doc_id=22)]))

    answer = await mod.unsave_gif(document_id=11, account="a")

    sent = client.sent("SaveGifRequest")
    assert sent.unsave is True
    assert sent.id.file_reference == b"live-ref"
    assert _records(answer)[0]["removed"] is True
    assert _records(answer)[0]["returned"] == 1


@pytest.mark.asyncio
async def test_removing_one_that_is_not_saved_says_so_instead_of_reporting_success(wire):
    """Telegram answers a removal of a GIF it never held with success, so passing
    it through would report a deletion that did not happen."""
    client = wire(_Client(saved=[_gif(doc_id=11)]))

    answer = await mod.unsave_gif(document_id=99, account="a")

    assert "nothing here to remove" in answer
    assert "SaveGifRequest" not in client.names, "a removal went out for a GIF not held"


@pytest.mark.asyncio
async def test_a_non_numeric_id_is_refused_before_any_request(wire):
    client = wire(_Client())

    answer = await mod.unsave_gif(document_id="not-an-id", account="a")

    assert "list_saved_gifs" in answer
    assert client.requests == []


def test_the_cap_is_reported_as_two_numbers_because_premium_doubles_it():
    """Written as a flat 200 first and corrected against a live account holding
    400. A single "documented" cap reads as authoritative and is wrong for every
    Premium account - and since Telegram drops the oldest silently rather than
    refusing, a caller trusting the wrong number loses GIFs."""
    assert mod.SAVED_GIFS_FREE_LIMIT == 200
    assert mod.SAVED_GIFS_PREMIUM_LIMIT == 400
    assert "200" in mod._LIMIT_NOTE and "400" in mod._LIMIT_NOTE
    assert "drops the oldest" in mod._LIMIT_NOTE


@pytest.mark.asyncio
async def test_the_listing_never_calls_its_window_a_total(wire):
    """Measured live: an account whose reply held 400 GIFs had one removed - it
    was confirmed absent from the next reply - and the reply still held 400. A
    removal cannot succeed AND leave the total unchanged, so `getSavedGifs`
    returns a capped window with more behind it.

    The field was called `saved_count`, which is how a caller diffs two 400s and
    concludes the removal did nothing. Every result reports `returned`, and every
    success is decided by whether the id is PRESENT rather than by arithmetic.
    """
    message = SimpleNamespace(document=_gif(doc_id=11))
    wire(_Client(saved=[_gif(doc_id=11), _gif(doc_id=22)], message=message))

    listed = json.loads(await mod.list_saved_gifs(account="a"))
    saved = json.loads(await mod.save_gif(chat_id=-100123, message_id=7, account="a"))
    removed = json.loads(await mod.unsave_gif(document_id=11, account="a"))

    assert "saved_count" not in json.dumps(saved), "a window is a total again"
    assert "saved_count" not in json.dumps(removed)
    assert "count" not in listed, "the listing calls its window a count"
    assert listed["returned"] == 2
    assert saved["results"][0]["returned"] == 2
    assert "never by the count" in mod._WINDOW_NOTE
