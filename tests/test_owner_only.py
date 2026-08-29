"""The owner-only contract, asserted against real OS state.

An audit proved the previous implementation reported success over a file that was
still world-readable, and the tests of the day could not have caught it: they
asserted the `icacls` argv, which described the intent rather than the result. So
everything here reads the protection back off the object.
"""

import os
import subprocess
import sys

import pytest

from telegram_mcp.owner_only import (
    _is_owner_only,
    restrict_handle_to_owner,
    restrict_to_owner_strict,
    verify_handle_owner_only,
    verify_owner_only,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows DACLs")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")

EVERYONE = "*S-1-1-0"
USERS = "*S-1-5-32-545"


def _dacl(path) -> str:
    return subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout


def _seed(path, principal, rights):
    result = subprocess.run(
        ["icacls", str(path), "/grant", f"{principal}:({rights})"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"could not seed {principal}: {result.stderr}"


@windows_only
@pytest.mark.parametrize("principal", [EVERYONE, USERS])
def test_an_explicit_entry_for_someone_else_is_removed(tmp_path, principal):
    """The exact case the old command missed. `/inheritance:r` drops INHERITED
    entries and `/grant:r` replaces only the named principal's own, so an explicit
    entry belonging to anyone else survived - and the helper still returned True.
    """
    target = tmp_path / "secret.txt"
    target.write_text("a session string, as far as this test is concerned", encoding="utf-8")
    _seed(target, principal, "R")
    assert not verify_owner_only(target), "the seeded entry was not visible; this proves nothing"

    assert restrict_to_owner_strict(target) is True

    listing = _dacl(target)
    assert "Everyone" not in listing, listing
    assert "S-1-1-0" not in listing, listing
    assert "S-1-5-32-545" not in listing, listing
    assert verify_owner_only(target), listing


@windows_only
def test_several_foreign_entries_go_together(tmp_path):
    """A DACL is a list, so removing one entry is not the job - replacing the list is."""
    target = tmp_path / "secret.txt"
    target.write_bytes(b"x")
    _seed(target, EVERYONE, "R")
    _seed(target, USERS, "M")

    assert restrict_to_owner_strict(target) is True
    assert verify_owner_only(target), _dacl(target)


@windows_only
def test_a_directory_is_protected_and_passes_it_on(tmp_path):
    """Files created inside a hardened directory have to start out hardened, or every
    writer has to remember to call this and one of them eventually will not."""
    directory = tmp_path / "state"
    directory.mkdir()
    _seed(directory, EVERYONE, "R")

    assert restrict_to_owner_strict(directory) is True
    assert verify_owner_only(directory), _dacl(directory)

    child = directory / "created-after.txt"
    child.write_text("inherits the protected DACL", encoding="utf-8")
    listing = _dacl(child)
    assert "Everyone" not in listing, listing
    assert "S-1-1-0" not in listing, listing


@windows_only
def test_verification_is_read_from_the_object_not_from_the_call(tmp_path):
    """`verify_owner_only` must answer about the file. Loosening the DACL after a
    successful hardening has to flip it back to False, or it is reporting history.
    """
    target = tmp_path / "secret.txt"
    target.write_bytes(b"x")
    assert restrict_to_owner_strict(target) is True
    assert verify_owner_only(target)

    _seed(target, EVERYONE, "R")

    assert not verify_owner_only(target), "a widened DACL still verified as private"


@windows_only
def test_a_missing_object_is_refused_rather_than_reported_private(tmp_path):
    """ "Nothing to protect" is not "protected". A caller about to write a session
    string needs the difference."""
    assert restrict_to_owner_strict(tmp_path / "does-not-exist") is False
    assert verify_owner_only(tmp_path / "does-not-exist") is False


@posix_only
def test_the_posix_path_verifies_the_mode_it_set(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_bytes(b"x")
    os.chmod(target, 0o644)
    assert not verify_owner_only(target)

    assert restrict_to_owner_strict(target) is True
    assert verify_owner_only(target)
    assert (os.stat(target).st_mode & 0o077) == 0

    os.chmod(target, 0o604)
    assert not verify_owner_only(target), "a group/other bit still verified as private"


# --- what "owner-only" has to mean, beyond the SID set ----------------------
#
# The check used to be `set(sids) == {mine}`, and three lists pass that while
# leaving the object open in ways the name does not suggest. `_is_owner_only` is
# a pure decision over what was read back, so every rule is asserted directly
# rather than by trying to construct four exotic DACLs on a real filesystem.

MINE = "S-1-5-21-1-2-3-1001"
FULL = 0x001F01FF
INHERIT = 0x01 | 0x02
ALLOW = 0x00
DENY = 0x01


def _entry(**overrides):
    """One ACE as `_read_dacl` reports it: owner-only, allow, full, inheritable."""
    entry = {"sid": MINE, "mask": FULL, "flags": INHERIT, "type": ALLOW}
    entry.update(overrides)
    return [entry]


def test_a_correct_list_is_accepted_for_a_directory_and_for_a_file():
    """Guard the guard: a check that refused everything would pass every case
    below and break every caller."""
    assert _is_owner_only(_entry(), protected=True, mine=MINE, inheritable=True)
    assert _is_owner_only(
        _entry(flags=0), protected=False, mine=MINE, inheritable=False
    ), "a file inherits its entry from the protected directory it was born in"


def test_a_directory_that_is_still_attached_to_inheritance_is_refused():
    """One entry, this account, full rights - and whatever the parent grants
    tomorrow arrives inside it, along with everything born there afterwards."""
    assert not _is_owner_only(_entry(), protected=False, mine=MINE, inheritable=True)


def test_a_directory_whose_entry_does_not_propagate_is_refused():
    """The directory is private and nothing born in it is. That is the exact
    hole: SQLite makes -wal and -shm files whenever it likes, long after any
    startup sweep, and they inherit nothing."""
    assert not _is_owner_only(_entry(flags=0), protected=True, mine=MINE, inheritable=True)
    assert not _is_owner_only(
        _entry(flags=0x01), protected=True, mine=MINE, inheritable=True
    ), "object inheritance alone leaves subdirectories out"


def test_a_single_deny_entry_is_not_owner_only():
    """It has a one-element SID set and it grants nobody anything, including the
    account that is supposed to own the file."""
    assert not _is_owner_only(_entry(type=DENY), protected=True, mine=MINE, inheritable=True)


def test_an_entry_that_does_not_grant_the_owner_full_control_is_refused():
    """READ_CONTROL alone also names this account and nothing else."""
    assert not _is_owner_only(_entry(mask=0x00020000), protected=True, mine=MINE, inheritable=True)


def test_somebody_else_is_still_refused():
    assert not _is_owner_only(
        _entry(sid="S-1-5-21-9-9-9-513"), protected=True, mine=MINE, inheritable=True
    )


def test_a_null_dacl_is_never_owner_only():
    """A NULL DACL grants everyone everything, so it must not read as an empty
    list of foreign identities."""
    assert not _is_owner_only(
        [{"sid": "<null-dacl>", "mask": 0, "flags": 0, "type": ALLOW}],
        protected=True,
        mine=MINE,
        inheritable=False,
    )


def test_an_unreadable_dacl_is_never_success():
    """None means the question could not be answered, which is not the same as
    'nobody else is on it'."""
    assert not _is_owner_only(None, protected=True, mine=MINE, inheritable=True)
    assert not _is_owner_only(_entry(), protected=None, mine=MINE, inheritable=True)
    assert not _is_owner_only(_entry(), protected=True, mine=None, inheritable=True)


# --- applying and verifying through one handle ------------------------------


@windows_only
def test_a_directory_is_hardened_and_proved_through_the_same_handle(tmp_path):
    """The pathname form resolves the name to apply the descriptor and again to
    read it back, so it can report success about an object it never wrote to.
    One handle has nothing left to re-resolve."""
    import msvcrt

    from telegram_mcp import win_handles

    target = tmp_path / "staging"
    target.mkdir()

    descriptor = win_handles.open_for_security(str(target))
    try:
        handle = msvcrt.get_osfhandle(descriptor)
        assert restrict_handle_to_owner(handle, inheritable=True)
        assert verify_handle_owner_only(handle, inheritable=True)
    finally:
        os.close(descriptor)

    # And the same object, asked by name, agrees.
    assert verify_owner_only(target)
    born = target / "born.txt"
    born.write_text("x", encoding="utf-8")
    assert verify_owner_only(born), "the inheritable entry did not reach a new file"


@windows_only
def test_the_transfer_directory_is_hardened_through_a_handle():
    """Where it matters, not merely where it is available."""
    import inspect

    from telegram_mcp.handles import SystemCalls

    body = inspect.getsource(SystemCalls.restrict_directory)

    assert "open_for_security" in body
    assert "restrict_handle_to_owner" in body
    assert "restrict_to_owner_strict" not in body


@posix_only
def test_the_handle_form_declines_where_there_is_no_handle_security():
    """POSIX answers this with mode bits through the path form; a handle-addressed
    descriptor does not exist there, and saying False is the honest answer."""
    assert not restrict_handle_to_owner(3, inheritable=True)
    assert not verify_handle_owner_only(3, inheritable=True)
