"""The read tools that were registered, contract-checked, and never actually run.

Audit 16 listed fifty tool names that appeared in no test file. Thirty-one have
since gained tests as a side effect of other work; these are what was left.

`tests/test_tool_registry.py` already proves every tool has a unique name, a
schema, honest annotations and a matching router — but a contract test asserts
that a tool is *declared* correctly, never that calling it does anything. Two
different failures hide in that gap: a tool that sends the wrong request, and a
tool that raises on a shape Telegram legitimately returns. Every test here
asserts on the REQUEST that went out or on the handling of an answer, not on the
returned sentence, which is identical whether the call was right or wrong.

Empty results get their own cases throughout. "No contacts" and "the call
failed" arrive at these tools looking similar, and a reader that treats an empty
list as an error is the more likely mistake.
"""

import json

import pytest
from telethon.tl import functions, types

from telegram_mcp.tools import accounts as accounts_mod
from telegram_mcp.tools import contacts as contacts_mod
from telegram_mcp.tools import folders as folders_mod
from telegram_mcp.tools import profile as profile_mod


class Recorder:
    """Records the raw requests a tool sends, and answers with a queued reply."""

    def __init__(self, *answers):
        self.sent = []
        self.answers = list(answers)

    async def __call__(self, request):
        self.sent.append(request)
        return self.answers.pop(0) if self.answers else None


def _wire(monkeypatch, module, client):
    async def _connected(cl=None):
        return None

    monkeypatch.setattr(module, "get_client", lambda account=None: client)
    if hasattr(module, "ensure_connected"):
        monkeypatch.setattr(module, "ensure_connected", _connected)
    return client


def _last(client, request_type):
    matches = [r for r in client.sent if isinstance(r, request_type)]
    assert matches, (
        f"no {request_type.__name__} was sent; got " f"{[type(r).__name__ for r in client.sent]}"
    )
    return matches[-1]


def _user(user_id, **kwargs):
    return types.User(id=user_id, **kwargs)


# --- contacts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contact_ids_asks_with_a_zero_hash(monkeypatch):
    """`hash=0` means "I have nothing cached, send everything". Telegram answers a
    non-zero hash it does not recognise with an empty diff, which would read here
    as an account with no contacts."""
    client = _wire(monkeypatch, contacts_mod, Recorder([11, 22, 33]))

    result = await contacts_mod.get_contact_ids()

    assert _last(client, functions.contacts.GetContactIDsRequest).hash == 0
    assert "11, 22, 33" in result


@pytest.mark.asyncio
async def test_get_contact_ids_says_none_rather_than_an_empty_list(monkeypatch):
    _wire(monkeypatch, contacts_mod, Recorder([]))

    assert "No contact IDs" in await contacts_mod.get_contact_ids()


@pytest.mark.asyncio
async def test_get_blocked_users_reads_the_users_not_the_peers(monkeypatch):
    """`contacts.getBlocked` answers with both `blocked` peer stubs and full
    `users`. Formatting the stubs would lose every name."""
    answer = types.contacts.Blocked(
        blocked=[], chats=[], users=[_user(7, first_name="Blocked", last_name="Person")]
    )
    client = _wire(monkeypatch, contacts_mod, Recorder(answer))

    result = await contacts_mod.get_blocked_users()

    request = _last(client, functions.contacts.GetBlockedRequest)
    assert request.offset == 0 and request.limit == 100
    assert any(entry.get("id") == 7 for entry in json.loads(result))


@pytest.mark.asyncio
async def test_get_blocked_users_returns_an_empty_list_not_an_error(monkeypatch):
    """Blocking nobody is the normal case and must not read as a failure."""
    _wire(
        monkeypatch,
        contacts_mod,
        Recorder(types.contacts.Blocked(blocked=[], chats=[], users=[])),
    )

    assert json.loads(await contacts_mod.get_blocked_users()) == []


# --- folders ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folders_skips_the_default_pseudo_folder(monkeypatch):
    """`DialogFilterDefault` is "All chats" - a marker for the built-in view, not
    a folder anyone created. Reporting it invents a folder whose id the other
    folder tools cannot use."""
    real = types.DialogFilter(
        id=9,
        title=types.TextWithEntities(text="Work", entities=[]),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
    )
    answer = types.messages.DialogFilters(
        filters=[types.DialogFilterDefault(), real], tags_enabled=False
    )
    client = _wire(monkeypatch, folders_mod, Recorder(answer))

    result = await folders_mod.list_folders()

    _last(client, functions.messages.GetDialogFiltersRequest)
    assert "Work" in result
    assert "DialogFilterDefault" not in result


@pytest.mark.asyncio
async def test_list_folders_unwraps_a_title_that_carries_entities(monkeypatch):
    """A folder title is `TextWithEntities`, not a string. Printing the object
    would put a TL repr where the name belongs."""
    answer = types.messages.DialogFilters(
        filters=[
            types.DialogFilter(
                id=3,
                title=types.TextWithEntities(text="Iran market", entities=[]),
                pinned_peers=[],
                include_peers=[],
                exclude_peers=[],
            )
        ],
        tags_enabled=False,
    )
    _wire(monkeypatch, folders_mod, Recorder(answer))

    result = await folders_mod.list_folders()

    assert "Iran market" in result
    assert "TextWithEntities" not in result


# --- profile ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_status_resolves_the_identifier_it_was_given(monkeypatch):
    """The status comes off the RESOLVED user. Reading it from the raw argument
    would report nothing for a username."""
    resolved = _user(5, status=types.UserStatusRecently())

    async def _resolve(user_id, cl=None, account=None):
        assert user_id == "@someone", "the caller's identifier must reach resolution"
        return resolved

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: Recorder())
    monkeypatch.setattr(profile_mod, "resolve_entity", _resolve)

    assert "UserStatusRecently" in await profile_mod.get_user_status("@someone")


@pytest.mark.asyncio
async def test_get_user_status_reports_a_hidden_status_without_raising(monkeypatch):
    """A user whose last-seen is private has `status=None`. That is an answer, not
    an error, and it is the shape most likely to be met in practice."""

    async def _resolve(user_id, cl=None, account=None):
        return _user(6, status=None)

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: Recorder())
    monkeypatch.setattr(profile_mod, "resolve_entity", _resolve)

    result = await profile_mod.get_user_status(6)

    assert "Error" not in result


# --- accounts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_accounts_names_every_configured_label(monkeypatch):
    """The one tool an agent uses to discover what `account=` may be set to. If it
    omits a label, that account is unreachable through every other tool."""

    class Client:
        def __init__(self, name, phone):
            self._me = types.User(id=1, first_name=name, phone=phone)

        async def get_me(self):
            return self._me

    monkeypatch.setattr(
        accounts_mod,
        "clients",
        {"work": Client("Worker", "100"), "personal": Client("Me", "200")},
    )

    result = await accounts_mod.list_accounts()

    assert "work" in result and "personal" in result
    assert "Worker" in result and "Me" in result


@pytest.mark.asyncio
async def test_list_accounts_keeps_going_when_one_account_cannot_answer(monkeypatch):
    """One unreachable account must not hide the others. This is the discovery
    tool: if it raises, every remaining account becomes unaddressable."""

    class Broken:
        async def get_me(self):
            raise RuntimeError("offline")

    class Fine:
        async def get_me(self):
            return types.User(id=2, first_name="Fine", phone="300")

    monkeypatch.setattr(accounts_mod, "clients", {"broken": Broken(), "fine": Fine()})

    result = await accounts_mod.list_accounts()

    assert "fine" in result and "Fine" in result
    assert "broken" in result
    assert "offline" not in result, "an internal error message leaked into the listing"


# --- stickers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sticker_sets_reports_the_short_name_a_write_tool_needs(monkeypatch):
    """A title cannot be turned back into a set. `inspect_sticker_set` and every
    sticker write tool address a set by `short_name`, so a listing without it is
    a listing of things that cannot then be opened."""
    from telegram_mcp.tools import media as media_mod

    covers = types.messages.AllStickers(
        hash=0,
        sets=[
            types.StickerSet(
                id=99,
                access_hash=1234,
                title="My Pack",
                short_name="my_pack",
                count=7,
                hash=0,
                official=False,
                masks=False,
                emojis=False,
                installed_date=None,
                thumbs=[],
                thumb_dc_id=None,
                thumb_version=None,
                thumb_document_id=None,
            )
        ],
    )
    client = _wire(monkeypatch, media_mod, Recorder(covers))

    result = await media_mod.get_sticker_sets()

    assert _last(client, functions.messages.GetAllStickersRequest).hash == 0
    assert "my_pack" in result, "the short name is the only usable identifier here"
    assert "1234" in result, "access_hash is required by add/remove/move"


@pytest.mark.asyncio
async def test_suggest_sticker_set_name_asks_telegram_rather_than_guessing(monkeypatch):
    """A short name is permanent once the set exists, so the check has to be the
    server's answer, not a local slug."""
    from telegram_mcp.tools import stickers as stickers_mod

    answer = types.stickers.SuggestedShortName(short_name="my_pack_by_bot")
    client = _wire(monkeypatch, stickers_mod, Recorder(answer))

    result = await stickers_mod.suggest_sticker_set_name("My Pack")

    request = _last(client, functions.stickers.SuggestShortNameRequest)
    assert request.title == "My Pack"
    assert "my_pack_by_bot" in result


# --- contacts, continued ----------------------------------------------------


@pytest.mark.asyncio
async def test_export_contacts_asks_for_the_full_list(monkeypatch):
    answer = types.contacts.Contacts(
        contacts=[], saved_count=1, users=[_user(4, first_name="Exported")]
    )
    client = _wire(monkeypatch, contacts_mod, Recorder(answer))

    result = await contacts_mod.export_contacts()

    assert _last(client, functions.contacts.GetContactsRequest).hash == 0
    assert any(entry.get("id") == 4 for entry in json.loads(result))


@pytest.mark.asyncio
async def test_get_last_interaction_refuses_a_peer_that_is_not_a_user(monkeypatch):
    """ "Last interaction with a contact" is meaningless for a channel, and the
    refusal has to come before any history is fetched."""
    client = Recorder()

    async def _resolve(contact_id, cl=None, account=None):
        return types.Chat(
            id=8, title="A Group", photo=None, participants_count=2, date=None, version=1
        )

    monkeypatch.setattr(contacts_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(contacts_mod, "resolve_entity", _resolve)

    result = await contacts_mod.get_last_interaction(8)

    assert "not a user" in result.lower()
    assert client.sent == [], "history was fetched for a peer that cannot have a contact"


# --- profile, continued -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_bot_info_says_so_when_the_username_resolves_to_nothing(monkeypatch):
    """An unresolvable username must not fall through into a request built on
    `None`, where the failure would surface as an opaque RPC error instead."""
    client = Recorder()

    async def _resolve(username, cl=None, account=None):
        return None

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(profile_mod, "resolve_entity", _resolve)

    result = await profile_mod.get_bot_info("@nosuchbot")

    assert "not found" in result.lower()
    assert client.sent == [], "a request went out for an entity that does not exist"


@pytest.mark.asyncio
async def test_list_contacts_says_none_rather_than_an_empty_result(monkeypatch):
    """An account with no contacts is a normal answer. A bare empty payload here
    reads as "the call failed" to whoever is holding it."""
    _wire(
        monkeypatch,
        contacts_mod,
        Recorder(types.contacts.Contacts(contacts=[], saved_count=0, users=[])),
    )

    assert "No contacts" in await contacts_mod.list_contacts()


@pytest.mark.asyncio
async def test_send_contact_addresses_the_resolved_peer(monkeypatch):
    """The caller passes `@somechat`; what must reach the wire is the RESOLVED
    peer. Sending the raw string is invisible in the returned sentence, which is
    identical either way."""
    resolved = types.InputPeerChannel(channel_id=4242, access_hash=7)
    client = Recorder()

    async def _resolve(chat_id, cl=None, account=None):
        return resolved

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(contacts_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(contacts_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(contacts_mod, "ensure_connected", _connected)

    await contacts_mod.send_contact("@somechat", "+15550001111", "Ada")

    assert _last(client, functions.messages.SendMediaRequest).peer is resolved


# --- folders, continued -----------------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_refuses_past_telegram_s_ceiling_without_sending(monkeypatch):
    """Telegram caps folders. Discovering that by RPC error would leave the
    caller with an opaque failure; the refusal has to be local and must not send
    an update that cannot succeed."""
    existing = [
        types.DialogFilter(
            id=n,
            title=types.TextWithEntities(text=f"F{n}", entities=[]),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
        )
        for n in range(1, 11)
    ]
    client = _wire(
        monkeypatch,
        folders_mod,
        Recorder(types.messages.DialogFilters(filters=existing, tags_enabled=False)),
    )

    result = await folders_mod.create_folder("One too many")

    assert "limit is 10" in result
    assert not [
        r for r in client.sent if isinstance(r, functions.messages.UpdateDialogFilterRequest)
    ], "an update went out for a folder Telegram would have refused"


# --- reads that page --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_photos_refuses_a_limit_it_cannot_honour(monkeypatch):
    """Bounded before the request, not after: a negative or absurd limit handed
    to Telegram amplifies the call rather than failing it."""
    client = Recorder()

    async def _resolve(user_id, cl=None, account=None):
        return _user(3)

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(profile_mod, "resolve_entity", _resolve)

    result = await profile_mod.get_user_photos(3, limit=-1)

    assert "limit" in result.lower()
    assert client.sent == [], "a negative limit reached the wire"


@pytest.mark.asyncio
async def test_search_global_reports_an_empty_page_as_empty(monkeypatch):
    """Paging past the end is expected, and must not look like an error."""

    class Client:
        def __init__(self):
            self.calls = []

        async def get_messages(self, entity, limit=None, search=None, add_offset=None):
            self.calls.append((entity, limit, search, add_offset))
            return []

    from telegram_mcp.tools import messages_read as read_mod

    async def _connected(cl=None):
        return None

    client = Client()
    monkeypatch.setattr(read_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(read_mod, "ensure_connected", _connected)

    result = await read_mod.search_global("nothing matches this")

    assert "No messages found" in result
    assert client.calls, "search_global never queried anything"
    assert client.calls[-1][2] == "nothing matches this"


@pytest.mark.asyncio
async def test_search_contacts_reports_no_match_as_no_match(monkeypatch):
    """Zero hits is an answer. Returning an empty payload would leave the caller
    unable to tell it apart from a search that failed."""
    answer = types.contacts.Found(my_results=[], results=[], chats=[], users=[])
    client = _wire(monkeypatch, contacts_mod, Recorder(answer))

    result = await contacts_mod.search_contacts("nobody by this name")

    request = _last(client, functions.contacts.SearchRequest)
    assert request.q == "nobody by this name"
    assert "No contacts found" in result


@pytest.mark.asyncio
async def test_get_saved_history_bounds_the_limit_before_asking(monkeypatch):
    """Saved Messages can be enormous. An unbounded limit turns one tool call
    into an unbounded fetch, which is the failure the shared ceiling exists for."""
    from telegram_mcp.tools import saved as saved_mod

    client = Recorder()

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(saved_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(saved_mod, "ensure_connected", _connected)

    result = await saved_mod.get_saved_history(12345, limit=0)

    assert "limit" in result.lower()
    assert client.sent == [], "a zero limit reached the wire"


# --- the two capture tools --------------------------------------------------
#
# `telegram_mcp/visual/capture.py` has its own 22 tests for the Win32 seam. What
# had no test was the TOOL layer above it: which arguments each tool forwards,
# and whether a bad option is refused before any capture is attempted. Patching
# `_capture_encoded` is what makes that testable off Windows - the point here is
# the call these tools make, not the pixels that come back.


class _Capture:
    """Stands in for `_capture_encoded`, recording exactly how it was called."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return b"\x89PNG-not-real", {"image": {"format": "png"}, "downscaled": False}


@pytest.mark.asyncio
async def test_get_telegram_region_forwards_the_crop_box(monkeypatch):
    """The whole difference between the two tools is that one carries a region.
    Dropping it would silently return the entire window - a much larger image
    than asked for, and correct-looking."""
    from telegram_mcp.tools import visual as visual_mod

    capture = _Capture()
    monkeypatch.setattr(visual_mod, "_capture_encoded", capture)

    result = await visual_mod.get_telegram_region(10, 20, 110, 220)

    _args, kwargs = capture.calls[-1]
    assert kwargs["region"] == (10, 20, 110, 220)
    assert len(result) == 2, "a metadata block and an image are both expected"
    assert "format" in result[0], "the metadata block should describe the image"


@pytest.mark.asyncio
async def test_get_telegram_screen_asks_for_no_region(monkeypatch):
    """Its counterpart: the full-window tool must not smuggle a crop through."""
    from telegram_mcp.tools import visual as visual_mod

    capture = _Capture()
    monkeypatch.setattr(visual_mod, "_capture_encoded", capture)

    await visual_mod.get_telegram_screen()

    _args, kwargs = capture.calls[-1]
    assert kwargs.get("region") is None


@pytest.mark.asyncio
async def test_native_resolution_reaches_the_capture(monkeypatch):
    """`native_resolution` is the expensive switch - a 4K window at native size
    costs roughly 20k tokens. A tool that accepted it and did not pass it on
    would quietly keep downscaling while the caller believed otherwise."""
    from telegram_mcp.tools import visual as visual_mod

    capture = _Capture()
    monkeypatch.setattr(visual_mod, "_capture_encoded", capture)

    await visual_mod.get_telegram_region(0, 0, 50, 50, native_resolution=True)

    assert capture.calls[-1][1]["native_resolution"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "telepathy"},
        {"image_format": "bmp"},
    ],
)
@pytest.mark.asyncio
async def test_a_bad_option_is_refused_before_anything_is_captured(monkeypatch, kwargs):
    """Capturing first and rejecting after would pay for a PrintWindow, a bitmap
    and an encode before noticing the answer cannot be returned."""
    from telegram_mcp.tools import visual as visual_mod

    capture = _Capture()
    monkeypatch.setattr(visual_mod, "_capture_encoded", capture)

    result = await visual_mod.get_telegram_screen(**kwargs)

    assert isinstance(result, str), "a refusal is a sentence, not an image list"
    assert capture.calls == [], "a capture ran for options that were already invalid"


@pytest.mark.asyncio
async def test_a_capture_failure_comes_back_as_a_sentence(monkeypatch):
    """Telegram Desktop not running is the ordinary case on a machine that has
    the server installed. It must read as an explanation, not a traceback."""
    from telegram_mcp.visual.capture import CaptureError
    from telegram_mcp.tools import visual as visual_mod

    async def _fail(*args, **kwargs):
        raise CaptureError("no Telegram window found")

    monkeypatch.setattr(visual_mod, "_capture_encoded", _fail)

    result = await visual_mod.get_telegram_screen()

    assert isinstance(result, str)
    assert "no Telegram window found" in result
