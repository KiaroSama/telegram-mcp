"""Privacy settings: the wire type, and the policy nobody asked for.

`account.setPrivacy` REPLACES the whole rule set for a key -- there is no patch
form -- so a setter that omits the base policy is choosing one. It used to
choose `InputPrivacyValueAllowAll` whenever no allow-list was supplied, which
for `phone`, `status` and `profile_photo` means "show it to everyone".

The exception lists have their own bug: `InputPrivacyValue*Users` takes a vector
of `InputUser`, and a resolved `User` is not one.

No network: a fake client records the TL requests it was handed.
"""

from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    InputPrivacyValueAllowAll,
    InputPrivacyValueAllowContacts,
    InputPrivacyValueAllowUsers,
    InputPrivacyValueDisallowAll,
    InputPrivacyValueDisallowUsers,
    InputUser,
    PrivacyValueAllowContacts,
    PrivacyValueDisallowUsers,
    User,
)

from telegram_mcp.tools import profile as mod


class _Client:
    def __init__(self, rules=()):
        self.requests = []
        self.rules = list(rules)

    async def __call__(self, request):
        self.requests.append(request)
        if type(request).__name__ == "GetPrivacyRequest":
            return SimpleNamespace(rules=self.rules, users=[], chats=[])
        return SimpleNamespace(rules=self.rules)

    def sent(self, name):
        return next((r for r in self.requests if type(r).__name__ == name), None)


@pytest.fixture
def _wire(monkeypatch):
    def wire(client=None, resolvable=(5, 6)):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _ensure(_client):
            return None

        async def _resolve(user_id, _client):
            if user_id not in resolvable:
                raise ValueError(f"no such user {user_id}")
            return User(id=user_id, access_hash=1000 + user_id)

        monkeypatch.setattr(mod, "ensure_connected", _ensure)
        monkeypatch.setattr(mod, "resolve_entity", _resolve)
        return client

    return wire


@pytest.mark.asyncio
async def test_an_omitted_allow_list_no_longer_means_show_it_to_everyone(_wire):
    """The old default turned "restrict my phone number to these people" with a
    typo'd argument name into "publish my phone number"."""
    client = _wire()

    result = await mod.set_privacy_settings("phone", account="a")

    assert "base_policy" in result
    assert client.sent("SetPrivacyRequest") is None, "a privacy change was sent anyway"


@pytest.mark.asyncio
async def test_the_exception_list_goes_out_as_input_users(_wire):
    """A `User` inside an InputPrivacyValue* is the wrong wire type."""
    client = _wire()

    await mod.set_privacy_settings(
        "status", base_policy="contacts", allow_users=[5], disallow_users=[6], account="a"
    )

    rules = client.sent("SetPrivacyRequest").rules
    for rule in rules:
        for user in getattr(rule, "users", []):
            assert isinstance(user, InputUser), f"{type(user).__name__} is not an InputUser"


@pytest.mark.asyncio
async def test_the_rules_are_a_complete_ordered_set_with_the_exceptions_first(_wire):
    """Telegram applies the rules in order, so a base policy ahead of its own
    exceptions would swallow them."""
    client = _wire()

    await mod.set_privacy_settings(
        "profile_photo", base_policy="nobody", allow_users=[5], disallow_users=[6], account="a"
    )

    kinds = [type(r) for r in client.sent("SetPrivacyRequest").rules]
    assert kinds == [
        InputPrivacyValueAllowUsers,
        InputPrivacyValueDisallowUsers,
        InputPrivacyValueDisallowAll,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,expected",
    [
        ("everyone", InputPrivacyValueAllowAll),
        ("contacts", InputPrivacyValueAllowContacts),
        ("nobody", InputPrivacyValueDisallowAll),
    ],
)
async def test_each_base_policy_maps_to_its_own_rule(_wire, policy, expected):
    client = _wire()

    await mod.set_privacy_settings("status", base_policy=policy, account="a")

    assert type(client.sent("SetPrivacyRequest").rules[-1]) is expected


@pytest.mark.asyncio
async def test_an_unknown_base_policy_is_refused(_wire):
    client = _wire()

    result = await mod.set_privacy_settings("status", base_policy="public", account="a")

    assert "everyone" in result and "contacts" in result and "nobody" in result
    assert client.sent("SetPrivacyRequest") is None


@pytest.mark.asyncio
async def test_a_user_that_cannot_be_resolved_stops_the_change(_wire):
    """Dropping the unresolved name and sending the rest silently applies a
    policy the caller did not ask for."""
    client = _wire(resolvable=(5,))

    result = await mod.set_privacy_settings(
        "status", base_policy="nobody", allow_users=[5, 999], account="a"
    )

    assert "999" in result
    assert client.sent("SetPrivacyRequest") is None


@pytest.mark.asyncio
async def test_a_user_named_on_both_lists_is_refused(_wire):
    client = _wire()

    result = await mod.set_privacy_settings(
        "status", base_policy="nobody", allow_users=[5], disallow_users=[5], account="a"
    )

    assert "both" in result.lower()
    assert client.sent("SetPrivacyRequest") is None


@pytest.mark.asyncio
async def test_reading_privacy_settings_answers_with_structure_not_a_repr(_wire):
    client = _wire(
        _Client(
            rules=[
                PrivacyValueDisallowUsers(users=[6]),
                PrivacyValueAllowContacts(),
            ]
        )
    )

    payload = await mod.get_privacy_settings(key="status", account="a")

    assert "PrivacyValue" not in payload, "the raw TLObject repr came back"
    assert '"contacts_allowed"' in payload
    assert "6" in payload
    assert client.sent("GetPrivacyRequest") is not None
