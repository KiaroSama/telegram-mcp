"""An alias belongs to one login, all the way through a decorated tool.

`tests/test_aliases.py` proves the store and the matcher scope by account when
they are *handed* one. This file proves the callers hand them one: the decorator
that substitutes an id before a tool body runs, the resolver every tool without
that decorator goes through, and the three alias tools themselves.

That distinction is the whole point. Chat ids are unique only within a login, so
a `work` alias resolved while `personal` is the active account is a plausible id
that names a different person there - or nobody - and nothing downstream can
tell. Helper-level tests passed while exactly that was happening end to end.

No network: fake clients record what they were asked to send.
"""

import json
from types import SimpleNamespace

import pytest

from telegram_mcp import aliases, connection, runtime
from telegram_mcp.tools import contacts, messages


@pytest.fixture(autouse=True)
def _tmp_aliases(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    monkeypatch.delenv("TELEGRAM_CONTACT_FUZZY", raising=False)
    yield


class _Client:
    """Records what a tool asked it to send, and nothing else."""

    def __init__(self, label):
        self.label = label
        self.sent = []

    async def send_message(self, entity, message, parse_mode=None):
        self.sent.append((entity, message))
        return SimpleNamespace(id=1)


@pytest.fixture
def logins(monkeypatch):
    """Configure N accounts and route every tool at their fake clients."""

    def configure(*labels):
        clients = {label: _Client(label) for label in labels}
        monkeypatch.setattr(connection, "clients", clients)

        async def _resolve(identifier, client=None, account=None):
            return SimpleNamespace(id=identifier)

        def _get_client(account=None):
            # Same contract as the real one: an omitted label is the sole login.
            if account is None:
                return next(iter(clients.values()))
            return clients[account]

        for module in (messages, contacts):
            monkeypatch.setattr(module, "get_client", _get_client)
            monkeypatch.setattr(module, "resolve_entity", _resolve)
        return clients

    return configure


def _save(alias, chat_id, account):
    runtime.save_aliases({alias: {"id": chat_id, "name": None, "account": account}})


# --- the decorated path: @validate_id substitutes the id before the body runs ---


@pytest.mark.asyncio
async def test_a_work_alias_never_resolves_inside_a_personal_send(logins):
    """The audit's reproduction: `send_message(chat_id="андрей", account="personal")`
    delivered to the id saved on `work`."""
    clients = logins("work", "personal")
    _save("андрей", 12345, "work")

    result = await messages.send_message(chat_id="андрей", message="hi", account="personal")

    assert clients["personal"].sent == [], "sent to another login's contact"
    assert clients["work"].sent == []
    assert json.loads(result)["nothing_sent"] is True


@pytest.mark.asyncio
async def test_the_owning_login_still_resolves_it(logins):
    clients = logins("work", "personal")
    _save("андрей", 12345, "work")

    await messages.send_message(chat_id="андрей", message="hi", account="work")

    assert [entity.id for entity, _ in clients["work"].sent] == [12345]


@pytest.mark.asyncio
async def test_the_ask_payload_never_offers_another_login_s_contacts(logins, monkeypatch):
    """The candidates are what the agent reads out for confirmation. A name from a
    login the user is not on is how the wrong person gets picked."""
    monkeypatch.setenv("TELEGRAM_CONTACT_FUZZY", "1")
    logins("work", "personal")
    runtime.save_aliases(
        {
            "андрей бекендер": {"id": 111, "name": "Work Andrey", "account": "work"},
            "борис фронтендер": {"id": 222, "name": "Home Boris", "account": "personal"},
        }
    )

    result = await messages.send_message(chat_id="андрей", message="hi", account="personal")

    payload = json.loads(result)
    assert payload["candidates"] == []
    assert payload["known_aliases"] == ["борис фронтендер"]


# --- the resolver path: tools without @validate_id ------------------------------


@pytest.mark.asyncio
async def test_resolve_entity_scopes_by_the_client_it_was_given(monkeypatch):
    """`_resolve` gets a client, not a label. Reverse-looking it up is what makes
    every tool that skipped @validate_id account-aware without touching it."""
    work, personal = object(), object()
    monkeypatch.setattr(connection, "clients", {"work": work, "personal": personal})
    _save("андрей", 12345, "work")

    seen = []

    async def _retries(getter, identifier, client, label, try_marked=True):
        seen.append(identifier)
        if not isinstance(identifier, int):
            raise ValueError("no such peer")  # what Telethon does with free text
        return SimpleNamespace(id=identifier)

    monkeypatch.setattr(runtime, "_resolve_with_retries", _retries)

    with pytest.raises(runtime.AliasNeedsUser):
        await runtime.resolve_entity("андрей", personal)
    assert seen == ["андрей"], "another login's id was used as the peer"

    entity = await runtime.resolve_entity("андрей", work)
    assert entity.id == 12345


@pytest.mark.asyncio
async def test_an_explicit_account_beats_the_client_lookup(monkeypatch):
    """A fake client is in no registry, so the reverse lookup finds nothing. The
    caller's own label has to win, or every tool test silently loses scoping."""
    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})
    _save("андрей", 12345, "work")

    async def _retries(getter, identifier, client, label, try_marked=True):
        return SimpleNamespace(id=identifier)

    monkeypatch.setattr(runtime, "_resolve_with_retries", _retries)

    entity = await runtime.resolve_entity("андрей", object(), account="work")

    assert entity.id == 12345


# --- storage is keyed by (account, alias) ---------------------------------------


def test_two_logins_can_use_the_same_nickname(monkeypatch):
    """A flat {alias: record} file made the second save delete the first, so one
    of the two people became unreachable by the only name their owner uses."""
    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})
    _save("мама", 111, "work")
    runtime.update_aliases(
        lambda rows: rows.__setitem__(
            ("personal", "мама"), {"id": 222, "name": None, "account": "personal"}
        )
    )

    assert runtime.apply_alias("мама", account="work") == 111
    assert runtime.apply_alias("мама", account="personal") == 222


# --- legacy rows: deterministic where they can be, refused where they cannot ----


def test_a_legacy_row_resolves_and_migrates_under_one_login(monkeypatch):
    """With a single login there is only one account it can have been saved on."""
    monkeypatch.setattr(connection, "clients", {"work": object()})
    runtime.save_aliases({"андрей": 12345})

    assert runtime.apply_alias("андрей") == 12345
    assert runtime.apply_alias("андрей", account="work") == 12345

    runtime.update_aliases(lambda rows: None)  # any write migrates what it loaded
    assert runtime.load_aliases() == {
        ("work", "андрей"): {"id": 12345, "name": None, "account": "work"}
    }


def test_a_legacy_row_never_resolves_under_several_logins(monkeypatch):
    """Which login saved it is unknowable, and guessing picks a recipient."""
    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})
    runtime.save_aliases({"андрей": 12345})

    assert runtime.apply_alias("андрей", account="work") == "андрей"
    assert runtime.apply_alias("андрей", account="personal") == "андрей"


def test_an_ambiguous_legacy_row_is_offered_for_confirmation(monkeypatch):
    """Refusing it silently would look identical to "never saved", and the user
    would be asked to identify someone they already named."""
    monkeypatch.setattr(connection, "clients", {"work": object(), "personal": object()})
    runtime.save_aliases({"андрей": 12345})

    payload = json.loads(runtime.alias_ask_payload("андрей", account="work"))

    assert payload["candidates"] == [
        {"alias": "андрей", "id": 12345, "name": None, "needs_migration": True}
    ]


# --- the alias tools themselves --------------------------------------------------


@pytest.mark.asyncio
async def test_saving_without_an_account_uses_the_real_sole_label(logins, monkeypatch):
    """One login called `work` stored the invented label `default`, and every
    later lookup on `work` missed its own row."""
    logins("work")
    monkeypatch.setattr(contacts, "get_marked_id", lambda entity: 999)
    monkeypatch.setattr(contacts, "format_entity", lambda entity: {"name": "Andrey"})

    await contacts.set_contact_alias(alias="андрей", chat_id="@andrey")

    assert runtime.load_aliases() == {
        ("work", "андрей"): {"id": 999, "name": "Andrey", "account": "work"}
    }


@pytest.mark.asyncio
async def test_listing_shows_only_the_asking_login(logins):
    logins("work", "personal")
    _save("андрей", 111, "work")
    _save("борис", 222, "personal")

    listed = json.loads(await contacts.list_contact_aliases(account="personal"))["results"]

    assert [row["id"] for row in listed] == [222]


@pytest.mark.asyncio
async def test_deleting_cannot_reach_another_login_s_alias(logins):
    logins("work", "personal")
    _save("андрей", 111, "work")

    result = await contacts.delete_contact_alias(alias="андрей", account="personal")

    assert "not found" in result
    assert runtime.apply_alias("андрей", account="work") == 111


@pytest.mark.asyncio
async def test_deleting_removes_the_asking_login_s_row_only(logins):
    logins("work", "personal")
    _save("мама", 111, "work")
    runtime.update_aliases(
        lambda rows: rows.__setitem__(
            ("personal", "мама"), {"id": 222, "name": None, "account": "personal"}
        )
    )

    await contacts.delete_contact_alias(alias="мама", account="personal")

    assert runtime.apply_alias("мама", account="work") == 111
    assert runtime.apply_alias("мама", account="personal") == "мама"


def test_the_account_label_is_matched_case_insensitively(monkeypatch):
    """`get_client` lowercases before it looks a label up, so the store has to
    agree or `account=Work` resolves nothing it saved."""
    monkeypatch.setattr(connection, "clients", {"work": object()})
    _save("андрей", 12345, "work")

    assert runtime.apply_alias("андрей", account="Work") == 12345
    assert runtime.apply_alias("андрей", account=" WORK ") == 12345


def test_aliases_module_still_works_without_a_client_layer(monkeypatch):
    """`aliases` depends on nothing else in the package by design; the sole-label
    lookup must not turn an unconfigured server into an alias crash."""
    monkeypatch.setattr(connection, "clients", {})

    _save("андрей", 12345, "work")

    assert aliases.sole_account_label() is None
    assert aliases.apply_alias("андрей") == "андрей"
