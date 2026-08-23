"""The machine-value convention at the sites that used to skip it.

A machine value — a URL, a payload, a language tag, an emoji alt — is not prose,
so it gets a larger budget than a display name. It is still attacker-chosen, so
it is cleaned, and the cleaning is *announced* with a sibling ``<name>_altered``
key. The flag is the load-bearing half: an agent deciding whether to report or
follow a URL has to know the string it is holding was altered.

``telegram_mcp/button_view.py`` is the reference implementation. These tests pin
the four sites elsewhere that passed the raw value straight through.

The flag must be CONDITIONAL. An always-set flag carries no information and
trains the reader to ignore it, so every site is checked both ways: set when the
value moved, absent when it did not.
"""

import json
from types import SimpleNamespace

import pytest

import telegram_mcp.tools.buttons as buttons_tool

# From button_view, the origin and reference implementation of the convention -
# not from message_view, which only carries a local copy because importing the
# constant back from button_view would close an import cycle. Taking it from the
# origin also keeps these assertions about the CLEANING rather than about where
# the number happens to be defined.
from telegram_mcp.button_view import MAX_MACHINE_VALUE
from telegram_mcp.message_view import describe_entities
from telegram_mcp.tools.buttons import _resolve_icons, click_button

# RIGHT-TO-LEFT OVERRIDE: the classic way to make a URL read as something else.
BIDI = "‮"

# Long enough to have broken a prose-sized bound, short enough to be a real link.
BENIGN_URL = "https://example.test/" + "a" * (300 - len("https://example.test/"))


def _entity(cls_name, **fields):
    """An entity whose class NAME drives its reported type, as in Telethon."""
    fields.setdefault("offset", 0)
    fields.setdefault("length", 5)
    return type(cls_name, (SimpleNamespace,), {})(**fields)


def _described(entity, text="hello world"):
    return describe_entities(SimpleNamespace(message=text, entities=[entity]))[0]


# --- site 1: the entity URL behind sender-chosen display text ---------------


def test_an_entity_url_loses_a_bidi_override_and_says_so():
    item = _described(_entity("MessageEntityTextUrl", url=f"https://ok.test/{BIDI}evil"))

    assert BIDI not in item["url"]
    assert item["url_altered"] is True


def test_a_realistic_entity_url_survives_intact_and_unflagged():
    """Catches a bound set too low: a real Mini App link must not be cut."""
    item = _described(_entity("MessageEntityTextUrl", url=BENIGN_URL))

    assert item["url"] == BENIGN_URL
    assert "url_altered" not in item


def test_an_overlong_entity_url_is_bounded_to_the_machine_value_budget():
    item = _described(_entity("MessageEntityTextUrl", url="https://ok.test/" + "a" * 4000))

    assert len(item["url"]) == MAX_MACHINE_VALUE
    assert item["url_altered"] is True


# --- site 2: the language tag on a `pre` block ------------------------------


def test_a_pre_block_language_is_cleaned_and_flagged():
    item = _described(_entity("MessageEntityPre", language=f"py{BIDI}thon"))

    assert BIDI not in item["language"]
    assert item["language_altered"] is True


def test_an_ordinary_language_tag_is_left_alone_and_unflagged():
    item = _described(_entity("MessageEntityPre", language="python"))

    assert item["language"] == "python"
    assert "language_altered" not in item


# --- site 3: the URL a bot returns in its callback answer -------------------


class _Client:
    def __init__(self, msg, answer=None):
        self._msg, self._answer = msg, answer
        self.calls = []

    async def get_messages(self, entity, ids=None):
        return self._msg

    async def __call__(self, request):
        self.calls.append(request)
        return self._answer


@pytest.fixture
def _wire(monkeypatch):
    def use(answer):
        button = type("KeyboardButtonCallback", (SimpleNamespace,), {})(
            text="Confirm", data=b"cb:1", style=None
        )
        markup = type("ReplyInlineMarkup", (SimpleNamespace,), {})(
            rows=[SimpleNamespace(buttons=[button])]
        )
        client = _Client(SimpleNamespace(id=7, reply_markup=markup), answer)
        monkeypatch.setattr(buttons_tool, "get_client", lambda account=None: client)

        async def _connect(cl):
            return None

        async def _resolve(chat_id, cl):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(buttons_tool, "ensure_connected", _connect)
        monkeypatch.setattr(buttons_tool, "resolve_entity", _resolve)
        return client

    return use


async def _pressed(wire, url):
    wire(SimpleNamespace(message="done", alert=None, url=url))
    payload = json.loads(await click_button(1, 7, 0, account="default"))
    return payload["results"][0]


@pytest.mark.asyncio
async def test_a_callback_answer_url_is_cleaned_and_flagged(_wire):
    result = await _pressed(_wire, f"https://ok.test/{BIDI}evil")

    assert BIDI not in result["url"]
    assert result["url_altered"] is True


@pytest.mark.asyncio
async def test_a_benign_callback_answer_url_is_reported_verbatim(_wire):
    result = await _pressed(_wire, BENIGN_URL)

    assert result["url"] == BENIGN_URL
    assert "url_altered" not in result


@pytest.mark.asyncio
async def test_an_overlong_callback_answer_url_is_bounded(_wire):
    result = await _pressed(_wire, "https://ok.test/" + "a" * 4000)

    assert len(result["url"]) == MAX_MACHINE_VALUE
    assert result["url_altered"] is True


# --- site 4: the custom-emoji alt glyph -------------------------------------


async def _resolved_style(alt):
    """Drive _resolve_icons over one button carrying one custom-emoji icon."""
    document = SimpleNamespace(
        id=99,
        mime_type="image/webp",
        attributes=[SimpleNamespace(alt=alt)],
    )

    async def _client(_request):
        return [document]

    buttons = [{"style": {"icon_document_id": 99}}]
    await _resolve_icons(_client, buttons)
    return buttons[0]["style"]


@pytest.mark.asyncio
async def test_a_custom_emoji_alt_is_cleaned_and_flagged():
    style = await _resolved_style(f"\U0001f600{BIDI}")

    assert BIDI not in style["alt"]
    assert style["alt_altered"] is True


@pytest.mark.asyncio
async def test_a_plain_emoji_alt_is_left_alone_and_unflagged():
    """The alt is short prose, so it keeps display_name's own default bound -
    the same call media_preview.py makes on this field."""
    style = await _resolved_style("\U0001f600")

    assert style["alt"] == "\U0001f600"
    assert "alt_altered" not in style
