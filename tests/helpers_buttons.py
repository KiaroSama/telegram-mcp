"""Keyboards, and the client that answers a press, built in memory.

Shared because the button suite is split in two - what a keyboard IS and how it
is reported (`test_buttons.py`), and what happens when you press one
(`test_button_pressing.py`). Both halves need the same builders and the same
wired client, so they live here rather than in one file the other imports.

`make_wire` is a factory, not a fixture: each module wraps it in its own
three-line `_wire` fixture, which keeps flake8 quiet about a fixture
parameter shadowing an imported name.
"""

import json
from types import SimpleNamespace


import telegram_mcp.tools.buttons as buttons_tool
from telegram_mcp.tools.buttons import inspect_buttons


def _button(cls_name, **fields):
    """A button whose class NAME drives the description, as in Telethon."""
    fields.setdefault("style", None)
    return type(cls_name, (SimpleNamespace,), {})(**fields)


def _callback(text="Confirm", data=b"cb:1", **kw):
    return _button("KeyboardButtonCallback", text=text, data=data, **kw)


def _message(rows, message_id=7, inline=True):
    """A message with a keyboard. `inline` picks glass vs reply — Telethon
    distinguishes them by the markup CLASS, and both fill `rows`."""
    markup_cls = "ReplyInlineMarkup" if inline else "ReplyKeyboardMarkup"
    markup = type(markup_cls, (SimpleNamespace,), {})(
        rows=[SimpleNamespace(buttons=r) for r in rows]
    )
    return SimpleNamespace(id=message_id, reply_markup=markup)


_UNUSABLE_TOKEN = "b1:" + "0" * 32


class _Client:
    def __init__(self, msg, answer=None):
        self._msg, self._answer = msg, answer
        self.calls = []

    async def get_messages(self, entity, ids=None):
        return self._msg

    async def __call__(self, request):
        self.calls.append(request)
        return self._answer


def make_wire(monkeypatch):
    """The wired client, as a factory rather than a fixture.

    A fixture imported into two modules trips F811 the moment a test takes
    it as a parameter, so each module declares its own three-line fixture
    over this instead.
    """

    def use(msg, answer=None):
        client = _Client(msg, answer)
        monkeypatch.setattr(buttons_tool, "get_client", lambda account=None: client)

        async def _connect(cl):
            return None

        async def _resolve(chat_id, cl):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(buttons_tool, "ensure_connected", _connect)
        monkeypatch.setattr(buttons_tool, "resolve_entity", _resolve)
        return client

    return use


def _tokens_of(payload):
    """index -> press_token, from an inspect_buttons payload."""
    return {b["index"]: b.get("press_token") for b in payload["results"]}


async def _inspect(chat_id=1, message_id=7, account="default"):
    return json.loads(await inspect_buttons(chat_id, message_id, account=account))
