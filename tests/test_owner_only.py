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

from telegram_mcp.owner_only import restrict_to_owner_strict, verify_owner_only

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
