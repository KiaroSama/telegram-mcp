"""Five reads the server could not write back, and one write nothing could read.

Every one is the same shape: a tool reports a fact, and the tool that ought to
act on that fact cannot express it. An agent can see the state and not reach it.

* `get_privacy_settings` reports twelve rule kinds; `set_privacy_settings` could
  build three. Since `account.setPrivacy` REPLACES, reading "contacts, plus close
  friends, except Bob" and passing back what the writer could say DELETED the
  close-friends rule -- permanently, with no warning, from a tool whose own
  docstring recommended the round trip.
* Five media senders confirmed a send and returned nothing addressable.
* `channel_settings` writes six toggles; `get_full_chat` read none of them.
* `list_saved_tags` reports a custom-emoji tag as `{"custom_emoji_id": ...}`;
  `name_saved_tag` took an emoticon only, so that tag could not be named.
* `get_message_effect` resolves an id you already have. Nothing listed them, so
  an effect could only be copied off a message that already used it.
"""

import inspect
from types import SimpleNamespace

import pytest
from telethon.tl import functions, types

from telegram_mcp.sent import sent_message_ids
from telegram_mcp.tools import effects as effects_mod
from telegram_mcp.tools import profile as profile_mod
from telegram_mcp.tools import saved as saved_mod


class Recorder:
    def __init__(self, answer=None):
        self.sent = []
        self.answer = answer

    async def __call__(self, request):
        self.sent.append(request)
        return self.answer


# --- privacy: the destructive round trip ------------------------------------


@pytest.fixture
def privacy(monkeypatch):
    client = Recorder()

    async def _resolve(identifier, cl=None, account=None):
        return types.InputPeerUser(user_id=int(identifier), access_hash=0)

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(profile_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(profile_mod, "resolve_entity", _resolve)
    monkeypatch.setattr(profile_mod, "ensure_connected", _connected)
    return client


def test_every_kind_the_reader_reports_can_be_written_back():
    """The bug in one assertion: a name the reader emits and the writer cannot
    build is a rule that a round trip silently destroys."""
    assert set(profile_mod._PRIVACY_RULE_NAMES.values()) == set(profile_mod._PRIVACY_INPUT_RULES)


def test_each_mapped_name_resolves_to_a_real_telethon_type():
    from telethon.tl import types as tl_types

    for class_name in profile_mod._PRIVACY_INPUT_RULES.values():
        assert hasattr(tl_types, class_name), class_name


@pytest.mark.asyncio
async def test_the_rules_a_read_reported_go_back_out_in_order(privacy):
    """Order is the rule: Telegram applies the first match, so a base policy moved
    ahead of its own exceptions swallows them."""
    await profile_mod.set_privacy_settings(
        "status",
        rules=[
            {"rule": "contacts_allowed"},
            {"rule": "close_friends_allowed"},
            {"rule": "users_disallowed", "users": [123]},
        ],
    )

    request = privacy.sent[-1]
    assert isinstance(request, functions.account.SetPrivacyRequest)
    assert [type(r).__name__ for r in request.rules] == [
        "InputPrivacyValueAllowContacts",
        "InputPrivacyValueAllowCloseFriends",
        "InputPrivacyValueDisallowUsers",
    ]


@pytest.mark.asyncio
async def test_a_chat_rule_carries_its_chat_ids(privacy):
    await profile_mod.set_privacy_settings(
        "status", rules=[{"rule": "chats_allowed", "chats": [42, 43]}]
    )

    assert privacy.sent[-1].rules[0].chats == [42, 43]


@pytest.mark.asyncio
async def test_an_unknown_rule_kind_changes_nothing(privacy):
    """Sending the rest would apply a REPLACEMENT missing whichever rule was not
    understood -- the exact deletion this parameter exists to prevent."""
    result = await profile_mod.set_privacy_settings("status", rules=[{"rule": "invented"}])

    assert privacy.sent == []
    assert "invented" in result


@pytest.mark.asyncio
async def test_the_old_convenience_arguments_still_work(privacy):
    """Guard the guard: `rules` is additive, not a replacement."""
    await profile_mod.set_privacy_settings("status", base_policy="contacts")

    assert [type(r).__name__ for r in privacy.sent[-1].rules] == ["InputPrivacyValueAllowContacts"]


# --- the ids five senders threw away ----------------------------------------


def test_a_friendly_send_reports_its_id():
    assert sent_message_ids(SimpleNamespace(id=5)) == [5]


def test_an_album_reports_every_id():
    assert sent_message_ids([SimpleNamespace(id=5), SimpleNamespace(id=6)]) == [5, 6]


def test_updates_are_walked_for_the_id():
    """A raw request answers with Updates; the id arrives in an UpdateMessageID
    rather than on the result, which is why the rich path reported none."""
    result = SimpleNamespace(
        updates=[
            SimpleNamespace(id=9, message=None),
            SimpleNamespace(message=SimpleNamespace(id=9)),
        ]
    )

    assert sent_message_ids(result) == [9], "the same id must not be reported twice"


def test_an_unfamiliar_receipt_is_not_an_error():
    """The send happened. Reporting failure because the receipt had an odd shape
    would be worse than reporting no id."""
    assert sent_message_ids(SimpleNamespace(nothing=1)) == []
    assert sent_message_ids(None) == []


def test_a_media_send_reports_the_id_beside_its_sentence():
    from telegram_mcp.tools import media as media_mod

    result = media_mod._sent_result(SimpleNamespace(id=4242), -100, "File sent.")

    assert '"message_id": 4242' in result
    assert "File sent." in result, "the original sentence must survive as detail"


def test_a_send_with_no_id_still_reports_success():
    from telegram_mcp.tools import media as media_mod

    assert media_mod._sent_result(None, -100, "GIF sent.") == "GIF sent."


def test_no_media_sender_discards_its_result():
    """The defect was uniform: every one of them awaited the send and dropped what
    came back. A sixth added the same way would fail here."""
    from telegram_mcp.tools import media as media_mod

    source = inspect.getsource(media_mod)

    assert "        await cl.send_file(" not in source
    assert "            await cl.send_file(" not in source


# --- the six write-only toggles ---------------------------------------------


def test_get_full_chat_reads_back_every_toggle_channel_settings_writes():
    """Pins the pairing itself: a seventh setter with no reader fails here."""
    from telegram_mcp.tools import channel_settings as settings_mod
    from telegram_mcp.tools import chats as chats_mod

    setters = {
        name[len("set_") :]
        for name, obj in vars(settings_mod).items()
        if name.startswith("set_") and callable(obj)
    }
    read_source = inspect.getsource(chats_mod.get_full_chat)

    # The setter names and the reported keys differ by design; this maps them.
    pairs = {
        "join_to_send": "join_to_send",
        "join_request": "join_request",
        "prehistory_hidden": "hidden_prehistory",
        "participants_hidden": "participants_hidden",
        "signatures": "signatures",
        "view_forum_as_messages": "view_forum_as_messages",
    }
    assert setters == set(pairs), f"a toggle changed: {setters ^ set(pairs)}"
    for key in pairs.values():
        assert f'"{key}"' in read_source, f"{key} is written but never read back"


# --- naming a premium tag ----------------------------------------------------


@pytest.fixture
def tags(monkeypatch):
    client = Recorder()

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(saved_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(saved_mod, "ensure_connected", _connected)
    return client


@pytest.mark.asyncio
async def test_a_custom_emoji_tag_can_be_named(tags):
    await saved_mod.name_saved_tag(custom_emoji_id=5361234567890123456, title="Receipts")

    request = tags.sent[-1]
    assert isinstance(request.reaction, types.ReactionCustomEmoji)
    assert request.reaction.document_id == 5361234567890123456
    assert request.title == "Receipts"


@pytest.mark.asyncio
async def test_naming_a_standard_tag_is_unchanged(tags):
    """Guard the guard: `emoji` was positional and must stay first."""
    await saved_mod.name_saved_tag("🔖", "Receipts")

    assert tags.sent[-1].reaction == types.ReactionEmoji(emoticon="🔖")


@pytest.mark.asyncio
async def test_naming_nothing_is_refused(tags):
    result = await saved_mod.name_saved_tag(title="Receipts")

    assert tags.sent == []
    assert "custom_emoji_id" in result


@pytest.mark.asyncio
async def test_both_at_once_is_refused(tags):
    """Naming names exactly one tag; two reactions would silently name one."""
    result = await saved_mod.name_saved_tag("🔖", "R", custom_emoji_id=1)

    assert tags.sent == []
    assert "not both" in result


# --- discovering an effect ---------------------------------------------------


def _effect(ident, emoticon, premium):
    return types.AvailableEffect(
        id=ident,
        emoticon=emoticon,
        effect_sticker_id=ident + 1,
        premium_required=premium,
        static_icon_id=None,
        effect_animation_id=None,
    )


@pytest.fixture
def catalog(monkeypatch):
    effects = {n: _effect(n, "🔥" if n % 2 else "🎉", premium=n > 2) for n in range(1, 6)}
    snapshot = SimpleNamespace(effects=effects, documents={})

    async def _load(cl, account=None):
        return snapshot, False

    async def _connected(cl=None):
        return None

    monkeypatch.setattr(effects_mod, "get_client", lambda account=None: object())
    monkeypatch.setattr(effects_mod, "ensure_connected", _connected)
    monkeypatch.setattr(effects_mod, "load_catalog", _load)
    return snapshot


@pytest.mark.asyncio
async def test_the_catalogue_can_be_listed_at_all(catalog):
    result = await effects_mod.list_message_effects(account="a")

    assert '"total": 5' in result
    assert '"id": 1' in result


@pytest.mark.asyncio
async def test_paging_is_stable(catalog):
    """Sorted by id: a dict in arrival order would reshuffle between pages."""
    first = await effects_mod.list_message_effects(limit=2, account="a")
    second = await effects_mod.list_message_effects(limit=2, offset=2, account="a")

    assert '"has_more": true' in first
    assert '"id": 3' in second and '"id": 1' not in second


@pytest.mark.asyncio
async def test_an_emoticon_filter_narrows_the_total(catalog):
    result = await effects_mod.list_message_effects(emoticon="🎉", account="a")

    assert '"total": 2' in result


@pytest.mark.asyncio
async def test_premium_only_narrows_to_the_gated_ones(catalog):
    result = await effects_mod.list_message_effects(premium_only=True, account="a")

    assert '"total": 3' in result


@pytest.mark.asyncio
async def test_a_negative_offset_is_refused(catalog):
    """Python would silently index from the end and serve the wrong page."""
    result = await effects_mod.list_message_effects(offset=-1, account="a")

    assert "offset" in result
