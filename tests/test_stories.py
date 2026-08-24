"""Stories: reading them without touching them, and posting one deliberately.

The claim worth testing here is a negative one — describing a story must not mark
it seen, because that is visible to the person who posted it. So the fake client
records every request it is handed and the tests assert what was NOT sent, not
only what came back.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.tl import functions, types

from tests.helpers_handles import refuse_source, source_gate

from telegram_mcp.tools import stories as mod
from telegram_mcp.tools.stories import (
    PRIVACY_RULES,
    STORY_PERIODS,
    _describe_story,
    get_stories,
    list_peer_stories,
    post_story,
    react_to_story,
)

NOW = datetime.now(timezone.utc)


def _story(story_id=7, caption="", expires=None, media=None, **kwargs):
    return types.StoryItem(
        id=story_id,
        date=NOW - timedelta(hours=1),
        expire_date=expires if expires is not None else NOW + timedelta(hours=23),
        media=media if media is not None else types.MessageMediaPhoto(photo=None),
        caption=caption,
        **kwargs,
    )


def _video(size=987654, duration=15):
    document = types.Document(
        id=42,
        access_hash=0,
        file_reference=b"",
        date=NOW,
        mime_type="video/mp4",
        size=size,
        dc_id=2,
        attributes=[types.DocumentAttributeVideo(duration=duration, w=720, h=1280)],
    )
    return types.MessageMediaDocument(document=document)


class _Client:
    """Records every TL request, so a test can assert one was never sent."""

    def __init__(self, result=None):
        self.requests = []
        self.result = result
        self.uploaded = []

    async def __call__(self, request):
        self.requests.append(request)
        if callable(self.result):
            return self.result(request)
        return self.result

    async def _file_to_media(self, path):
        self.uploaded.append(path)
        return None, types.InputMediaUploadedPhoto(file=SimpleNamespace()), True

    def sent(self, request_type):
        return [r for r in self.requests if isinstance(r, request_type)]


@pytest.fixture
def _wire(monkeypatch):
    def wire(client):
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


def _peer_result(items, max_read_id=0):
    return SimpleNamespace(stories=SimpleNamespace(stories=items, max_read_id=max_read_id))


# --- describing one story ---------------------------------------------------


def test_a_hostile_caption_is_cleaned_like_any_other_user_text():
    """A bidi override in a caption reverses everything after it on screen."""
    described = _describe_story(_story(caption="holiday‮gpj.exe"))

    assert "‮" not in described["caption"]
    assert "holiday" in described["caption"]


def test_caption_entities_are_described_alongside_the_text():
    story = _story(caption="look here", entities=[types.MessageEntityBold(offset=0, length=4)])

    described = _describe_story(story)

    assert described["caption_entities"] == [
        {"type": "bold", "offset": 0, "length": 4, "text": "look"}
    ]


def test_media_is_described_through_the_shared_describer_not_re_derived():
    """A StoryItem has none of Telethon's Message properties, so the wrapper is
    what makes kind/size/duration come out at all."""
    described = _describe_story(_story(media=_video()))["media"]

    assert described["kind"] == "video"
    assert described["size_bytes"] == 987654
    assert described["duration_seconds"] == 15
    assert described["mime_type"] == "video/mp4"


def test_an_expired_story_is_marked_expired():
    expired = _describe_story(_story(expires=NOW - timedelta(minutes=1)))
    live = _describe_story(_story(expires=NOW + timedelta(hours=5)))

    assert expired["expired"] is True
    assert live["expired"] is False


def test_privacy_and_pinning_are_reported_by_name():
    story = _story(pinned=True, privacy=[types.PrivacyValueAllowCloseFriends()])

    described = _describe_story(story)

    assert described["pinned_to_profile"] is True
    assert described["privacy"] == ["allow_close_friends"]


def test_views_carry_the_per_emoji_breakdown():
    story = _story(
        views=types.StoryViews(
            views_count=12,
            forwards_count=2,
            reactions_count=3,
            reactions=[types.ReactionCount(reaction=types.ReactionEmoji("+1"), count=3)],
        )
    )

    views = _describe_story(story)["views"]

    assert views["views"] == 12
    assert views["forwards"] == 2
    assert views["reaction_breakdown"]["items"] == [{"count": 3, "emoji": "+1"}]


def test_a_skipped_story_says_it_is_fetchable_rather_than_hidden():
    described = _describe_story(
        types.StoryItemSkipped(id=8, date=NOW, expire_date=NOW + timedelta(hours=2))
    )

    assert described["state"] == "not_fetched"
    assert "get_stories" in described["hint"]


def test_a_deleted_story_is_not_dressed_up_as_content():
    assert _describe_story(types.StoryItemDeleted(id=9)) == {"story_id": 9, "state": "deleted"}


# --- listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_with_no_stories_says_so_without_implying_none_ever_existed(_wire):
    _wire(_Client(result=_peer_result([])))

    result = await list_peer_stories(1, account="a")

    assert "no stories up right now" in result
    assert "not that none were ever posted" in result


@pytest.mark.asyncio
async def test_listing_reports_the_existing_read_marker_without_setting_one(_wire):
    """The whole point: the poster must not see a new name in their viewer list
    because an agent listed the stories."""
    client = _wire(_Client(result=_peer_result([_story(4), _story(9)], max_read_id=4)))

    payload = json.loads(await list_peer_stories(1, account="a"))
    seen = {r["story_id"]: r["seen_by_you"] for r in payload["results"]}

    assert seen == {4: True, 9: False}
    assert client.sent(functions.stories.GetPeerStoriesRequest), "the listing request was not sent"
    assert not client.sent(functions.stories.ReadStoriesRequest), "listing marked the stories read"
    assert "never calls it" in payload["note"]


@pytest.mark.asyncio
async def test_listing_asks_for_the_resolved_peer(_wire):
    client = _wire(_Client(result=_peer_result([_story(4)])))

    await list_peer_stories("durov", account="a")

    assert client.requests[0].peer.id == "durov"


# --- fetching by id ---------------------------------------------------------


@pytest.mark.asyncio
async def test_fetching_by_id_sends_the_ids_and_marks_nothing_read(_wire):
    client = _wire(_Client(result=SimpleNamespace(stories=[_story(7, caption="hi")])))

    payload = json.loads(await get_stories(1, [7], account="a"))

    request = client.sent(functions.stories.GetStoriesByIDRequest)[0]
    assert request.id == [7]
    assert not client.sent(functions.stories.ReadStoriesRequest)
    assert payload["results"][0]["caption"] == "hi"


@pytest.mark.asyncio
async def test_a_single_id_does_not_have_to_be_wrapped_in_a_list(_wire):
    client = _wire(_Client(result=SimpleNamespace(stories=[_story(7)])))

    await get_stories(1, 7, account="a")

    assert client.sent(functions.stories.GetStoriesByIDRequest)[0].id == [7]


@pytest.mark.asyncio
async def test_ids_that_did_not_come_back_are_named_rather_than_dropped(_wire):
    _wire(_Client(result=SimpleNamespace(stories=[_story(7)])))

    payload = json.loads(await get_stories(1, [7, 8], account="a"))

    assert payload["not_returned"] == [8]
    assert "Expired, deleted, or never visible" in payload["not_returned_reason"]


@pytest.mark.asyncio
async def test_nothing_returned_explains_that_expiry_and_privacy_look_identical(_wire):
    _wire(_Client(result=SimpleNamespace(stories=[])))

    result = await get_stories(1, [7], account="a")

    assert "dropped" in result and "never allowed to see" in result


# --- reacting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_reacting_sends_the_emoji_and_confirms_what_landed(_wire):
    echo = SimpleNamespace(
        updates=[SimpleNamespace(story=SimpleNamespace(sent_reaction=types.ReactionEmoji("+1")))]
    )
    client = _wire(_Client(result=echo))

    payload = json.loads(await react_to_story(1, 7, "+1", account="a"))

    request = client.sent(functions.stories.SendReactionRequest)[0]
    assert request.story_id == 7
    assert request.reaction.emoticon == "+1"
    assert payload["results"][0]["confirmed_reaction"] == "+1"
    assert payload["results"][0]["removed"] is False


@pytest.mark.asyncio
async def test_omitting_the_emoji_removes_the_reaction_rather_than_sending_an_empty_one(_wire):
    client = _wire(_Client(result=SimpleNamespace(updates=[])))

    payload = json.loads(await react_to_story(1, 7, account="a"))

    request = client.sent(functions.stories.SendReactionRequest)[0]
    assert isinstance(request.reaction, types.ReactionEmpty)
    assert payload["results"][0]["removed"] is True


# --- posting ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_posting_is_refused_cleanly_when_allowed_roots_are_unconfigured(monkeypatch, _wire):
    """The path gate is upstream's; a story must not be a way around it."""
    client = _wire(_Client())
    seen = {}

    def _gate(*, raw_path, ctx, tool_name):
        seen["tool_name"] = tool_name
        return refuse_source(f"{tool_name} is disabled until allowed roots are configured.")

    monkeypatch.setattr(mod, "_open_verified_source", _gate)

    result = await post_story("holiday.jpg", "contacts", account="a")

    assert "allowed roots" in result
    assert seen["tool_name"] == "post_story"
    assert client.uploaded == [], "the file was uploaded despite the gate refusing it"
    assert client.requests == [], "a story was posted despite the gate refusing it"


@pytest.mark.asyncio
async def test_an_unknown_privacy_value_is_refused_before_anything_is_uploaded(monkeypatch, _wire):
    client = _wire(_Client())

    def _gate(*, raw_path, ctx, tool_name):  # pragma: no cover - must not run
        raise AssertionError("the file was opened before privacy was validated")

    monkeypatch.setattr(mod, "_open_verified_source", _gate)

    result = await post_story("holiday.jpg", "friends-ish", account="a")

    assert "privacy must be one of" in result
    assert "There is no default" in result
    assert client.uploaded == []


@pytest.mark.asyncio
async def test_an_unsupported_lifetime_is_refused_with_the_accepted_ones(monkeypatch, _wire):
    _wire(_Client())

    def _gate(*, raw_path, ctx, tool_name):  # pragma: no cover - must not run
        raise AssertionError("the file was opened before the period was validated")

    monkeypatch.setattr(mod, "_open_verified_source", _gate)

    result = await post_story("holiday.jpg", "contacts", hours=36, account="a")

    assert "hours must be one of" in result
    assert "24" in result


@pytest.mark.asyncio
async def test_posting_builds_the_request_with_the_chosen_privacy_and_period(
    monkeypatch, _wire, tmp_path
):
    posted = SimpleNamespace(updates=[SimpleNamespace(story=SimpleNamespace(id=31))])
    client = _wire(_Client(result=posted))
    photo = tmp_path / "holiday.jpg"
    photo.write_bytes(b"jpeg")

    monkeypatch.setattr(mod, "_open_verified_source", source_gate(lambda raw: (photo, None)))

    payload = json.loads(
        await post_story("holiday.jpg", "close_friends", caption="hi", hours=48, account="a")
    )

    request = client.sent(functions.stories.SendStoryRequest)[0]
    assert isinstance(request.privacy_rules[0], types.InputPrivacyValueAllowCloseFriends)
    assert request.period == STORY_PERIODS[48]
    assert request.caption == "hi"
    assert payload["results"][0]["story_id"] == 31
    assert payload["results"][0]["privacy"] == "close_friends"


@pytest.mark.asyncio
async def test_a_document_is_refused_instead_of_posted_as_a_broken_story(
    monkeypatch, _wire, tmp_path
):
    """Telegram takes a photo or a video; uploading a PDF first and letting the
    server reject it wastes the upload and reports a confusing RPC error."""
    client = _wire(_Client())
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")

    async def _pdf(_handle):
        return (
            None,
            types.InputMediaUploadedDocument(
                file=SimpleNamespace(), mime_type="application/pdf", attributes=[]
            ),
            False,
        )

    monkeypatch.setattr(mod, "_open_verified_source", source_gate(lambda raw: (report, None)))
    monkeypatch.setattr(client, "_file_to_media", _pdf)

    result = await post_story("report.pdf", "contacts", account="a")

    assert "photo or a video" in result
    assert "no story was posted" in result
    assert client.requests == []


def test_every_named_privacy_value_maps_to_a_real_input_privacy_rule():
    for name, rule in PRIVACY_RULES.items():
        assert rule().__class__.__name__.startswith("InputPrivacyValue"), name
