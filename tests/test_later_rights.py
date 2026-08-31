"""Setting an admin right through TDLib, on the layer Telethon cannot reach.

No network and no TDLib binary: the client is a recorder, so these assert on the
request that would be sent.

The load-bearing test is `test_every_other_right_is_preserved_exactly`. This tool
rewrites the WHOLE rights object to change one field, which is the design that
avoids a name mapping — and it is also the design where a bug silently revokes
rights nobody mentioned. A permissions regression does not raise; it just leaves
an admin unable to do something next week. So the untouched fields are pinned
directly, not inferred from the one that changed.
"""

import json

import pytest

from telegram_mcp.tools import later_rights as lr

# What TDLib hands back for an ordinary admin. Deliberately a mixture of on and
# off, so a test that clobbered everything to one value would be visible.
BASE_RIGHTS = {
    "@type": "chatAdministratorRights",
    "can_manage_chat": True,
    "can_change_info": True,
    "can_post_messages": True,
    "can_edit_messages": False,
    "can_delete_messages": True,
    "can_invite_users": True,
    "can_restrict_members": False,
    "can_pin_messages": True,
    "can_manage_topics": False,
    "can_promote_members": False,
    "can_manage_video_chats": True,
    "can_post_stories": False,
    "can_edit_stories": False,
    "can_delete_stories": False,
    "can_manage_direct_messages": True,
    "can_manage_tags": False,
    "can_send_welcome_messages": False,
    "is_anonymous": False,
}


class FakeTDLib:
    """Answers `getChatMember` from a mutable status and records every request.

    `setChatMemberStatus` writes back into that status, so a read after a write
    sees what was written — which is what makes the tool's own before/after
    check meaningful here rather than tautological.
    """

    def __init__(self, status=None, obey_writes=True):
        self.requests = []
        self.obey_writes = obey_writes
        self.status = (
            status
            if status is not None
            else {
                "@type": "chatMemberStatusAdministrator",
                "can_be_edited": True,
                "rights": dict(BASE_RIGHTS),
            }
        )

    async def request(self, obj, timeout=30.0):
        self.requests.append(obj)
        kind = obj["@type"]
        if kind == "getChatMember":
            return {"@type": "chatMember", "status": json.loads(json.dumps(self.status))}
        if kind == "setChatMemberStatus":
            if self.obey_writes:
                self.status = json.loads(json.dumps(obj["status"]))
            return {"@type": "ok"}
        return {"@type": "ok"}

    def sent_rights(self):
        """The rights object of the last write."""
        writes = [r for r in self.requests if r["@type"] == "setChatMemberStatus"]
        return writes[-1]["status"]["rights"] if writes else None


@pytest.fixture
def wire(monkeypatch):
    def _wire(**kwargs):
        client = FakeTDLib(**kwargs)
        monkeypatch.setattr(lr, "account_label", lambda account=None: "acct")

        async def _client(label):
            return client

        monkeypatch.setattr(lr, "secret_client", _client)
        return client

    return _wire


def _results(raw):
    return json.loads(raw)["results"]


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_other_right_is_preserved_exactly(wire):
    """The whole object is rewritten to change one field. Anything else that
    moves is a right revoked by accident, which fails silently and later."""
    client = wire()

    await lr.set_admin_right(
        chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
    )

    sent = client.sent_rights()
    changed = {k for k in BASE_RIGHTS if k != "@type" and sent[k] != BASE_RIGHTS[k]}
    assert changed == {"can_send_welcome_messages"}, f"collateral change: {changed}"
    assert set(sent) == set(BASE_RIGHTS), "a field was added or dropped from the rights object"


@pytest.mark.asyncio
async def test_a_right_that_moved_without_being_asked_is_reported(wire):
    """The tool checks its own work. If Telegram (or a bug) changes a field the
    caller never named, saying so beats a clean-looking success."""
    client = wire()

    async def _meddle(obj, timeout=30.0):
        client.requests.append(obj)
        if obj["@type"] == "setChatMemberStatus":
            rights = json.loads(json.dumps(obj["status"]["rights"]))
            rights["can_pin_messages"] = not rights["can_pin_messages"]
            client.status = {**obj["status"], "rights": rights}
            return {"@type": "ok"}
        return {"@type": "chatMember", "status": json.loads(json.dumps(client.status))}

    client.request = _meddle

    result = _results(
        await lr.set_admin_right(
            chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
        )
    )

    assert result["unexpectedly_changed"] == ["can_pin_messages"]


# --------------------------------------------------------------------------
# The right that motivated the module
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_welcome_right_can_be_set_and_is_read_back(wire):
    """The one Telethon's layer drops. Reported from a read after the write, so
    the result is evidence rather than an assumption about what was sent."""
    wire()

    result = _results(
        await lr.set_admin_right(
            chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
        )
    )

    assert result["before"] is False
    assert result["after"] is True
    assert result["applied"] is True


@pytest.mark.asyncio
async def test_the_mtproto_name_is_accepted_for_it(wire):
    """A caller arrives from `edit_admin_rights` holding the MTProto name. The
    alias exists only where the correspondence is unambiguous."""
    wire()

    result = _results(
        await lr.set_admin_right(
            chat_id=-100123, user_id=7, right="manage_welcome_messages", account="acct"
        )
    )

    assert result["right"] == "can_send_welcome_messages"
    assert result["after"] is True


@pytest.mark.asyncio
async def test_a_right_can_be_revoked_as_well_as_granted(wire):
    wire()

    result = _results(
        await lr.set_admin_right(
            chat_id=-100123,
            user_id=7,
            right="can_pin_messages",
            enabled=False,
            account="acct",
        )
    )

    assert result["before"] is True
    assert result["after"] is False


@pytest.mark.asyncio
async def test_telegram_declining_the_change_is_reported_not_hidden(wire):
    """The exact failure this module routes around: a request Telegram accepts
    while the flag stays off. A tool that trusted its own write would report
    success for a right that is not there."""
    wire(obey_writes=False)

    result = _results(
        await lr.set_admin_right(
            chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
        )
    )

    assert result["applied"] is False
    assert result["after"] is False
    assert "declining it" in result["note"]


# --------------------------------------------------------------------------
# Refusals that name the right next step
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_right_lists_what_this_tdlib_actually_has(wire):
    """The list comes from the object that just arrived, so it cannot go stale
    against the installed build."""
    client = wire()

    result = await lr.set_admin_right(
        chat_id=-100123, user_id=7, right="can_do_the_thing", account="acct"
    )

    assert "Unknown right" in result
    assert "can_send_welcome_messages" in result
    assert client.sent_rights() is None, "wrote despite refusing the right name"


@pytest.mark.asyncio
async def test_a_creator_is_refused_with_the_reason(wire):
    """A creator holds every right implicitly and Telegram stores no flags for
    them, so 'set this flag' has no meaning rather than merely failing."""
    client = wire(status={"@type": "chatMemberStatusCreator", "is_anonymous": False})

    result = await lr.set_admin_right(
        chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
    )

    assert "creator" in result
    assert client.sent_rights() is None


@pytest.mark.asyncio
async def test_a_non_admin_is_sent_to_promote_admin(wire):
    """Granting a right to a plain member is a promotion, which is a different
    decision with a different tool."""
    client = wire(status={"@type": "chatMemberStatusMember"})

    result = await lr.set_admin_right(
        chat_id=-100123, user_id=7, right="can_send_welcome_messages", account="acct"
    )

    assert "promote_admin" in result
    assert client.sent_rights() is None


@pytest.mark.asyncio
async def test_reading_rights_reports_which_are_granted(wire):
    wire()

    result = _results(
        await lr.get_admin_rights_via_tdlib(chat_id=-100123, user_id=7, account="acct")
    )

    assert "@type" not in result["rights"], "the TL type leaked into the reported rights"
    assert "can_send_welcome_messages" in result["rights"]
    assert "can_pin_messages" in result["granted"]
    assert "can_edit_messages" not in result["granted"]


# --------------------------------------------------------------------------
# Finishing what the MTProto layer could not carry
# --------------------------------------------------------------------------
#
# `edit_admin_rights` sends every right in one `channels.editAdmin` and Telegram
# silently drops the ones newer than the announced layer. It cannot announce a
# later one: Telegram accepts `invokeWithLayer` only as a connection's FIRST
# request (measured -- `ConnectionLayerInvalidError`), so there is no per-call
# escape. TDLib finishes the remainder.
#
# The property under test throughout is that every right the caller asked for
# ends up in exactly one bucket. A name that reached none of them is the
# original silent drop wearing a longer message.


@pytest.mark.asyncio
async def test_a_right_the_layer_dropped_is_finished_over_tdlib(wire):
    client = wire()

    outcome = await lr.finish_later_rights("acct", -1002677705573, 42, ["manage_welcome_messages"])

    assert outcome["delivered"] == ["manage_welcome_messages"]
    assert outcome["failed"] == {}
    assert client.sent_rights()["can_send_welcome_messages"] is True


@pytest.mark.asyncio
async def test_a_right_with_no_tdlib_equivalent_is_not_guessed_at(wire):
    """`manage_linked_peers` has no unambiguous TDLib field. Picking the
    closest-looking one would revoke a right silently, which is the failure this
    module was built to avoid, so it is reported instead of mapped."""
    client = wire()

    outcome = await lr.finish_later_rights("acct", -1002677705573, 42, ["manage_linked_peers"])

    assert outcome["unmappable"] == ["manage_linked_peers"]
    assert outcome["delivered"] == []
    assert client.requests == [], "a right with no known mapping still reached Telegram"


@pytest.mark.asyncio
async def test_a_missing_tdlib_login_still_names_every_right_it_could_not_finish(monkeypatch):
    """The bucket-completeness property at its most tempting failure point:
    returning early here would drop the names entirely, so the caller would be
    told the rights were fine."""
    monkeypatch.setattr(lr, "account_label", lambda account=None: "acct")

    async def _refuse(label):
        raise lr.NotSignedIn("acct", "authorizationStateWaitPassword")

    monkeypatch.setattr(lr, "secret_client", _refuse)

    outcome = await lr.finish_later_rights(
        "acct", -1002677705573, 42, ["manage_welcome_messages", "manage_linked_peers"]
    )

    assert outcome["delivered"] == []
    assert "manage_welcome_messages" in outcome["failed"]
    # The real exception, not a stand-in, so this also pins that what reaches
    # the caller names the launcher rather than a Python command they have said
    # they do not want to run.
    assert "option 6" in outcome["failed"]["manage_welcome_messages"], "the remedy was not named"
    assert outcome["unmappable"] == ["manage_linked_peers"]


@pytest.mark.asyncio
async def test_telegram_declining_the_right_is_reported_rather_than_counted_as_delivered(wire):
    """A write TDLib accepts but Telegram does not honour must not be reported
    as delivered -- that is the same lie as the layer drop, one layer down."""
    wire(obey_writes=False)

    outcome = await lr.finish_later_rights("acct", -1002677705573, 42, ["manage_welcome_messages"])

    assert outcome["delivered"] == []
    assert "manage_welcome_messages" in outcome["failed"]
