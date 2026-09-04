"""Channel username, statistics and recommendations.

No network: the client is a fake that records the TL requests it was handed, so
the assertions are about which requests the tools build and how honestly they
describe what comes back. The two things worth guarding hardest are that a
username change is not attempted when the name is taken, and that an
unresolved graph token is never reported as if it were graph data.
"""

import json
from types import SimpleNamespace

import pytest
from telethon import errors, types

from telegram_mcp.tools import channel_admin as mod
from telegram_mcp.tools.channel_admin import (
    _normalize_username,
    _username_rule_broken,
    check_channel_username,
    get_channel_statistics,
    get_similar_channels,
    set_channel_username,
)


class _Client:
    """Records every TL request and answers the ones these tools send."""

    def __init__(self, username_free=True, stats=None, graphs=None, result=None, fails=None):
        self.requests = []
        self.username_free = username_free
        self.stats = stats
        self.graphs = graphs or {}
        self.result = result
        self.fails = fails or {}

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in self.fails:
            raise self.fails[name]
        if name == "CheckUsernameRequest":
            return self.username_free
        if name == "UpdateUsernameRequest":
            return True
        if name.endswith("StatsRequest"):
            return self.stats
        if name == "LoadAsyncGraphRequest":
            answer = self.graphs[request.token]
            if isinstance(answer, Exception):
                raise answer
            return answer
        if name == "GetChannelRecommendationsRequest":
            return self.result
        raise AssertionError(f"unexpected request {name}")

    @property
    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _channel(title="Ops Room", username="ops_room", broadcast=True, megagroup=False):
    return SimpleNamespace(
        id=99, title=title, username=username, broadcast=broadcast, megagroup=megagroup
    )


def _graph_json(names=("Followers",), points=3):
    columns = [["x", *[1700000000000 + i * 86400000 for i in range(points)]]]
    for index, _name in enumerate(names):
        columns.append([f"y{index}", *[10 + i for i in range(points)]])
    return types.StatsGraph(
        json=types.DataJSON(
            data=json.dumps(
                {
                    "columns": columns,
                    "names": {f"y{i}": n for i, n in enumerate(names)},
                }
            )
        ),
        zoom_token=None,
    )


@pytest.fixture
def _wire(monkeypatch):
    def wire(client, entity=None):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return entity if entity is not None else _channel()

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


# --- the username rules stated locally --------------------------------------


@pytest.mark.parametrize(
    ("username", "rule"),
    [
        ("abc", "5-32 characters"),
        ("a" * 33, "5-32 characters"),
        ("ops room", "letters, digits and underscores"),
        ("ops-room", "letters, digits and underscores"),
        ("ops.room", "letters, digits and underscores"),
        ("1ops_room", "cannot start with a digit"),
    ],
)
def test_a_broken_rule_is_named_rather_than_left_to_the_server(username, rule):
    broken = _username_rule_broken(username)
    assert broken and rule in broken


@pytest.mark.parametrize("username", ["ops_room", "Ops_Room9", "a" * 32, "_hidden"])
def test_a_well_formed_name_is_passed_through_to_telegram(username):
    """Everything beyond form — reserved words, Fragment names — is the server's call."""
    assert _username_rule_broken(username) is None


@pytest.mark.parametrize(
    "raw", ["ops_room", "@ops_room", " @ops_room ", "t.me/ops_room", "https://t.me/ops_room"]
)
def test_the_at_sign_and_link_prefixes_are_stripped(raw):
    assert _normalize_username(raw) == "ops_room"


# --- checking a username ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_free_username_reports_the_link_it_would_get(_wire):
    client = _wire(_Client(username_free=True))

    payload = json.loads(await check_channel_username(1, "new_room", account="a"))

    assert client.sent("CheckUsernameRequest").username == "new_room"
    assert payload["results"][0]["available"] is True
    assert payload["results"][0]["public_link"] == "https://t.me/new_room"


@pytest.mark.asyncio
async def test_a_broken_rule_costs_no_request_at_all(_wire):
    client = _wire(_Client())

    result = await check_channel_username(1, "no", account="a")

    assert "5-32 characters" in result
    assert client.requests == [], "a request Telegram would reject was sent anyway"


# --- setting a username -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_taken_username_is_reported_without_attempting_the_change(_wire):
    client = _wire(_Client(username_free=False))

    result = await set_channel_username(1, "taken_name", account="a")

    assert "already taken" in result
    assert "UpdateUsernameRequest" not in client.names, "the doomed change was sent anyway"
    assert "still ops_room" in result


@pytest.mark.asyncio
async def test_setting_a_free_username_checks_first_then_changes_it(_wire):
    client = _wire(_Client(username_free=True))

    payload = json.loads(await set_channel_username(1, "@new_room", account="a"))

    assert client.names == ["CheckUsernameRequest", "UpdateUsernameRequest"]
    assert client.sent("UpdateUsernameRequest").username == "new_room"
    record = payload["results"][0]
    assert record["username"] == "new_room"
    assert record["previous_username"] == "ops_room"
    assert record["public_link"] == "https://t.me/new_room"
    assert record["now_private"] is False


@pytest.mark.asyncio
async def test_clearing_the_username_is_flagged_as_making_the_channel_private(_wire):
    client = _wire(_Client())

    payload = json.loads(await set_channel_username(1, "", account="a"))

    assert client.names == ["UpdateUsernameRequest"], "an empty name has nothing to check"
    assert client.sent("UpdateUsernameRequest").username == ""
    record = payload["results"][0]
    assert record["now_private"] is True
    assert record["username"] is None
    assert "PRIVATE" in record["effect"]
    assert "invite link" in record["effect"]
    assert "free for anyone else to claim" in record["effect"]


@pytest.mark.asyncio
async def test_removal_has_to_be_asked_for_rather_than_arrived_at_by_omission(_wire):
    """An absent argument must not be the thing that makes a channel private."""
    client = _wire(_Client())

    result = await set_channel_username(1, None, account="a")

    assert "empty string" in result
    assert client.requests == []


@pytest.mark.asyncio
async def test_a_username_taken_between_check_and_change_says_nothing_changed(_wire):
    _wire(_Client(fails={"UpdateUsernameRequest": errors.UsernameOccupiedError(request=None)}))

    result = await set_channel_username(1, "new_room", account="a")

    assert "nothing changed" in result


# --- statistics -------------------------------------------------------------


@pytest.mark.asyncio
async def test_scalar_counters_report_current_previous_and_the_move_between(_wire):
    stats = SimpleNamespace(
        period=types.StatsDateRangeDays(min_date=1700000000, max_date=1700086400),
        followers=types.StatsAbsValueAndPrev(current=1200.0, previous=1000.0),
        views_per_post=types.StatsAbsValueAndPrev(current=4.5, previous=6.0),
        enabled_notifications=types.StatsPercentValue(part=25.0, total=100.0),
    )
    _wire(_Client(stats=stats))

    payload = json.loads(await get_channel_statistics(1, account="a"))
    described = payload["results"][0]

    assert described["followers"] == {"current": 1200.0, "previous": 1000.0, "delta": 200.0}
    assert described["views_per_post"]["delta"] == -1.5, "a fall must read as a fall"
    assert described["enabled_notifications"]["percent"] == 25.0


@pytest.mark.asyncio
async def test_an_unresolved_graph_token_is_never_presented_as_data(_wire):
    stats = SimpleNamespace(growth_graph=types.StatsGraphAsync(token="secret-token"))
    _wire(_Client(stats=stats))

    raw = await get_channel_statistics(1, resolve_graphs=False, account="a")
    payload = json.loads(raw)

    graph = payload["results"][0]["growth_graph"]
    assert graph["status"] == "not_loaded"
    assert "secret-token" not in raw, "the token was handed back as if it were the graph"
    assert payload["graphs_loaded"] == 0
    assert payload["graphs_unresolved"] == 1


@pytest.mark.asyncio
async def test_a_graph_token_that_can_be_resolved_is_resolved(_wire):
    stats = SimpleNamespace(growth_graph=types.StatsGraphAsync(token="tok"))
    client = _wire(_Client(stats=stats, graphs={"tok": _graph_json(("Followers",), points=3)}))

    payload = json.loads(await get_channel_statistics(1, account="a"))

    assert client.sent("LoadAsyncGraphRequest").token == "tok"
    graph = payload["results"][0]["growth_graph"]
    assert graph["status"] == "loaded"
    assert graph["series"] == ["Followers"]
    assert graph["points"] == 3
    assert "columns" not in graph, "raw columns are opt-in"
    assert payload["graphs_loaded"] == 1


@pytest.mark.asyncio
async def test_a_failed_second_call_says_so_instead_of_inventing_an_empty_graph(_wire):
    stats = SimpleNamespace(growth_graph=types.StatsGraphAsync(token="secret-token"))
    _wire(_Client(stats=stats, graphs={"secret-token": RuntimeError("GRAPH_EXPIRED_RELOAD")}))

    raw = await get_channel_statistics(1, account="a")
    graph = json.loads(raw)["results"][0]["growth_graph"]

    assert graph["status"] == "not_loaded"
    assert "loadAsyncGraph" in graph["note"]
    assert "GRAPH_EXPIRED_RELOAD" in graph["note"], "Telegram's own reason is worth keeping"
    assert "secret-token" not in raw, "a token that failed to load is still not data"


@pytest.mark.asyncio
async def test_a_stats_graph_error_is_surfaced_with_telegram_s_own_reason(_wire):
    stats = SimpleNamespace(mute_graph=types.StatsGraphError(error="GRAPH_OUTDATED_RELOAD"))
    _wire(_Client(stats=stats))

    payload = json.loads(await get_channel_statistics(1, account="a"))

    assert payload["results"][0]["mute_graph"] == {
        "status": "error",
        "error": "GRAPH_OUTDATED_RELOAD",
    }
    assert payload["graphs_unresolved"] == 1


@pytest.mark.asyncio
async def test_raw_columns_are_included_when_asked_for(_wire):
    stats = SimpleNamespace(growth_graph=_graph_json(("Followers",), points=2))
    _wire(_Client(stats=stats))

    payload = json.loads(await get_channel_statistics(1, include_graph_data=True, account="a"))

    assert payload["results"][0]["growth_graph"]["columns"][1] == ["y0", 10, 11]


@pytest.mark.asyncio
async def test_telegram_s_refusal_is_explained_rather_than_leaked(_wire):
    """Telethon documents this error as the too-few-members case as well as the
    not-an-admin one, so the refusal has to name both."""
    _wire(_Client(fails={"GetBroadcastStatsRequest": errors.ChatAdminRequiredError(request=None)}))

    result = await get_channel_statistics(1, account="a")

    assert "admin" in result and "500" in result
    assert "cannot be told apart" in result
    assert "ChatAdminRequiredError" not in result


@pytest.mark.asyncio
async def test_a_supergroup_gets_the_megagroup_request_not_the_broadcast_one(_wire):
    client = _wire(
        _Client(stats=SimpleNamespace(members=types.StatsAbsValueAndPrev(current=5, previous=4))),
        entity=_channel(broadcast=False, megagroup=True),
    )

    payload = json.loads(await get_channel_statistics(1, account="a"))

    assert client.names == ["GetMegagroupStatsRequest"]
    assert payload["scope"] == "supergroup"


@pytest.mark.asyncio
async def test_a_post_id_asks_for_that_post_s_statistics(_wire):
    client = _wire(_Client(stats=SimpleNamespace(views_graph=_graph_json(("Views",)))))

    payload = json.loads(await get_channel_statistics(1, message_id=42, account="a"))

    assert client.sent("GetMessageStatsRequest").msg_id == 42
    assert payload["scope"] == "message 42"


@pytest.mark.asyncio
async def test_a_post_id_on_a_supergroup_is_refused_before_any_request(_wire):
    client = _wire(_Client(), entity=_channel(broadcast=False, megagroup=True))

    result = await get_channel_statistics(1, message_id=42, account="a")

    assert "broadcast channel posts" in result
    assert client.requests == []


@pytest.mark.asyncio
async def test_a_user_has_no_statistics_and_is_told_so_without_a_request(_wire):
    client = _wire(_Client(), entity=types.User(id=5, first_name="Ann", access_hash=0))

    result = await get_channel_statistics(1, account="a")

    assert "no statistics API" in result
    assert client.requests == []


# --- similar channels -------------------------------------------------------


@pytest.mark.asyncio
async def test_similar_channels_are_listed_with_their_links(_wire):
    chats = SimpleNamespace(
        count=40,
        chats=[
            SimpleNamespace(
                id=7,
                title="Ops Weekly",
                username="ops_weekly",
                broadcast=True,
                megagroup=False,
                participants_count=1200,
                verified=True,
            )
        ],
    )
    _wire(_Client(result=chats))

    payload = json.loads(await get_similar_channels(1, account="a"))

    record = payload["results"][0]
    assert record["title"] == "Ops Weekly"
    assert record["public_link"] == "https://t.me/ops_weekly"
    assert payload["returned"] == 1 and payload["available"] == 40
    assert "Premium" in payload["truncated"]


@pytest.mark.asyncio
async def test_no_recommendations_says_so_rather_than_returning_an_empty_list(_wire):
    _wire(_Client(result=SimpleNamespace(chats=[])))

    assert "no similar-channel recommendations" in await get_similar_channels(1, account="a")


@pytest.mark.asyncio
async def test_checking_a_username_on_a_user_says_why_instead_of_erroring(_wire):
    """Found live: aimed at a user, this answered `An error occurred (code: GEN-ERR-936)`.

    Telethon casts the peer inside `resolve()`, so an `InputPeerUser` raises
    `TypeError: Cannot cast ... to any kind of InputChannel` from deep in the library
    and the generic handler turns it into a code. The two sibling tools
    (`get_channel_statistics`, `get_similar_channels`) already explained this; these two
    did not, which is the whole defect.
    """
    client = _wire(
        _Client(), entity=SimpleNamespace(id=5899781975, broadcast=False, megagroup=False)
    )

    result = await check_channel_username(5899781975, "some_free_name", account="a")

    assert "CheckUsername" in result and "channels and supergroups" in result
    assert "GEN-ERR" not in result
    assert client.requests == [], "a request was sent for a peer that cannot take one"


@pytest.mark.asyncio
async def test_setting_a_username_on_a_user_refuses_the_same_way(_wire):
    client = _wire(
        _Client(), entity=SimpleNamespace(id=5899781975, broadcast=False, megagroup=False)
    )

    result = await set_channel_username(5899781975, "some_free_name", account="a")

    assert "channels and supergroups" in result
    assert client.requests == []


# --- linking a discussion group (the "community" shape) ---------------------
#
# The link runs channel -> group and both sides have a structural requirement
# Telegram enforces with unrelated error codes. Each refusal here is a sentence
# instead, because the two ways to get it wrong look identical from a chat id.


class _Recorder:
    """Records the link requests. The suite's own _Client refuses anything it
    does not know, which is right for it and wrong here."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(updates=[])

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


def _supergroup(title="Ops Chat"):
    return SimpleNamespace(id=1234, title=title, username=None, broadcast=False, megagroup=True)


class _Peers:
    """Resolves the channel and the group to different objects by id."""

    def __init__(self, channel, group):
        self.channel, self.group = channel, group

    async def __call__(self, chat_id, _client):
        return self.group if int(chat_id) == 1234 else self.channel


@pytest.mark.asyncio
async def test_linking_sends_both_sides_the_right_way_round(monkeypatch):
    client = _Recorder()
    monkeypatch.setattr(mod, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _Peers(_channel(), _supergroup()))

    answer = await mod.set_discussion_group(channel_id=99, group_id=1234, account="a")

    sent = client.sent("SetDiscussionGroupRequest")
    assert sent.broadcast.id == 99, "the broadcast side is not the channel"
    assert sent.group.id == 1234
    assert "Ops Chat" in answer


@pytest.mark.asyncio
async def test_omitting_the_group_detaches_rather_than_doing_nothing(monkeypatch):
    """Telegram unlinks with InputChannelEmpty, not by omitting the field. A tool
    that sent nothing would report success and change nothing."""
    client = _Recorder()
    monkeypatch.setattr(mod, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return _channel()

    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _resolve)

    answer = await mod.set_discussion_group(channel_id=99, account="a")

    sent = client.sent("SetDiscussionGroupRequest")
    assert type(sent.group).__name__ == "InputChannelEmpty", "nothing was detached"
    assert json.loads(answer)["results"][0]["detached"] is True


@pytest.mark.asyncio
async def test_passing_the_supergroup_as_the_channel_is_refused_by_name(monkeypatch):
    """The likeliest mistake: the two arguments are both chat ids and swapping
    them fails on the wire with something that does not mention the order."""
    client = _Recorder()
    monkeypatch.setattr(mod, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    async def _resolve(chat_id, _client):
        return _supergroup()

    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _resolve)

    answer = await mod.set_discussion_group(channel_id=1234, group_id=99, account="a")

    assert "channel -> group" in answer
    assert client.requests == [], "a reversed link went out anyway"


@pytest.mark.asyncio
async def test_a_basic_group_is_refused_with_the_step_that_fixes_it(monkeypatch):
    """Telegram links SUPERGROUPS only. A client converts silently first; this
    call does not, so the conversion is named rather than left as an error."""
    client = _Recorder()
    monkeypatch.setattr(mod, "get_client", lambda account=None: client)

    async def _ensure(_client):
        return None

    basic = SimpleNamespace(id=1234, title="Old Group")

    async def _resolve(chat_id, _client):
        return basic if int(chat_id) == 1234 else _channel()

    monkeypatch.setattr(mod, "ensure_connected", _ensure)
    monkeypatch.setattr(mod, "resolve_entity", _resolve)

    answer = await mod.set_discussion_group(channel_id=99, group_id=1234, account="a")

    assert "Convert it to a supergroup" in answer
    assert client.requests == []
