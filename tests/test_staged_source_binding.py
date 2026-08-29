"""The staged file is written by somebody else, so it has to be bound too.

``DirHandle.install`` was bound at one end only. The destination was proved --
the placeholder this call reserved -- while the SOURCE was opened by the name it
happened to have. A download is written into the staging directory by Telethon,
through a pathname, so between the size check and the publication that name can
be given to a different object, and the install published that one instead.

Nothing here is mocked or mid-call: the replacement is a real one, made at the
exact point the real flow leaves the staged file closed and unattended.
"""

import inspect
import os
from pathlib import Path

import pytest

from telegram_mcp import handles
from telegram_mcp.handles import SystemCalls, UnsafeTarget, open_verified_file
from telegram_mcp.owner_only import verify_owner_only


def _staged(root: Path, payload: bytes = b"the-real-download"):
    """A parent handle plus a staging directory holding one written file."""
    parent = handles.open_allowed_directory(root, [root])
    _name, staging = parent.make_private_subdirectory(".dl-")
    (Path(staging.path) / "part.bin").write_bytes(payload)
    return parent, staging


# --- the staging directory is private before anything writes into it --------


def test_a_staging_directory_is_owner_only_from_the_moment_it_exists(tmp_path):
    """``mkdir(0o700)`` is the whole protection on POSIX and NOTHING on Windows,
    where the mode argument is ignored. Until this was fixed a download staged
    into a directory that inherited whatever its parent granted, and stayed
    readable for as long as the transfer took."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as parent:
        _name, staging = parent.make_private_subdirectory(".dl-")
        try:
            assert verify_owner_only(Path(staging.path)), (
                "the transfer directory is not owner-only, so the bytes staged in it "
                "are readable while they are being written"
            )
        finally:
            staging.close()


def test_a_staging_directory_that_cannot_be_made_private_is_not_used(tmp_path):
    """Fail closed. A warning here would mean the download proceeds anyway, into
    a directory whose permissions nobody established."""

    class _CannotRestrict(SystemCalls):
        def restrict_directory(self, path):
            return False

    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root], calls=_CannotRestrict()) as parent:
        with pytest.raises(UnsafeTarget, match="owner-only"):
            parent.make_private_subdirectory(".dl-")

        leftovers = [p for p in root.iterdir() if p.name.startswith(".dl-")]
        assert leftovers == [], f"a directory was left behind: {leftovers}"


# --- the object that was verified is the object that gets published ---------


def test_a_staged_source_replaced_after_verification_is_refused(tmp_path):
    """The acceptance case. Verify the staged file, replace it while nothing
    holds it -- which is exactly what the real flow does between the size check
    and the install -- and the publication must refuse."""
    root = tmp_path / "root"
    root.mkdir()
    parent, staging = _staged(root)

    try:
        with open_verified_file(staging, "part.bin") as fetched:
            staged_identity = fetched.identity
        assert staged_identity is not None, "nothing recorded which object passed"

        # A different object under the same name. `unlink` then `write_bytes`
        # rather than an overwrite: the point is a new inode / file index, which
        # is what an attacker substituting a file produces.
        (Path(staging.path) / "part.bin").unlink()
        (Path(staging.path) / "part.bin").write_bytes(b"planted")

        reserved = parent.reserve_free_name("out", ".bin")
        with pytest.raises(UnsafeTarget, match="no longer the object"):
            parent.install(staging, "part.bin", reserved, expect_source=staged_identity)

        assert (root / reserved).read_bytes() != b"planted", "the replacement was published"
        # And the reservation survives the refusal. Checking the source after
        # deleting the placeholder spent the caller's name on a rejection.
        assert (root / reserved).exists(), "the refusal consumed the reserved name"
    finally:
        staging.remove_tree()
        staging.remove_self()
        staging.close()
        parent.close()


def test_without_the_binding_the_replacement_is_published(tmp_path):
    """Why the argument is not optional in the download path.

    This is the seam exactly as it behaved before: same replacement, same
    install, no `expect_source` -- and the planted bytes become the final file.
    Kept as a characterisation of what the binding prevents, so nobody removes
    it believing the name check upstream was already enough.
    """
    root = tmp_path / "root"
    root.mkdir()
    parent, staging = _staged(root)

    try:
        with open_verified_file(staging, "part.bin") as fetched:
            assert fetched.size == len(b"the-real-download")

        (Path(staging.path) / "part.bin").unlink()
        (Path(staging.path) / "part.bin").write_bytes(b"planted")

        reserved = parent.reserve_free_name("out", ".bin")
        parent.install(staging, "part.bin", reserved)

        assert (root / reserved).read_bytes() == b"planted"
    finally:
        staging.remove_tree()
        staging.remove_self()
        staging.close()
        parent.close()


def test_an_unchanged_staged_source_still_installs(tmp_path):
    """Guard the guard: a check that refused everything would also pass the test
    above, and would break every real download."""
    root = tmp_path / "root"
    root.mkdir()
    parent, staging = _staged(root)

    try:
        with open_verified_file(staging, "part.bin") as fetched:
            staged_identity = fetched.identity

        reserved = parent.reserve_free_name("out", ".bin")
        parent.install(staging, "part.bin", reserved, expect_source=staged_identity)

        assert (root / reserved).read_bytes() == b"the-real-download"
    finally:
        staging.remove_tree()
        staging.remove_self()
        staging.close()
        parent.close()


def test_cleanup_after_a_refused_install_leaves_the_stranger_alone(tmp_path):
    """The refusal must not turn into a deletion of somebody else's file."""
    root = tmp_path / "root"
    root.mkdir()
    stranger = root / "precious.bin"
    stranger.write_bytes(b"not-ours")
    parent, staging = _staged(root)

    try:
        with open_verified_file(staging, "part.bin") as fetched:
            staged_identity = fetched.identity

        (Path(staging.path) / "part.bin").unlink()
        (Path(staging.path) / "part.bin").write_bytes(b"planted")

        with pytest.raises(UnsafeTarget):
            parent.install(staging, "part.bin", "precious.bin", expect_source=staged_identity)

        assert stranger.read_bytes() == b"not-ours"
    finally:
        staging.remove_tree()
        staging.remove_self()
        staging.close()
        parent.close()


def test_the_download_path_passes_the_identity_it_verified():
    """The binding is only worth anything where the real caller uses it."""
    source = Path("telegram_mcp/tools/media.py").read_text(encoding="utf-8")

    assert "staged = fetched.identity" in source
    assert "expect_source=staged" in source


@pytest.mark.skipif(os.name != "nt", reason="the Win32 branch reads the identity off the handle")
def test_on_windows_the_identity_is_read_off_the_handle_that_renames():
    """Not a stat of the NAME followed by a rename of the name: the same open
    handle is measured and then moved, so there is no window between them. And
    the measurement comes before anything is deleted, or a refusal would spend
    the caller's reserved name.

    Read off the function rather than a character slice of the file - the
    previous version broke the moment a comment above it grew.
    """
    body = inspect.getsource(handles.DirHandle.install)

    measured = body.index("self._calls.fstat(moving)")
    removed = body.index("self._remove_object(name, expect)")
    renamed = body.index("rename_object(moving")

    assert measured < removed, "the placeholder is deleted before the source is proved"
    assert measured < renamed, "the rename happens before the identity is checked"
