"""One account's data must not land where another account can find it.

The directory is named by the account's NUMERIC id rather than its label, and
that is the whole point: a label is a name in `.env` that can be renamed,
removed and re-added, or handed to a different account. Keying by the label
fails silently - the work is not lost, it is simply somewhere the same account
no longer looks, or worse, somewhere a DIFFERENT account now looks.

**None of these tests clears the cache to make its point.** The first version of
this file did, and that hid a real leak: the cache was keyed by label, so a
second account borrowing a label was handed the first one's directory, and the
test only passed because it reset the cache in between. A test that has to
disarm the mechanism it is testing is proving nothing.
"""

import pytest

from telegram_mcp import personal


class _Client:
    """A client that knows who it is, and counts how often it is asked."""

    def __init__(self, numeric):
        self.numeric = numeric
        self.asked = 0

    async def get_me(self):
        self.asked += 1
        return type("Me", (), {"id": self.numeric})()


@pytest.fixture(autouse=True)
def _clean_cache():
    personal.forget_cached_ids()
    yield
    personal.forget_cached_ids()


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(personal, "project_root", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_the_directory_is_named_by_the_numeric_id(temp_root):
    directory = await personal.personal_dir(_Client(5899781975))

    assert directory == temp_root / ".personal" / "5899781975"
    assert directory.is_dir()


@pytest.mark.asyncio
async def test_two_accounts_never_share_a_directory(temp_root):
    """The leak this module exists to prevent, asserted with the cache live."""
    one = await personal.personal_dir(_Client(111))
    two = await personal.personal_dir(_Client(222))

    assert one != two
    assert (one.name, two.name) == ("111", "222")
    assert one.parent == two.parent


@pytest.mark.asyncio
async def test_a_second_account_does_not_inherit_the_first_ones_directory(temp_root):
    """The regression. Two accounts, one after the other, nothing reset between
    them: with a label-keyed cache the second was handed `111` and the two
    accounts shared a folder."""
    first = await personal.personal_dir(_Client(111))
    (first / "private.json").write_text("{}", encoding="utf-8")

    second = await personal.personal_dir(_Client(222))

    assert second.name == "222"
    assert not (second / "private.json").exists()


@pytest.mark.asyncio
async def test_the_same_account_finds_its_work_again_after_a_restart(temp_root):
    """A restart is a NEW client object for the same account - and in `.env` the
    label may have changed, or the account may have been removed and re-added.
    The id is what makes it the same person."""
    before = await personal.personal_dir(_Client(5899781975))
    (before / "work.json").write_text("{}", encoding="utf-8")

    after = await personal.personal_dir(_Client(5899781975))

    assert after == before
    assert (after / "work.json").exists()


@pytest.mark.asyncio
async def test_the_label_cannot_be_passed_at_all(temp_root):
    """Not merely ignored - absent. A parameter that cannot be supplied cannot be
    supplied wrongly, which is why the fix removed it rather than documenting
    it as safe."""
    import inspect

    assert "label" not in inspect.signature(personal.personal_dir).parameters


@pytest.mark.asyncio
async def test_the_id_is_asked_for_once_per_client(temp_root):
    client = _Client(5899781975)

    for _ in range(4):
        await personal.personal_dir(client)

    assert client.asked == 1, "get_me() costs a round trip on a cold session"


@pytest.mark.asyncio
async def test_the_folder_explains_itself_without_the_source(temp_root):
    """Someone who finds `.personal/` in a checkout should not have to read the
    code to learn what it is or why the name is a number."""
    await personal.personal_dir(_Client(5899781975))

    note = (temp_root / ".personal" / "README.md").read_text(encoding="utf-8")

    assert "numeric user id" in note and "git-ignored" in note


def test_the_ignore_rule_ships_with_the_project():
    """The rule has to hold for anyone who clones this, not only here."""
    ignore = (personal.project_root() / ".gitignore").read_text(encoding="utf-8")

    assert f"/{personal.DIRECTORY_NAME}/" in ignore
