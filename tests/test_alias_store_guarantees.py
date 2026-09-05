"""Two guarantees the alias store makes about itself.

The first is stated in its own module docstring and was not kept: a caller
holding something sensitive "must treat False as a refusal, not a warning".
`save_aliases` called `restrict_to_owner` and discarded the answer. On Windows
that DACL write is the ONLY thing making the file private — `mkstemp`'s 0600 is a
POSIX guarantee — so a silent failure published a nickname-to-real-person map
under its real name, with no log line and no error.

The second is about cost, not safety. `load_aliases` re-read and re-parsed the
file on every call, and its own docstring says it "runs inside resolve_entity on
every call" — synchronously, on the event loop, once per candidate in
`match_aliases`. A cache is only correct if an edit made by another process is
still seen, which is what the third test here is for.
"""

import json

import pytest

# `restrict_to_owner` and `save_aliases` both moved into the store module;
# patching `aliases` would now miss the caller that reads the name.
from telegram_mcp import alias_store as alias_mod
from telegram_mcp.aliases import (
    AliasStoreUnprotected,
    _reset_alias_cache,
    load_aliases,
    save_aliases,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "aliases.json"
    monkeypatch.setenv(alias_mod._ALIASES_ENV, str(store))
    _reset_alias_cache()
    yield store
    _reset_alias_cache()


def test_a_write_that_cannot_be_made_private_is_refused(_isolated_store, monkeypatch):
    save_aliases({("acct", "mum"): {"id": 1, "name": "Mum", "account": "acct"}})
    before = _isolated_store.read_text(encoding="utf-8")

    monkeypatch.setattr(alias_mod, "restrict_to_owner", lambda path: False)

    with pytest.raises(AliasStoreUnprotected):
        save_aliases({("acct", "dad"): {"id": 2, "name": "Dad", "account": "acct"}})

    assert (
        _isolated_store.read_text(encoding="utf-8") == before
    ), "the store was replaced even though it could not be protected"


def test_a_refused_write_leaves_no_temporary_file_behind(_isolated_store, monkeypatch):
    save_aliases({("acct", "mum"): {"id": 1, "name": "Mum", "account": "acct"}})
    monkeypatch.setattr(alias_mod, "restrict_to_owner", lambda path: False)

    with pytest.raises(AliasStoreUnprotected):
        save_aliases({("acct", "dad"): {"id": 2, "name": "Dad", "account": "acct"}})

    leftovers = [p.name for p in _isolated_store.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"a readable temp file survived the refusal: {leftovers}"


def test_the_file_is_read_once_for_two_consecutive_loads(_isolated_store, monkeypatch):
    save_aliases({("acct", "mum"): {"id": 1, "name": "Mum", "account": "acct"}})
    _reset_alias_cache()

    reads = []
    original = type(_isolated_store).read_text

    def _counting_read_text(self, *args, **kwargs):
        reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(_isolated_store), "read_text", _counting_read_text)

    first = load_aliases()
    second = load_aliases()

    assert first == second
    assert len(reads) == 1, f"the store was read {len(reads)} times for two loads"


def test_an_edit_by_another_process_is_still_seen(_isolated_store):
    """What makes the cache safe rather than merely fast. Manage-Accounts.ps1, a
    second server and the session generator all write this file."""
    save_aliases({("acct", "mum"): {"id": 1, "name": "Mum", "account": "acct"}})
    assert ("acct", "mum") in load_aliases()

    # Written from outside, as another process would: different content, and
    # therefore a different size and mtime.
    _isolated_store.write_text(
        json.dumps({"dad": {"id": 2, "name": "Dad", "account": "acct"}}),
        encoding="utf-8",
    )

    reloaded = load_aliases()
    assert ("acct", "mum") not in reloaded, "a stale cached parse was served"
    # The on-disk key format is the store's own business; what this pins is that
    # the new CONTENT is seen, so it asserts on the record, not on the spelling.
    assert [record["id"] for record in reloaded.values()] == [2], reloaded


def test_the_map_handed_out_is_not_the_cached_one(_isolated_store):
    """`update_aliases` passes this map to migrate_legacy_rows and to the caller's
    mutate(), both of which change it in place."""
    save_aliases({("acct", "mum"): {"id": 1, "name": "Mum", "account": "acct"}})
    _reset_alias_cache()

    first = load_aliases()
    first[("acct", "mum")]["name"] = "TAMPERED"
    first[("acct", "injected")] = {"id": 99, "name": "nope", "account": "acct"}

    second = load_aliases()

    assert second[("acct", "mum")]["name"] == "Mum"
    assert ("acct", "injected") not in second
