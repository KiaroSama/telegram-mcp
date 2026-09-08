"""Finding things: in one chat, across every chat, and in public posts.

Telegram's own search bar is four different requests wearing one box, and the
difference matters at the call site rather than in the answer. `search_messages`
narrows one chat by sender and media kind; `search_global` sweeps everything
this account can see; `search_posts` reaches public channels the account has
never met; `search_public_chats` looks for the chats themselves.

Two of these fail SILENTLY when they are wrong, so the tests assert on the call
rather than on the reply:

* a media filter that never reaches Telegram returns text messages, which look
  like a perfectly good answer to "show me the photos";
* `messages.searchGlobal` has no `add_offset` field, so Telethon's `add_offset=`
  lands on a dead attribute and every page silently repeats page 1. That is
  fixed here, and `test_page_two_is_not_page_one_again` is the reason.
"""

import json

import pytest
from telethon.tl import functions, types

from telegram_mcp.tools import chats as chats_mod
from telegram_mcp.tools import messages_read as read_mod

CHAT = "@somegroup"
ENTITY = types.InputPeerChannel(channel_id=4242, access_hash=7)
SENDER = types.InputPeerUser(user_id=99, access_hash=3)


class Message:
    """Enough of a message for the record builders, and nothing more."""

    def __init__(self, number, text="", photo=None, chat=None, peer_id=None, views=None):
        self.id = number
        self.message = text
        self.date = None
        self.sender = None
        self.sender_id = None
        self.photo = photo
        self.chat = chat
        self.chat_id = -100123
        self.peer_id = peer_id
        self.views = views
        self.reply_to = None
        self.media = photo


class Client:
    """Records what it was asked, which is the only place the truth shows."""

    def __init__(self, messages=None, answer=None):
        self.asked = []
        self.sent = []
        self._messages = messages if messages is not None else []
        self._answer = answer

    async def get_messages(self, entity, **kwargs):
        self.asked.append((entity, kwargs))
        return self._messages[: kwargs.get("limit", len(self._messages))]

    async def __call__(self, request):
        self.sent.append(request)
        return self._answer


def _resolver(mapping):
    async def resolve(chat_id, _cl=None, _account=None):
        return mapping[chat_id]

    return resolve


# ------------------------------------------------------------- media filters


def test_every_tab_a_client_shows_has_a_name_here():
    """These are the tabs above a Telegram search result. A missing one sends
    the caller to raw Telethon type names, which is the thing this replaces."""
    for tab in ("photos", "videos", "links", "files", "music", "voice"):
        assert tab in read_mod.MEDIA_FILTERS


def test_a_name_becomes_an_instance_not_the_class():
    chosen, problem = read_mod.media_filter("photos")

    assert problem is None
    assert isinstance(chosen, types.InputMessagesFilterPhotos)


def test_an_unknown_media_type_is_refused_and_lists_the_real_ones():
    """Dropping an unrecognised filter returns text messages, which reads as a
    successful answer to "show me the photos"."""
    chosen, problem = read_mod.media_filter("pictures")

    assert chosen is None
    assert "pictures" in problem and "photos" in problem


def test_no_media_type_is_not_an_error():
    assert read_mod.media_filter(None) == (None, None)
    assert read_mod.media_filter("") == (None, None)


# ------------------------------------------------------------ one chat


@pytest.mark.asyncio
async def test_a_person_in_a_group_is_one_call_with_no_query(wire_client):
    """The headline case: "everything Ali posted here". Asserted on the REQUEST
    because an ignored from_user returns the whole chat, which looks fine."""
    client = wire_client(
        read_mod, Client([Message(1, "hi")]), resolve=_resolver({CHAT: ENTITY, "@alireza": SENDER})
    )

    await read_mod.search_messages(chat_id=CHAT, from_user="@alireza")

    entity, kwargs = client.asked[-1]
    assert entity is ENTITY
    assert kwargs["from_user"] is SENDER
    assert kwargs["search"] is None


@pytest.mark.asyncio
async def test_a_media_type_reaches_telegram_as_a_filter(wire_client):
    client = wire_client(read_mod, Client([Message(1)]), entity=ENTITY)

    await read_mod.search_messages(chat_id=CHAT, media_type="photos")

    assert isinstance(client.asked[-1][1]["filter"], types.InputMessagesFilterPhotos)


@pytest.mark.asyncio
async def test_sender_and_media_never_go_to_telegram_together(wire_client):
    """messages.search answers InputFilterInvalidError for from_id with ANY
    media filter - confirmed live against a real supergroup for all nine of
    photos, videos, media, links, files, music, voice, gifs and polls. So the
    filter goes to Telegram and the sender is matched here."""
    mine, theirs = Message(1), Message(2)
    mine.sender_id = 99
    theirs.sender_id = 1234
    client = wire_client(
        read_mod,
        Client([theirs, mine]),
        resolve=_resolver({CHAT: ENTITY, "@alireza": SENDER}),
    )

    payload = json.loads(
        await read_mod.search_messages(chat_id=CHAT, from_user="@alireza", media_type="videos")
    )

    _entity, kwargs = client.asked[-1]
    assert kwargs.get("from_user") is None, "from_user reached Telegram beside a filter"
    assert isinstance(kwargs["filter"], types.InputMessagesFilterVideo)
    assert [r["id"] for r in payload["results"]] == [1], "somebody else's message survived"


@pytest.mark.asyncio
async def test_an_empty_sender_media_answer_says_how_far_it_looked(wire_client):
    """ "Ali posted no videos" and "no videos from Ali in the last 500" are
    different answers, and only one of them is true."""
    other = Message(2)
    other.sender_id = 1234
    wire_client(read_mod, Client([other]), resolve=_resolver({CHAT: ENTITY, "@alireza": SENDER}))

    payload = json.loads(
        await read_mod.search_messages(chat_id=CHAT, from_user="@alireza", media_type="videos")
    )

    assert payload["results"] == []
    assert payload["scanned"] == 1
    assert payload["scan_limit"] == read_mod.SENDER_MEDIA_SCAN
    assert payload["has_more"] is False, "the chat ran out; that is not 'there may be more'"


@pytest.mark.asyncio
async def test_searching_for_nothing_at_all_is_refused(wire_client):
    """Without this it silently becomes get_history under another name."""
    client = wire_client(read_mod, Client([Message(1)]), entity=ENTITY)

    answer = await read_mod.search_messages(chat_id=CHAT)

    assert "get_history" in answer
    assert client.asked == [], "a no-criteria search still hit Telegram"


@pytest.mark.asyncio
async def test_an_unknown_media_type_never_reaches_telegram(wire_client):
    client = wire_client(read_mod, Client([Message(1)]), entity=ENTITY)

    answer = await read_mod.search_messages(chat_id=CHAT, query="x", media_type="pictures")

    assert "Unknown media_type" in answer
    assert client.asked == []


@pytest.mark.asyncio
async def test_a_photo_with_no_caption_still_says_it_is_a_photo(wire_client):
    """A media search returns messages whose text is empty by nature; a page of
    blank rows is not an answer."""
    photo = types.Photo(
        id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1, has_stickers=False
    )
    wire_client(read_mod, Client([Message(1, "", photo=photo)]), entity=ENTITY)

    payload = json.loads(await read_mod.search_messages(chat_id=CHAT, media_type="photos"))

    assert payload["results"][0]["media"]


@pytest.mark.asyncio
async def test_a_plain_text_search_still_works_exactly_as_before(wire_client):
    """The parameters are new; the original call must not have moved."""
    client = wire_client(read_mod, Client([Message(1, "found me")]), entity=ENTITY)

    payload = json.loads(await read_mod.search_messages(CHAT, "found"))

    assert client.asked[-1][1]["search"] == "found"
    assert payload["results"][0]["text"] == "found me"


# ----------------------------------------------------------------- global


@pytest.mark.asyncio
async def test_page_two_is_not_page_one_again(wire_client):
    """messages.searchGlobal has no add_offset field, so Telethon's add_offset
    was being set on an attribute that is never serialised: every page returned
    the same records. Paging is done here now, and this is what proves it."""
    everything = [Message(n, f"m{n}") for n in range(1, 7)]
    client = wire_client(read_mod, Client(everything))

    first = json.loads(await read_mod.search_global(query="m", page=1, page_size=3))
    second = json.loads(await read_mod.search_global(query="m", page=2, page_size=3))

    assert [r["id"] for r in first["results"]] == [1, 2, 3]
    assert [r["id"] for r in second["results"]] == [4, 5, 6]
    # Page 2 must actually ask for enough records to have a page 2 in it.
    assert client.asked[-1][1]["limit"] == 6


@pytest.mark.asyncio
async def test_paging_past_the_ceiling_is_refused_rather_than_fetched(wire_client):
    """Each page is paid for by re-reading everything before it, so the depth is
    a real cost and the refusal says what to do instead."""
    client = wire_client(read_mod, Client([]))

    answer = await read_mod.search_global(query="m", page=500, page_size=100)

    assert "search_messages" in answer
    assert client.asked == []


@pytest.mark.asyncio
async def test_a_global_media_search_needs_no_query(wire_client):
    client = wire_client(read_mod, Client([Message(1)]))

    await read_mod.search_global(media_type="links")

    _entity, kwargs = client.asked[-1]
    assert isinstance(kwargs["filter"], types.InputMessagesFilterUrl)
    assert kwargs["search"] is None


@pytest.mark.asyncio
async def test_a_global_search_for_nothing_is_refused(wire_client):
    client = wire_client(read_mod, Client([Message(1)]))

    answer = await read_mod.search_global()

    assert "media_type" in answer
    assert client.asked == []


# ------------------------------------------------------------------ posts


def _channel(number, title, username):
    return types.Channel(
        id=number,
        title=title,
        photo=None,
        date=None,
        username=username,
        creator=False,
        left=False,
        broadcast=True,
        verified=False,
        megagroup=False,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        access_hash=5,
    )


def _posts_answer():
    return types.messages.MessagesSlice(
        count=97,
        messages=[
            Message(55, "a public post", peer_id=types.PeerChannel(channel_id=777), views=9)
        ],
        chats=[_channel(777, "Some Channel", "somechannel")],
        users=[],
        next_rate=0,
        offset_id_offset=0,
        topics=[],
    )


@pytest.mark.asyncio
async def test_a_found_post_carries_the_link_that_reaches_it(wire_client):
    """A post nobody can open is not a search result."""
    wire_client(read_mod, Client(answer=_posts_answer()))

    payload = json.loads(await read_mod.search_posts(query="something"))

    found = payload["results"][0]
    assert found["link"] == "https://t.me/somechannel/55"
    assert found["chat_name"] == "Some Channel"
    assert found["views"] == 9


@pytest.mark.asyncio
async def test_a_hashtag_loses_its_hash_before_it_is_sent(wire_client):
    """Telegram's hashtag field wants the word; sending '#btc' finds nothing and
    reports it as no results."""
    client = wire_client(read_mod, Client(answer=_posts_answer()))

    await read_mod.search_posts(hashtag="#btc")

    request = client.sent[-1]
    assert isinstance(request, functions.channels.SearchPostsRequest)
    assert request.hashtag == "btc" and request.query is None


@pytest.mark.asyncio
async def test_a_query_and_a_hashtag_together_are_refused(wire_client):
    """They are separate Telegram features; sending both silently drops one."""
    client = wire_client(read_mod, Client(answer=_posts_answer()))

    answer = await read_mod.search_posts(query="btc", hashtag="btc")

    assert "exactly one" in answer
    assert client.sent == []


@pytest.mark.asyncio
async def test_neither_a_query_nor_a_hashtag_is_refused(wire_client):
    client = wire_client(read_mod, Client(answer=_posts_answer()))

    assert "exactly one" in await read_mod.search_posts()
    assert client.sent == []


@pytest.mark.asyncio
async def test_the_total_telegram_reports_survives_into_the_answer(wire_client):
    """ "97 matches, here are 20" and "there are 20" are different answers."""
    wire_client(read_mod, Client(answer=_posts_answer()))

    payload = json.loads(await read_mod.search_posts(query="x"))

    assert payload["total"] == 97


# ------------------------------------------------------------------ chats


def _found(my_results, results, chats, users=()):
    return types.contacts.Found(
        my_results=my_results, results=results, chats=list(chats), users=list(users)
    )


@pytest.mark.asyncio
async def test_a_chat_you_are_already_in_is_marked_as_such(wire_client):
    """Telegram answers in two halves and they mean different things: the group
    you are in, and a public group with a similar name. Flattened together they
    are indistinguishable, which is what this tool used to do."""
    mine = _channel(777, "My Group", "mygroup")
    theirs = _channel(888, "Some Other Group", "othergroup")
    wire_client(
        chats_mod,
        Client(
            answer=_found(
                my_results=[types.PeerChannel(channel_id=777)],
                results=[types.PeerChannel(channel_id=888)],
                chats=[mine, theirs],
            )
        ),
    )

    payload = json.loads(await chats_mod.search_public_chats("group"))

    joined = {row["name"]: row["joined"] for row in payload["results"]}
    assert joined == {"My Group": True, "Some Other Group": False}


@pytest.mark.asyncio
async def test_the_channels_tab_asks_telegram_for_channels(wire_client):
    client = wire_client(chats_mod, Client(answer=_found([], [], [])))

    await chats_mod.search_public_chats("news", kind="channels")

    request = client.sent[-1]
    assert request.broadcasts is True and request.bots is None


@pytest.mark.asyncio
async def test_the_apps_tab_asks_telegram_for_bots(wire_client):
    client = wire_client(chats_mod, Client(answer=_found([], [], [])))

    await chats_mod.search_public_chats("wallet", kind="bots")

    assert client.sent[-1].bots is True


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused_rather_than_ignored(wire_client):
    """Ignoring it searches everything and returns a plausible wrong tab."""
    client = wire_client(chats_mod, Client(answer=_found([], [], [])))

    answer = await chats_mod.search_public_chats("x", kind="stickers")

    assert "Unknown kind" in answer
    assert client.sent == []


@pytest.mark.asyncio
async def test_a_bot_is_reported_as_a_bot_not_as_a_user(wire_client):
    bot = types.User(id=5, first_name="Wallet", bot=True, username="wallet")
    wire_client(chats_mod, Client(answer=_found([], [types.PeerUser(user_id=5)], [], users=[bot])))

    payload = json.loads(await chats_mod.search_public_chats("wallet", kind="bots"))

    assert payload["results"][0]["type"] == "bot"
