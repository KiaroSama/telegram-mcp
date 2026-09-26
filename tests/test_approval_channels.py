"""Where an approval comes from: somewhere the model cannot answer (ADR 0007).

Three channels, tried in order: the client's own dialog, an approval bot pressing a
button on the owner's phone, a code answered in Saved Messages from another device.
Each must refuse by default: a decline, a closed dialog, silence and every failure end
in "not approved". A failing channel hands over to the next; only when none is left is
the answer ``channel_failed``, and with none available at all it is ``no_channel``.
"""

import asyncio
from types import SimpleNamespace

import pytest

from telegram_mcp.safeguard import channels as ac


def _request(**overrides):
    fields = dict(tool="delete_message", account="main", chat="-100", effect="deletes 1 message")
    fields["identity"] = "main · 111 · @owner"
    fields.update(overrides)
    return ac.new_request(reasons=["gated"], **fields)


# --- the client's dialog -----------------------------------------------------------


class _Session:
    def __init__(self, action="accept", decision="once", elicitation=True, delay=0.0):
        caps = SimpleNamespace(elicitation=object() if elicitation else None)
        self.client_capabilities = caps
        self.action, self.decision, self.delay = action, decision, delay
        self.asked = []

    async def elicit_form(self, message, requested_schema, related_request_id=None):
        self.asked.append((message, requested_schema, related_request_id))
        await asyncio.sleep(self.delay)
        content = {"decision": self.decision} if self.action == "accept" else None
        return SimpleNamespace(action=self.action, content=content)


def test_the_dialog_is_used_only_when_the_client_can_show_one():
    assert ac.DialogChannel(_Session(), 7).available()
    assert not ac.DialogChannel(_Session(elicitation=False), 7).available()
    assert not ac.DialogChannel(SimpleNamespace(client_capabilities=None), 7).available()


@pytest.mark.parametrize(
    "action, decision, outcome",
    [
        ("accept", "once", "approved_once"),
        ("accept", "always", "approved_always"),
        ("accept", "deny", "declined"),
        ("decline", None, "declined"),
        ("cancel", None, "dismissed"),
    ],
)
def test_the_dialog_answer_maps_to_an_outcome(action, decision, outcome):
    session = _Session(action, decision)
    assert asyncio.run(ac.DialogChannel(session, 7).ask(_request(), 5)) == outcome
    message, schema, related = session.asked[0]
    assert related == 7
    assert "delete_message" in message and "deletes 1 message" in message
    assert schema["properties"]["decision"]["enum"] == ["once", "always", "deny"]


def test_a_dialog_nobody_answers_times_out():
    session = _Session(delay=5)
    assert asyncio.run(ac.DialogChannel(session, 7).ask(_request(), 0.05)) == "timed_out"


# --- the approval bot --------------------------------------------------------------


class _Client:
    def __init__(self, fail_for=()):
        self.sent = []
        self.edited = []
        self.edited_text = []
        self.handlers = []
        self.fail_for = set(fail_for)

    async def send_message(self, peer, text, **kwargs):
        if peer in self.fail_for:
            raise RuntimeError("user never started the bot")
        self.sent.append((peer, text, kwargs))
        return SimpleNamespace(id=len(self.sent) + 100)

    async def edit_message(self, peer, message_id, text=None, **kwargs):
        self.edited.append((peer, message_id, kwargs))
        self.edited_text.append(text)

    def add_event_handler(self, handler, event=None):
        self.handlers.append(handler)

    def remove_event_handler(self, handler, event=None):
        self.handlers.remove(handler)


def _provider(client):
    async def provide():
        return client

    return provide


def _owners(*ids):
    async def provide():
        return frozenset(ids)

    return provide


async def _answer_when_sent(client, answer, count=1):
    for _ in range(200):
        if len(client.sent) >= count:
            return answer()
        await asyncio.sleep(0.005)
    raise AssertionError("nothing was sent")


def _bot(client, *owners):
    return ac.BotChannel(owners_provider=_owners(*owners), client_provider=_provider(client))


def test_only_an_allowed_users_press_with_the_right_nonce_counts():
    client = _Client()
    bot = _bot(client, 111)
    request = _request()

    async def run():
        task = asyncio.create_task(bot.ask(request, 5))
        await _answer_when_sent(client, lambda: None)
        assert not bot.handle_callback(222, f"sg:{request.nonce}:once")  # a stranger
        assert not bot.handle_callback(111, "sg:wrongnonce:once")
        assert bot.handle_callback(111, f"sg:{request.nonce}:always".encode())
        return await task

    assert asyncio.run(run()) == "approved_always"
    peer, text, kwargs = client.sent[0]
    assert peer == 111 and "delete_message" in text
    labels = [b.text for row in kwargs["buttons"] for b in row]
    assert len(labels) == 3
    assert [b.type.data for row in kwargs["buttons"] for b in row] == [
        f"sg:{request.nonce}:{c}".encode() for c in ("once", "deny", "always")
    ]


def test_a_stranger_is_never_allowed_even_after_a_request():
    client = _Client()
    bot = _bot(client, 111)
    asyncio.run(bot.ask(_request(), 0.05))
    assert not bot.is_allowed(222)
    assert bot.is_allowed(111)


def test_every_allowed_account_gets_the_request_quoting_the_account():
    client = _Client(fail_for={333})
    bot = _bot(client, 111, 222, 333)
    request = _request()

    async def run():
        task = asyncio.create_task(bot.ask(request, 5))
        await _answer_when_sent(
            client, lambda: bot.handle_callback(222, f"sg:{request.nonce}:deny"), 2
        )
        return await task

    assert asyncio.run(run()) == "declined"
    assert sorted(peer for peer, _, _ in client.sent) == [111, 222]
    text = client.sent[0][1]
    assert "<blockquote>" in text and "main · 111 · @owner" in text
    assert client.sent[0][2].get("parse_mode") == "html"
    # The buttons are taken away once the request is decided, on every copy.
    assert sorted(peer for peer, _, _ in client.edited) == [111, 222]


def test_the_bot_fails_over_when_no_allowed_account_can_be_reached():
    client = _Client(fail_for={111})
    bot = _bot(client, 111)
    with pytest.raises(RuntimeError):
        asyncio.run(bot.ask(_request(), 1))


def test_a_press_after_the_deadline_is_ignored():
    client = _Client()
    bot = _bot(client, 111)
    request = _request()
    assert asyncio.run(bot.ask(request, 0.05)) == "timed_out"
    assert not bot.handle_callback(111, f"sg:{request.nonce}:once")


def test_the_bot_is_available_only_when_configured():
    assert not ac.BotChannel(owners_provider=_owners(111), client_provider=None).available()
    assert ac.BotChannel(
        owners_provider=_owners(111), client_provider=_provider(_Client())
    ).available()


def test_owner_ids_parse_from_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_APPROVAL_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("TELEGRAM_APPROVAL_OWNER_IDS", " 111, 222 ,x")
    monkeypatch.delenv("TELEGRAM_APPROVAL_OWNER_ID", raising=False)
    token, owners = ac.bot_settings()
    assert token == "1:abc" and owners == frozenset({111, 222})
    monkeypatch.delenv("TELEGRAM_APPROVAL_OWNER_IDS")
    assert ac.bot_settings()[1] == frozenset()


# --- Saved Messages ----------------------------------------------------------------


def test_a_saved_messages_reply_the_server_wrote_itself_is_not_an_approval():
    client = _Client()
    saved = ac.SavedMessagesChannel(client_provider=_provider(client))
    request = _request()

    async def run():
        task = asyncio.create_task(saved.ask(request, 5))
        await _answer_when_sent(client, lambda: None)
        # The request message itself mentions "yes <code>".
        assert not saved.handle_message(101, f"yes {request.code}")
        assert not saved.handle_message(500, "yes ZZZZ")  # another code
        assert saved.handle_message(501, f"  YES {request.code.lower()} ")
        return await task

    assert asyncio.run(run()) == "approved_once"
    assert client.sent[0][0] == "me"
    assert not client.handlers  # the listener is gone afterwards


@pytest.mark.parametrize("word, outcome", [("always", "approved_always"), ("no", "declined")])
def test_saved_messages_words(word, outcome):
    client = _Client()
    saved = ac.SavedMessagesChannel(client_provider=_provider(client))
    request = _request()

    async def run():
        task = asyncio.create_task(saved.ask(request, 5))
        await _answer_when_sent(
            client, lambda: saved.handle_message(900, f"{word} {request.code}")
        )
        return await task

    assert asyncio.run(run()) == outcome


def test_two_pending_requests_never_approve_each_other():
    client = _Client()
    saved = ac.SavedMessagesChannel(client_provider=_provider(client))
    first, second = _request(), _request(chat="-200")
    assert first.code != second.code and first.nonce != second.nonce

    async def run():
        a = asyncio.create_task(saved.ask(first, 0.3))
        b = asyncio.create_task(saved.ask(second, 5))
        for _ in range(200):
            if len(client.sent) == 2:
                break
            await asyncio.sleep(0.005)
        saved.handle_message(900, f"yes {second.code}")
        return await a, await b

    assert asyncio.run(run()) == ("timed_out", "approved_once")


# --- choosing a channel -------------------------------------------------------------


class _Fixed:
    def __init__(self, kind, outcome=None, available=True, error=None):
        self.kind, self.outcome, self._available, self.error = kind, outcome, available, error
        self.asked = 0

    def available(self):
        return self._available

    async def ask(self, request, timeout):
        self.asked += 1
        if self.error:
            raise self.error
        return self.outcome


def test_a_failing_channel_hands_over_to_the_next():
    broken = _Fixed("dialog", error=RuntimeError("boom"))
    bot = _Fixed("bot", "approved_once")
    outcome, kind, failures = asyncio.run(ac.request_approval(_request(), [broken, bot], 5))
    assert (outcome, kind) == ("approved_once", "bot")
    assert failures == ["dialog: RuntimeError"]


def test_every_channel_failing_is_channel_failed_never_approved():
    channels = [_Fixed("dialog", error=RuntimeError()), _Fixed("bot", error=OSError())]
    outcome, kind, failures = asyncio.run(ac.request_approval(_request(), channels, 5))
    assert (outcome, kind) == ("channel_failed", None)
    assert failures == ["dialog: RuntimeError", "bot: OSError"]


def test_no_available_channel_is_no_channel():
    channels = [_Fixed("dialog", available=False), _Fixed("bot", available=False)]
    assert asyncio.run(ac.request_approval(_request(), channels, 5))[0] == "no_channel"
    assert channels[0].asked == channels[1].asked == 0


def test_a_decline_does_not_fall_through_to_another_channel():
    first, second = _Fixed("dialog", "declined"), _Fixed("bot", "approved_once")
    assert asyncio.run(ac.request_approval(_request(), [first, second], 5))[0] == "declined"
    assert second.asked == 0


def test_the_code_is_pending_only_while_the_request_is_open():
    seen = []

    class _Peek(_Fixed):
        async def ask(self, request, timeout):
            seen.append(request.code in ac.pending_codes())
            return "declined"

    request = _request()
    asyncio.run(ac.request_approval(request, [_Peek("dialog")], 5))
    assert seen == [True]
    assert request.code not in ac.pending_codes()


def test_the_deadline_defaults_to_five_minutes():
    assert ac.timeout_seconds("") == 300
    assert ac.timeout_seconds("30") == 30
    assert ac.timeout_seconds("nonsense") == 300
    assert ac.timeout_seconds("-5") == 300


@pytest.mark.parametrize(
    "press, outcome, line",
    [
        ("once", "approved_once", "✅ Approved"),
        ("always", "approved_always", "♾ Always approved"),
        ("deny", "declined", "❌ Denied"),
        (None, "timed_out", "⏱ Timed out - not run"),
    ],
)
def test_a_closed_request_shows_its_outcome_on_every_copy(press, outcome, line):
    """FR-039: no buttons left, and one line saying what happened."""
    client = _Client()
    bot = _bot(client, 111, 222)
    request = _request()

    async def run():
        task = asyncio.create_task(bot.ask(request, 5 if press else 0.05))
        if press:
            await _answer_when_sent(
                client, lambda: bot.handle_callback(111, f"sg:{request.nonce}:{press}"), 2
            )
        return await task

    assert asyncio.run(run()) == outcome
    assert sorted(peer for peer, _, _ in client.edited) == [111, 222]
    assert all(kwargs.get("buttons") is None for _, _, kwargs in client.edited)
    assert all(text.endswith(line) and "delete_message" in text for text in client.edited_text)


@pytest.mark.parametrize(
    "me, expected",
    [
        (
            SimpleNamespace(id=5899781975, username="refx_nexus_3", usernames=None),
            "refx_nexus_3 · 5899781975 · @refx_nexus_3",
        ),
        (
            SimpleNamespace(
                id=5899781975,
                username=None,
                usernames=[
                    SimpleNamespace(username="old_name", active=False),
                    SimpleNamespace(username="refx_nexus_3", active=True),
                ],
            ),
            "refx_nexus_3 · 5899781975 · @refx_nexus_3",
        ),
        (SimpleNamespace(id=7, username=None, usernames=None), "refx_nexus_3 · 7"),
    ],
)
def test_the_quote_names_the_accounts_username_with_an_at(monkeypatch, me, expected):
    """The bot quote shows @username even when Telegram keeps it only in `usernames`."""
    from telegram_mcp.safeguard import wiring

    async def _me(account):
        return me

    monkeypatch.setattr(wiring, "_me", _me)
    monkeypatch.setattr(wiring, "_identities", {})
    assert asyncio.run(wiring.identity("refx_nexus_3")) == expected


def test_everything_the_bot_and_saved_messages_show_is_english():
    """FR-041: the approval texts are English - buttons, outcome lines, tap answers."""
    import re

    shown = [ac._APPROVE, ac._DENY, ac._ALWAYS, ac._CLOSED_LINE, ac.ANSWERED, ac.NOT_OPEN]
    shown += list(ac._OUTCOME_LINES.values())
    request = _request()
    shown += [request.text(), request.html()]
    assert all(not re.search(r"[؀-ۿ]", text) for text in shown), shown
    assert [ac._APPROVE, ac._DENY, ac._ALWAYS] == ["✅ Approve", "❌ Deny", "♾ Always approve"]
