"""Every "how many" argument, checked against the live registry rather than a list.

The defect these cover was not one tool: it was that each tool decided for itself.
Some clamped, some clamped only the floor, and the rest handed whatever arrived
straight to `iter_messages` and then to `json.dumps`. A hand-written list of the
offenders would have gone stale on the next tool added, so the coverage test walks
the registered tools and fails on one that takes a count without declaring a
ceiling.

Nothing here needs a client: a count is checked from the arguments alone, so an
invalid one must come back as a refusal before any connection is attempted. These
tests deliberately wire no client at all -- if validation ever moves below
`get_client`, they stop returning a refusal and start failing.
"""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

import telegram_mcp.tools as tools_package
from telegram_mcp.paging import LIMITS, MAX_OFFSET, bounded, bounded_page, bounded_slice

# Parameters that are a count of records fetched or serialized. `offset`,
# `max_id`, `offset_id` and `offset_topic` are cursors Telegram issues, not
# counts, and are excluded on purpose: they are opaque positions, and clamping
# one would corrupt paging rather than bound it.
COUNT_PARAMS = ("limit", "page_size", "context_size")

# Accepted and ignored, so there is nothing to bound. `list_inline_buttons.limit`
# only ever fed the recent-message scan that was removed; the parameter is kept so
# saved prompts naming it still call. Validating a value that reaches nothing
# would be theatre.
IGNORED_COUNTS = {("list_inline_buttons", "limit")}


@pytest.fixture(scope="module")
def registered_tools():
    from telegram_mcp.runtime import mcp

    return asyncio.run(mcp.list_tools())


def _counts(tool):
    properties = (tool.input_schema or {}).get("properties", {}) or {}
    return [
        name
        for name in properties
        if name in COUNT_PARAMS and (tool.name, name) not in IGNORED_COUNTS
    ]


@pytest.fixture(scope="module")
def counting_tools(registered_tools):
    """Every registered tool that takes a count, from the registry itself."""
    found = [(tool, _counts(tool)) for tool in registered_tools]
    return [(tool, names) for tool, names in found if names]


def test_the_registry_actually_has_counting_tools(counting_tools):
    """Guard against a discovery bug turning every test below into a no-op."""
    assert len(counting_tools) >= 15, [t.name for t, _ in counting_tools]


def test_every_tool_that_takes_a_count_declares_a_ceiling(counting_tools):
    """A new list tool must not be able to ship without a number. This is the
    whole reason the ceilings live in one table instead of in each function."""
    undeclared = sorted(tool.name for tool, _names in counting_tools if tool.name not in LIMITS)

    assert undeclared == [], f"these tools take a count with no declared ceiling: {undeclared}"


def test_no_ceiling_is_declared_for_a_tool_that_no_longer_takes_one(counting_tools):
    """The other direction: a stale entry is a ceiling nothing enforces."""
    live = {tool.name for tool, _names in counting_tools}

    assert sorted(set(LIMITS) - live) == []


def test_every_ceiling_is_written_down_where_the_caller_will_read_it(counting_tools):
    """The description is what a model reads when it picks a number. A ceiling
    only the refusal knows about is one every caller discovers by tripping over
    it, and a docstring that names a different number is worse than none."""
    silent = [
        tool.name
        for tool, _names in counting_tools
        if str(LIMITS[tool.name]) not in (tool.description or "")
    ]

    assert silent == [], f"these tools never mention their own ceiling: {silent}"


def test_every_ceiling_is_a_usable_number():
    for name, ceiling in LIMITS.items():
        assert isinstance(ceiling, int) and not isinstance(ceiling, bool), name
        assert 1 <= ceiling <= 1000, f"{name}={ceiling} is not a ceiling anyone chose"


# --- the tools themselves, invoked with nonsense ----------------------------


def _dummy(name, schema):
    """A value the tool will accept for a required argument that is not the count."""
    if name in ("chat_id", "user_id", "peer_id", "message_id", "id"):
        return 1
    declared = schema.get("type")
    if declared is None and "anyOf" in schema:
        declared = (schema["anyOf"] or [{}])[0].get("type")
    return {
        "integer": 1,
        "number": 1,
        "boolean": False,
        "array": [],
        "object": {},
    }.get(declared, "probe")


def _call_kwargs(tool):
    schema = tool.input_schema or {}
    properties = schema.get("properties", {}) or {}
    kwargs = {
        name: _dummy(name, properties.get(name, {})) for name in schema.get("required", []) or []
    }
    # An explicit account keeps `with_account` from fanning the call out; nothing
    # here should reach a client either way, and this makes that unambiguous.
    if "account" in properties:
        kwargs["account"] = "probe"
    # The waits block for their whole budget by design. A test is not the place
    # to spend it, and the count is checked long before the wait begins.
    for name, brief in (("timeout", 0.05), ("max_wait_ms", 50)):
        if name in properties:
            kwargs[name] = brief
    return kwargs


def pytest_generate_tests(metafunc):
    """Parameterize from the registry, which a decorator cannot do: the fixture
    that reads it is only available once the session is running."""
    if "tool_name" not in metafunc.fixturenames:
        return
    from telegram_mcp.runtime import mcp

    tools = asyncio.run(mcp.list_tools())
    cases = [
        pytest.param(tool.name, name, id=f"{tool.name}-{name}")
        for tool in tools
        for name in _counts(tool)
    ]
    metafunc.parametrize(("tool_name", "count_param"), cases)


async def _invoke(tool_name, count_param, value, registered_tools):
    tool = next(t for t in registered_tools if t.name == tool_name)
    function = getattr(tools_package, tool_name)
    kwargs = _call_kwargs(tool)
    kwargs[count_param] = value
    return await function(**kwargs)


def _refusal(result):
    """The refusal text, whichever shape the tool returns.

    A tool that returns images is annotated `-> list` so the server builds no output
    schema for it, and it reports a refusal as a one-element list. That is the
    documented shape, not an inconsistency - but this file only ever met
    str-returning tools until an image tool acquired a limit, so the assertion
    below unwraps it rather than declaring the contract broken.
    """
    if isinstance(result, list):
        assert len(result) == 1, f"a refusal must not carry images: {result!r}"
        assert isinstance(result[0], str), result
        return result[0]
    assert isinstance(result, str), result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, -(10**9)])
async def test_a_non_positive_count_is_refused_before_any_client_work(
    tool_name, count_param, value, registered_tools
):
    """Telethon reads `limit<=0` as "no limit" rather than "none", so a zero that
    looks like an empty request is an unbounded one. Refused from the arguments
    alone, which is why this passes with no client wired at all."""
    result = await _invoke(tool_name, count_param, value, registered_tools)

    message = _refusal(result)
    assert count_param in message, message
    assert "Error" in message, message


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["many", 2.5, float("nan"), float("inf"), None])
async def test_a_count_that_is_not_a_whole_number_is_refused(
    tool_name, count_param, value, registered_tools
):
    """`nan` is the one that matters: every comparison against it is False, so a
    ceiling tested with `>` silently does not fire."""
    result = await _invoke(tool_name, count_param, value, registered_tools)

    message = _refusal(result)
    assert count_param in message, message
    assert "Error" in message, message


@pytest.mark.asyncio
async def test_a_huge_count_is_never_passed_through_as_given(
    tool_name, count_param, registered_tools
):
    """The clamp has to happen before the request. Proven here by refusing to give
    the tool a client: if it survives to the network it fails, and if it clamps
    first it still has to reach the network to do anything -- so the assertion is
    that whatever comes back, the number asked for is not what was used."""
    ceiling = LIMITS[tool_name]
    result = await _invoke(tool_name, count_param, 10**9, registered_tools)

    message = _refusal(result)
    assert "1000000000" not in message, f"the raw count reached the answer: {message[:200]}"
    assert ceiling >= 1


# --- the shared rule, on its own --------------------------------------------


def test_a_count_inside_the_ceiling_is_used_as_given():
    bound = bounded(30, 200)

    assert (bound.value, bound.error, bound.clamped) == (30, None, False)
    assert bound.metadata == {"requested_limit": 30, "effective_limit": 30}


def test_a_count_above_the_ceiling_is_clamped_and_says_so():
    bound = bounded(5000, 200)

    assert bound.value == 200
    assert bound.error is None
    assert bound.clamped is True
    assert bound.metadata["requested_limit"] == 5000
    assert bound.metadata["effective_limit"] == 200
    assert "5000" in bound.metadata["limit_note"]


def test_the_count_at_the_ceiling_is_not_treated_as_over_it():
    assert bounded(200, 200).clamped is False


@pytest.mark.parametrize("value", [True, False])
def test_a_bool_is_not_a_count(value):
    """`True` is an `int` in Python; `limit=True` meaning 1 is a coincidence."""
    assert bounded(value, 200).error is not None


@pytest.mark.parametrize("value", ["20", 20.0])
def test_a_whole_number_in_another_shape_is_still_a_whole_number(value):
    assert bounded(value, 200).value == 20


def test_a_ceiling_that_is_not_a_ceiling_is_a_programming_error():
    """Not a caller's mistake, so not a caller-facing refusal."""
    for bad in (0, -1, 2.5, True, "many"):
        with pytest.raises(ValueError):
            bounded(10, bad)


# --- page arithmetic ---------------------------------------------------------


def test_a_page_number_becomes_an_offset():
    bound, offset = bounded_page(3, 20, 200)

    assert (bound.value, offset) == (20, 40)


def test_the_first_page_starts_at_zero():
    _bound, offset = bounded_page(1, 20, 200)

    assert offset == 0


@pytest.mark.parametrize("page", [0, -1, "many", 2.5, None])
def test_a_page_that_is_not_a_page_is_refused(page):
    bound, offset = bounded_page(page, 20, 200)

    assert offset is None
    assert "page" in bound.error


def test_a_page_number_past_the_paging_limit_is_refused_rather_than_computed():
    """`(page - 1) * page_size` does not overflow in Python; it just produces a
    number of records Telegram is asked to skip. The bound has to be explicit."""
    bound, offset = bounded_page(10**18, 20, 200)

    assert offset is None
    assert str(MAX_OFFSET) in bound.error


def test_the_last_reachable_page_is_still_reachable():
    page = MAX_OFFSET // 20 + 1
    bound, offset = bounded_page(page, 20, 200)

    assert bound.error is None
    assert offset == MAX_OFFSET


def test_an_oversized_page_size_is_clamped_before_the_offset_is_computed():
    """Otherwise the clamp moves the page under the caller: page 3 of 5000 is not
    page 3 of 200."""
    bound, offset = bounded_page(3, 5000, 200)

    assert bound.value == 200
    assert offset == 400


def test_a_short_page_says_there_is_no_more():
    from telegram_mcp.paging import page_metadata

    described = page_metadata(bounded(20, 200), page=2, offset=20, returned=7)

    assert described["has_more"] is False
    assert (described["page"], described["offset"], described["returned"]) == (2, 20, 7)


def test_a_full_page_says_there_may_be_more():
    from telegram_mcp.paging import page_metadata

    assert page_metadata(bounded(20, 200), page=1, offset=0, returned=20)["has_more"] is True


def test_a_bounded_slice_reports_what_it_left_behind():
    served, described = bounded_slice(list(range(50)), bounded(10, 200))

    assert served == list(range(10))
    assert described["total"] == 50
    assert described["returned"] == 10
    assert described["has_more"] is True


def test_a_bounded_slice_that_fits_says_so():
    _served, described = bounded_slice([1, 2], bounded(10, 200))

    assert described["has_more"] is False


# --- the clamp has to reach the request -------------------------------------


class _RecordingClient:
    """Records the limit each call was made with."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.get_messages_kwargs = []

    async def get_messages(self, entity, **kwargs):
        self.get_messages_kwargs.append(kwargs)
        return self.messages

    async def get_dialogs(self, **kwargs):
        self.get_dialogs_kwargs = kwargs
        return []


@pytest.fixture
def _wire_messages(monkeypatch):
    from telegram_mcp.tools import messages_read

    client = _RecordingClient()
    monkeypatch.setattr(messages_read, "get_client", lambda account=None: client)

    async def _resolve(chat_id, cl=None, account=None):
        return SimpleNamespace(id=chat_id)

    monkeypatch.setattr(messages_read, "resolve_entity", _resolve)
    return client


@pytest.mark.asyncio
async def test_get_history_asks_telegram_for_the_clamped_number(_wire_messages):
    from telegram_mcp.tools.messages_read import get_history

    await get_history(1, limit=10**6, account="probe")

    assert _wire_messages.get_messages_kwargs[0]["limit"] == LIMITS["get_history"]


@pytest.mark.asyncio
async def test_get_history_reports_what_was_asked_for_beside_what_was_served(_wire_messages):
    from telegram_mcp.tools.messages_read import get_history

    _wire_messages.messages = []
    payload = json.loads(await get_history(1, limit=10**6, account="probe"))

    assert payload["requested_limit"] == 10**6
    assert payload["effective_limit"] == LIMITS["get_history"]
    assert "limit_note" in payload


@pytest.mark.asyncio
async def test_search_messages_asks_telegram_for_the_clamped_number(_wire_messages):
    from telegram_mcp.tools.messages_read import search_messages

    await search_messages(1, "q", limit=99999, account="probe")

    assert _wire_messages.get_messages_kwargs[0]["limit"] == LIMITS["search_messages"]


def test_the_shared_rule_is_the_only_one_each_tool_applies():
    """A tool that still clamps by hand is a second rule that will drift from
    this one -- which is exactly how the limits ended up inconsistent."""
    import telegram_mcp.tools.chats
    import telegram_mcp.tools.inspection
    import telegram_mcp.tools.messages_read
    import telegram_mcp.tools.polls
    import telegram_mcp.tools.saved

    modules = (
        telegram_mcp.tools.chats,
        telegram_mcp.tools.inspection,
        telegram_mcp.tools.messages_read,
        telegram_mcp.tools.polls,
        telegram_mcp.tools.saved,
    )
    handrolled = []
    for module in modules:
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if "min(int(limit)" in stripped or "max(1, int(page" in stripped:
                handrolled.append(f"{module.__name__}: {stripped}")

    assert handrolled == [], handrolled
