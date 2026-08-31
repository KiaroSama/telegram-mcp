"""The transport around Telegram's library, which is the only part written here.

TDLib does the cryptography; this module does correlation, routing and lifecycle,
and every one of those is a decision that can be wrong in a way no live test would
notice. A response delivered to the wrong waiter, an update queue that drops the
newest instead of the oldest, `use_secret_chats` quietly false — each produces a
working-looking client that is subtly wrong.

The binary is never loaded here: `_tdjson` is patched with a recorder, so these run
identically on a machine that has never installed the optional dependency, which is
what CI is.
"""

import asyncio
import json

import pytest

from telegram_mcp import tdlib


class FakeTdjson:
    """Stands in for the native module, recording what was sent."""

    def __init__(self):
        self.sent = []
        self.executed = []
        self.next_client_id = 7

    def td_create_client_id(self):
        return self.next_client_id

    def td_send(self, client_id, payload):
        self.sent.append((client_id, json.loads(payload.decode())))

    def td_receive(self, timeout):  # pragma: no cover - the reader thread is not run
        return None

    def td_execute(self, payload):
        request = json.loads(payload.decode())
        self.executed.append(request)
        if request.get("@type") == "getOption":
            return json.dumps({"@type": "optionValueString", "value": "1.8.67"}).encode()
        return json.dumps({"@type": "ok"}).encode()


@pytest.fixture
def fake(monkeypatch):
    module = FakeTdjson()
    monkeypatch.setattr(tdlib, "_tdjson", lambda: module)
    monkeypatch.setattr(tdlib, "_ensure_reader", lambda: None)
    monkeypatch.setattr(tdlib, "_clients", {})
    return module


async def _started(fake, tmp_path, account="acct"):
    """A client past `start`, with TDLib's own first update delivered."""
    client = tdlib.TDLibClient(account, database_dir=tmp_path / account)
    task = asyncio.ensure_future(client.start())
    await asyncio.sleep(0)
    client._handle_on_loop(
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateReady"},
        }
    )
    assert await task == "authorizationStateReady"
    return client


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_chats_are_switched_on_in_the_parameters(fake, tmp_path):
    """The one flag the whole feature rests on.

    With `use_secret_chats` false TDLib does not error - it simply never delivers a
    secret-chat update, so the failure looks like an unresponsive peer rather than
    a misconfiguration.
    """
    client = tdlib.TDLibClient("acct", database_dir=tmp_path / "db")

    params = client._parameters()

    assert params["use_secret_chats"] is True
    assert params["@type"] == "setTdlibParameters"


@pytest.mark.asyncio
async def test_the_database_directory_is_created_and_is_per_account(fake, tmp_path):
    """Two accounts sharing a database would share secret-chat keys, which is a
    cross-account leak, not a tidiness problem."""
    a = tdlib.TDLibClient("one", database_dir=tmp_path / "one")
    b = tdlib.TDLibClient("two", database_dir=tmp_path / "two")

    a._parameters()
    b._parameters()

    assert (tmp_path / "one").is_dir()
    assert (tmp_path / "two").is_dir()
    assert a.database_dir != b.database_dir


def test_the_default_database_lives_under_the_state_directory():
    """Never beside the code: the install directory may be read-only and is a git
    checkout. Same rule the Telethon sessions already follow."""
    path = tdlib.database_dir_for("kgb_verifier")

    assert path.name == "kgb_verifier"
    assert path.parent.name == "tdlib"


@pytest.mark.asyncio
async def test_the_parameters_are_sent_the_moment_tdlib_asks_for_them(fake, tmp_path):
    """TDLib answers its first request with `WaitTdlibParameters` and then waits.
    Nothing else happens until this reply, so a missed one hangs the client."""
    client = tdlib.TDLibClient("acct", database_dir=tmp_path / "db")
    task = asyncio.ensure_future(client.start())
    await asyncio.sleep(0)

    client._handle_on_loop(
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
        }
    )
    await asyncio.sleep(0)

    assert any(obj.get("@type") == "setTdlibParameters" for _, obj in fake.sent)

    client._handle_on_loop(
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateWaitPhoneNumber"},
        }
    )
    assert await task == "authorizationStateWaitPhoneNumber"


# --------------------------------------------------------------------------
# Request correlation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_requests_in_flight_get_their_own_answers(fake, tmp_path):
    """`@extra` is the only thing tying an answer to its question. Several calls
    are in flight whenever a tool loops over chats, and a mix-up there returns one
    chat's data under another chat's name."""
    client = await _started(fake, tmp_path)

    first = asyncio.ensure_future(client.request({"@type": "getChat", "chat_id": 1}))
    second = asyncio.ensure_future(client.request({"@type": "getChat", "chat_id": 2}))
    await asyncio.sleep(0)

    extras = [obj["@extra"] for _, obj in fake.sent if obj.get("@type") == "getChat"]
    assert len(set(extras)) == 2, "two requests shared an @extra"

    # Answered out of order, which is the case that matters.
    client._handle_on_loop({"@type": "chat", "id": 2, "@extra": extras[1]})
    client._handle_on_loop({"@type": "chat", "id": 1, "@extra": extras[0]})

    assert (await first)["id"] == 1
    assert (await second)["id"] == 2


@pytest.mark.asyncio
async def test_an_error_answer_is_raised_rather_than_returned(fake, tmp_path):
    """A caller that had to inspect every result for `@type == "error"` would
    forget once, and the forgotten one would read as an empty success."""
    client = await _started(fake, tmp_path)

    pending = asyncio.ensure_future(client.request({"@type": "getChat"}))
    await asyncio.sleep(0)
    extra = fake.sent[-1][1]["@extra"]
    client._handle_on_loop(
        {"@type": "error", "code": 400, "message": "CHAT_ID_INVALID", "@extra": extra}
    )

    with pytest.raises(tdlib.TDLibError) as raised:
        await pending
    assert raised.value.code == 400
    assert "CHAT_ID_INVALID" in str(raised.value)


@pytest.mark.asyncio
async def test_a_request_that_is_never_answered_times_out_and_stops_waiting(fake, tmp_path):
    """The pending entry has to go with it. Left behind, every timed-out call
    leaks a future for the lifetime of the process."""
    client = await _started(fake, tmp_path)

    with pytest.raises(TimeoutError):
        await client.request({"@type": "getChat"}, timeout=0.01)

    assert client._pending == {}


@pytest.mark.asyncio
async def test_a_request_before_start_is_refused_clearly(fake, tmp_path):
    client = tdlib.TDLibClient("acct", database_dir=tmp_path / "db")

    with pytest.raises(RuntimeError, match="not started"):
        await client.request({"@type": "getChat"})


# --------------------------------------------------------------------------
# Updates
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_updates_are_queued_and_drained_oldest_first(fake, tmp_path):
    client = await _started(fake, tmp_path)

    client._handle_on_loop({"@type": "updateNewMessage", "n": 1})
    client._handle_on_loop({"@type": "updateNewMessage", "n": 2})

    assert [e["n"] for e in await client.drain_updates()] == [1, 2]
    assert await client.drain_updates() == [], "a drain must not replay what it returned"


@pytest.mark.asyncio
async def test_a_full_queue_drops_the_oldest_not_the_newest(fake, tmp_path, monkeypatch):
    """Which end is dropped is the whole decision. A caller polling "did my message
    arrive" wants the newest; dropping that instead would answer no forever."""
    client = await _started(fake, tmp_path)
    client.updates = asyncio.Queue(maxsize=2)

    for n in (1, 2, 3):
        client._handle_on_loop({"@type": "updateNewMessage", "n": n})

    assert [e["n"] for e in await client.drain_updates()] == [2, 3]


@pytest.mark.asyncio
async def test_an_authorization_update_never_lands_in_the_update_queue(fake, tmp_path):
    """It drives the lifecycle instead. Queued, it would both be missed by `start`
    and pollute what a caller reads as chat activity."""
    client = await _started(fake, tmp_path)

    client._handle_on_loop(
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateClosed"},
        }
    )

    assert await client.drain_updates() == []
    assert client.authorization_state == "authorizationStateClosed"


@pytest.mark.asyncio
async def test_drain_can_filter_by_type(fake, tmp_path):
    client = await _started(fake, tmp_path)
    client._handle_on_loop({"@type": "updateNewMessage", "n": 1})
    client._handle_on_loop({"@type": "updateChatTitle", "n": 2})

    kept = await client.drain_updates(of_type={"updateNewMessage"})

    assert [e["n"] for e in kept] == [1]


# --------------------------------------------------------------------------
# Routing between clients
# --------------------------------------------------------------------------


def test_an_event_goes_only_to_the_client_it_belongs_to(monkeypatch):
    """`td_receive` is process-global: one call returns the next event for ANY
    client. Routing by `@client_id` is what keeps two accounts apart, and getting
    it wrong would hand one account's secret-chat traffic to the other."""
    seen = []

    class Recorder:
        def __init__(self, tag):
            self.tag = tag

        def _handle(self, event):
            seen.append((self.tag, event))

    monkeypatch.setattr(tdlib, "_clients", {1: Recorder("one"), 2: Recorder("two")})

    tdlib._dispatch({"@client_id": 2, "@type": "updateNewMessage"})
    tdlib._dispatch({"@client_id": 99, "@type": "updateNewMessage"})  # unknown: dropped

    assert [tag for tag, _ in seen] == ["two"]


# --------------------------------------------------------------------------
# The account cache
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_without_a_login_is_refused_with_the_command_to_fix_it(
    fake, tmp_path, monkeypatch
):
    """Returning a running-but-unauthorised client would fail later, on whichever
    call happened to come first, with a message about that call instead."""
    monkeypatch.setattr(tdlib, "_by_account", {})
    monkeypatch.setattr(tdlib, "database_dir_for", lambda account: tmp_path / account)

    class Unauthorised(tdlib.TDLibClient):
        async def start(self):
            self.authorization_state = "authorizationStateWaitPhoneNumber"
            return self.authorization_state

        async def close(self):
            return None

    monkeypatch.setattr(tdlib, "TDLibClient", Unauthorised)

    with pytest.raises(tdlib.NotSignedIn) as raised:
        await tdlib.secret_client("kgb_verifier")

    assert "secret_chat_login.py kgb_verifier" in str(raised.value)
    assert tdlib._by_account == {}, "an unusable client was cached"


@pytest.mark.asyncio
async def test_a_started_client_is_reused_rather_than_started_again(fake, tmp_path, monkeypatch):
    """Starting one opens a database and reconnects. Paying that per tool call
    would also mean several TDLib clients for one account, each with its own
    view of the same secret chats."""
    monkeypatch.setattr(tdlib, "_by_account", {})
    starts = []

    class Ready(tdlib.TDLibClient):
        async def start(self):
            starts.append(self.account)
            self._client_id = 1
            self.authorization_state = "authorizationStateReady"
            return self.authorization_state

    monkeypatch.setattr(tdlib, "TDLibClient", Ready)
    monkeypatch.setattr(tdlib, "database_dir_for", lambda account: tmp_path / account)

    first = await tdlib.secret_client("acct")
    second = await tdlib.secret_client("acct")

    assert first is second
    assert starts == ["acct"]


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_a_missing_binary_is_reported_not_raised(monkeypatch):
    """`secret_chat_status` has to be able to answer on a machine that never
    installed the optional dependency - that is its whole job."""

    def _absent():
        raise tdlib.TDLibUnavailable("Install it with: pip install tdjson")

    monkeypatch.setattr(tdlib, "_tdjson", _absent)

    status = tdlib.tdjson_status()

    assert status["available"] is False
    assert "pip install tdjson" in status["reason"]


def test_a_present_binary_reports_its_version(fake):
    status = tdlib.tdjson_status()

    assert status == {"available": True, "tdlib_version": "1.8.67"}


def test_the_log_verbosity_is_lowered_before_anything_is_sent(fake, tmp_path):
    """At its default level TDLib prints every request and response, which for a
    secret chat means printing the plaintext to stderr."""
    tdlib._quieten(fake)

    (request,) = [r for r in fake.executed if r["@type"] == "setLogVerbosityLevel"]
    assert request["new_verbosity_level"] <= 1


# --------------------------------------------------------------------------
# Authorising from the Telethon login that already exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link,expected",
    [
        # Telegram encodes the token base64url and strips the padding, which is
        # exactly what `b64decode` refuses. Each of these needs a different
        # number of '=' put back.
        ("tg://login?token=AQIDBAUGBwgJCgsMDQ4PEA", bytes(range(1, 17))),
        ("tg://login?token=AQID", b"\x01\x02\x03"),
        ("tg://login?token=AQI", b"\x01\x02"),
    ],
)
def test_a_login_token_survives_telegrams_unpadded_base64(link, expected):
    """A token decoded wrong is not rejected by Telegram as malformed - it is
    rejected as the wrong token, which reads like the whole approach failing."""
    assert tdlib.login_token(link) == expected


def test_a_link_with_no_token_is_refused_by_name():
    with pytest.raises(ValueError, match="No login token"):
        tdlib.login_token("tg://login")


@pytest.mark.asyncio
async def test_the_existing_telethon_login_authorises_tdlib_with_no_code(fake, tmp_path):
    """The whole point: TDLib publishes a login token and the account's already
    authorised Telethon client accepts it, so nobody is asked for anything.

    Pinned end to end because each half is useless alone - a token requested and
    never accepted leaves TDLib waiting forever, and an accept without a request
    has nothing to accept.
    """
    client = tdlib.TDLibClient("acct", database_dir=tmp_path / "db")
    task = asyncio.ensure_future(client.start())
    await asyncio.sleep(0)
    client._handle_on_loop(
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateWaitPhoneNumber"},
        }
    )
    assert await task == "authorizationStateWaitPhoneNumber"

    accepted = []

    class FakeTelethon:
        async def __call__(self, request):
            accepted.append(bytes(request.token))
            # Telegram pushes the acceptance back as a new authorisation state.
            client._handle_on_loop(
                {
                    "@type": "updateAuthorizationState",
                    "authorization_state": {"@type": "authorizationStateReady"},
                }
            )
            return object()

    async def _drive():
        # The client answers the QR request with the link, the way TDLib does.
        await asyncio.sleep(0)
        client._handle_on_loop(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {
                    "@type": "authorizationStateWaitOtherDeviceConfirmation",
                    "link": "tg://login?token=AQIDBAUGBwgJCgsMDQ4PEA",
                },
            }
        )
        extra = next(
            obj["@extra"]
            for _, obj in fake.sent
            if obj.get("@type") == "requestQrCodeAuthentication"
        )
        client._handle_on_loop({"@type": "ok", "@extra": extra})

    driver = asyncio.ensure_future(_drive())
    state = await tdlib.authorise_from_telethon(client, FakeTelethon())
    await driver

    assert state == "authorizationStateReady"
    assert accepted == [bytes(range(1, 17))], "the token Telethon accepted was not TDLib's"


@pytest.mark.asyncio
async def test_authorising_a_client_that_is_not_waiting_for_a_login_is_refused(fake, tmp_path):
    """Called on a signed-in client it would publish a login token for an account
    that already has one, which is a device nobody asked for."""
    client = await _started(fake, tmp_path)

    with pytest.raises(RuntimeError, match="not waiting for a login"):
        await tdlib.authorise_from_telethon(client, object())
