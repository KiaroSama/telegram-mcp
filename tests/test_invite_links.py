"""Named invite links: which request goes out, and what a missing argument means.

No network. Two behaviours carry most of the weight here:

* `edit_invite_link` treats an omitted argument as "leave it alone" and a 0 as
  "clear it", because Telegram's own request does. A tool that sent None for an
  unmentioned field would silently keep an expiry the caller meant to remove.
* `create_invite_link` must never be confused with `export_chat_invite`, which
  replaces the chat's PRIMARY link and locks out everyone holding the old one.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import invite_links as mod


def _invite(link="https://t.me/+abc", **extra):
    fields = {
        "link": link,
        "title": None,
        "revoked": False,
        "permanent": False,
        "request_needed": False,
        "usage": None,
        "usage_limit": None,
        "requested": None,
        "date": None,
        "expire_date": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class _Client:
    def __init__(self, answers=None):
        self.requests = []
        self.answers = answers or {}

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in self.answers:
            answer = self.answers[name]
            return answer(request) if callable(answer) else answer
        return _invite()

    def names(self):
        return [type(r).__name__ for r in self.requests]

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def wire(monkeypatch):
    def _wire(client=None, entity=None):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(target, _client):
            return entity or SimpleNamespace(id=int(str(target).lstrip("-@") or 1))

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return _wire


def _records(answer):
    return json.loads(answer)["results"]


# --- creating ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_condition_reaches_the_request(wire):
    client = wire()

    await mod.create_invite_link(
        chat_id=-100123, title="Newsletter", expire_seconds=3600, usage_limit=25, account="a"
    )

    sent = client.sent("ExportChatInviteRequest")
    assert sent.title == "Newsletter"
    assert sent.usage_limit == 25
    assert sent.expire_date is not None
    # A deadline, not a duration: Telegram takes an absolute time, so an hour
    # passed as 3600 has to arrive as roughly now+3600 in UTC.
    ahead = (sent.expire_date - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < ahead <= 3600, f"expiry landed at {ahead}s from now"


@pytest.mark.asyncio
async def test_no_conditions_sends_none_rather_than_zero(wire):
    """0 and None are different requests: 0 means "cap it at zero joins", which
    would mint a link nobody can use."""
    client = wire()

    await mod.create_invite_link(chat_id=-100123, account="a")

    sent = client.sent("ExportChatInviteRequest")
    assert sent.usage_limit is None
    assert sent.expire_date is None
    assert sent.title is None
    assert sent.request_needed is None


@pytest.mark.asyncio
async def test_approval_and_a_usage_limit_together_are_refused_locally(wire):
    """Telegram rejects the combination. Refusing here names the reason instead
    of returning whichever error code the server picks."""
    client = wire()

    answer = await mod.create_invite_link(
        chat_id=-100123, requires_approval=True, usage_limit=10, account="a"
    )

    assert "one or the other" in answer
    assert client.requests == [], "an impossible request went out anyway"


@pytest.mark.asyncio
async def test_a_zero_usage_limit_is_refused_rather_than_minting_a_dead_link(wire):
    client = wire()

    answer = await mod.create_invite_link(chat_id=-100123, usage_limit=0, account="a")

    assert "at least 1" in answer
    assert client.requests == []


@pytest.mark.asyncio
async def test_an_exhausted_link_is_marked_as_such(wire):
    """A used-up link looks identical to a live one in the raw reply. The caller
    cannot work it out from the string, so the listing says it."""
    wire(_Client({"ExportChatInviteRequest": _invite(usage=25, usage_limit=25)}))

    record = _records(await mod.create_invite_link(chat_id=-100123, account="a"))[0]

    assert record["exhausted"] is True


# --- editing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unmentioned_condition_is_not_sent_at_all(wire):
    """The trap. Telegram reads a missing field as "unchanged", so a tool that
    sent None for everything unmentioned would look like it cleared them and
    change nothing. Only what was asked for goes on the wire."""
    client = wire()

    await mod.edit_invite_link(
        chat_id=-100123, link="https://t.me/+abc", title="Renamed", account="a"
    )

    sent = client.sent("EditExportedChatInviteRequest")
    assert sent.title == "Renamed"
    assert sent.usage_limit is None, "an untouched limit was sent"
    assert sent.expire_date is None, "an untouched expiry was sent"
    assert sent.request_needed is None


@pytest.mark.asyncio
async def test_zero_clears_a_condition_rather_than_leaving_it(wire):
    """0 is Telegram's "no limit". It has to reach the request, which is exactly
    what an `if not value` guard would drop."""
    client = wire()

    await mod.edit_invite_link(
        chat_id=-100123, link="https://t.me/+abc", usage_limit=0, expire_seconds=0, account="a"
    )

    sent = client.sent("EditExportedChatInviteRequest")
    assert sent.usage_limit == 0, "clearing the cap did not reach Telegram"
    assert sent.expire_date is not None, "clearing the expiry did not reach Telegram"


@pytest.mark.asyncio
async def test_an_edit_that_changes_nothing_is_refused(wire):
    client = wire()

    answer = await mod.edit_invite_link(chat_id=-100123, link="https://t.me/+abc", account="a")

    assert "Nothing to change" in answer
    assert client.requests == []


@pytest.mark.asyncio
async def test_editing_without_a_link_says_where_to_get_one(wire):
    client = wire()

    answer = await mod.edit_invite_link(chat_id=-100123, link="", title="x", account="a")

    assert "list_invite_links" in answer
    assert client.requests == []


# --- revoking ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_leaves_the_record_unless_deletion_is_asked_for(wire):
    """The revoked record is often the only evidence of where a link went, so
    removing it is a second, separate decision."""
    client = wire(_Client({"EditExportedChatInviteRequest": _invite(revoked=True)}))

    await mod.revoke_invite_link(chat_id=-100123, link="https://t.me/+abc", account="a")

    assert "DeleteExportedChatInviteRequest" not in client.names()
    assert client.sent("EditExportedChatInviteRequest").revoked is True


@pytest.mark.asyncio
async def test_deleting_revokes_first(wire):
    """Order matters: deleting a live link would remove the record while the link
    still worked."""
    client = wire(_Client({"EditExportedChatInviteRequest": _invite(revoked=True)}))

    record = _records(
        await mod.revoke_invite_link(
            chat_id=-100123, link="https://t.me/+abc", delete=True, account="a"
        )
    )[0]

    assert client.names() == [
        "EditExportedChatInviteRequest",
        "DeleteExportedChatInviteRequest",
    ]
    assert record["deleted_from_list"] is True


@pytest.mark.asyncio
async def test_revoking_says_that_members_stay(wire):
    """The commonest wrong assumption about revoking: people who already joined
    are unaffected."""
    wire(_Client({"EditExportedChatInviteRequest": _invite(revoked=True)}))

    answer = await mod.revoke_invite_link(chat_id=-100123, link="https://t.me/+abc", account="a")

    assert "never removes a member" in answer


# --- listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_says_it_only_covers_this_accounts_links(wire):
    """Telegram indexes exported links by their admin, so an empty list means
    "you made none" and never "the chat has none". Reporting it as the chat's
    links would be a plain falsehood."""
    wire(
        _Client({"GetExportedChatInvitesRequest": SimpleNamespace(invites=[_invite()], users=[])})
    )

    answer = await mod.list_invite_links(chat_id=-100123, account="a")

    assert "Only links THIS account created" in answer


@pytest.mark.asyncio
async def test_revoked_links_are_a_separate_listing(wire):
    """They are two lists in Telegram, not one list with a flag - asking for the
    live ones must not quietly include the dead ones."""
    client = wire(
        _Client({"GetExportedChatInvitesRequest": SimpleNamespace(invites=[], users=[])})
    )

    await mod.list_invite_links(chat_id=-100123, account="a")
    await mod.list_invite_links(chat_id=-100123, revoked=True, account="a")

    live, revoked = client.requests
    assert live.revoked is None
    assert revoked.revoked is True


# --- join requests ----------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_requests_carry_the_id_the_approval_takes(wire):
    importer = SimpleNamespace(user_id=77, date=None, about="let me in")
    user = SimpleNamespace(id=77, first_name="Ada", last_name="L", username="ada")
    wire(
        _Client(
            {
                "GetChatInviteImportersRequest": SimpleNamespace(
                    importers=[importer], users=[user], count=1
                )
            }
        )
    )

    record = _records(await mod.list_join_requests(chat_id=-100123, account="a"))[0]

    assert record["user_id"] == 77
    assert record["username"] == "ada"
    assert "Ada" in record["name"]


@pytest.mark.asyncio
async def test_declining_is_not_a_ban(wire):
    """Declining removes the request and nothing else. Reading it as a ban would
    stop an admin from ever letting the person in later."""
    client = wire()

    answer = await mod.approve_join_request(
        chat_id=-100123, user_id=77, approved=False, account="a"
    )

    assert client.sent("HideChatJoinRequestRequest").approved is False
    assert "without banning" in answer
    assert _records(answer)[0]["in_chat"] is False
