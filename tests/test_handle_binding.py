"""Authorising an object, not the name that happened to point at it.

The roots gate used to end by handing back a *pathname*. Everything downstream --
Telethon reopening it to upload, ``open()`` to write, ``os.replace`` to install,
``unlink`` to clean up -- then resolved that name again, later, against whatever
the filesystem said at that moment. Any change in between (a parent directory
swapped, the final component replaced, a symlink dropped in) makes the second
lookup answer with a different object than the one that passed the checks.

So the checks now end holding an OPEN handle, and every later step goes through
that handle rather than through the name. These tests drive the three
replacements deterministically:

* **parent swapped** -- the directory the name resolved to is exchanged for a
  different real directory between the roots verdict and the open.
* **name swapped** -- the final component is exchanged after the directory
  handle exists but before the file behind it is opened.
* **symlink race** -- the object the name reaches is a link/reparse point rather
  than the regular file or directory that was authorised.

The swaps are real renames on a real filesystem, sequenced by a ``SystemCalls``
subclass that fires once at the exact call being raced. Nothing here sleeps and
nothing here is probabilistic.

``dir_fd``/``openat`` exists only on POSIX (``os.supports_dir_fd`` is empty on
Windows, and there is no ``O_NOFOLLOW`` or ``O_DIRECTORY`` either). The tests
that pin the POSIX branch therefore inject a ``SystemCalls`` that *reports*
openat support and records the keywords it is called with -- which proves the
decision logic routes through the descriptor, and does not prove the kernel call
itself. See ``test_the_posix_branch_passes_a_directory_descriptor``.
"""

import errno
import os
import stat as stat_module
from pathlib import Path

import pytest

from telegram_mcp import handles
from telegram_mcp.handles import DirHandle, SystemCalls, UnsafeTarget


class _RacingCalls(SystemCalls):
    """Real syscalls, with one swap fired the Nth time ``method`` is entered.

    The swap runs *before* the call it is attached to, which is what makes a
    race deterministic: the replacement is already in place when the primitive
    under test looks.

    ``pins = False`` because these tests drive the *decision* path -- the branch
    that has no way to hold the object and must therefore prove its identity by
    hand. On a host that can pin, the swap these tests perform is refused by the
    kernel outright and there is nothing left to decide; that guarantee is
    asserted against the real filesystem in ``tests/test_object_binding.py``.
    """

    pins = False
    # And no openat either, or this drives a different branch on each platform:
    # `SystemCalls.dir_fd` is empty on Windows and true on Linux, so the same
    # object routed through the fallback here and through the descriptor branch
    # there -- where the confirming lstat this class counts to does not exist.
    dir_fd = False

    def __init__(self, method: str, nth: int, swap):
        self._method = method
        self._left = nth
        self._swap = swap

    def _maybe(self, name):
        if name != self._method:
            return
        self._left -= 1
        if self._left == 0:
            self._swap()

    def open(self, *args, **kwargs):
        self._maybe("open")
        return super().open(*args, **kwargs)

    def lstat(self, *args, **kwargs):
        self._maybe("lstat")
        return super().lstat(*args, **kwargs)

    def fstat(self, *args, **kwargs):
        self._maybe("fstat")
        return super().fstat(*args, **kwargs)


def _swap_directory(live: Path, replacement: Path):
    """Exchange ``live`` for ``replacement``: a different inode under one name."""

    def swap():
        retired = live.with_name(live.name + ".retired")
        live.rename(retired)
        replacement.rename(live)

    return swap


# --- reading: the handle is what gets authorised ----------------------------


def test_a_parent_swapped_while_it_is_being_authorised_is_refused(tmp_path):
    """The verdict was taken about one directory; the handle must not come back
    holding another one that inherited its name a moment later."""
    root = tmp_path / "root"
    live = root / "box"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    # Fires just before the confirming look, so the replacement is already in
    # place when identity is checked.
    calls = _RacingCalls("lstat", 2, _swap_directory(live, decoy))

    with pytest.raises(UnsafeTarget):
        handles.open_allowed_directory(live, [root], calls=calls)


def test_a_parent_swapped_after_authorisation_stops_every_later_operation(tmp_path):
    """The write steps -- reserve, stage, install, clean up -- all name children
    of one directory. Once that name stops meaning the directory that was
    authorised, none of them may proceed.

    Three outcomes count, one per platform primitive. With ``openat`` the
    descriptor keeps naming the authorised object and the swap is irrelevant.
    With a Win32 pin the kernel refuses the swap. With neither, the identity
    check refuses every operation that follows it."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    parent = handles.open_allowed_directory(live, [root])
    try:
        if parent.fd is not None:  # pragma: no cover - POSIX only
            pytest.skip("with openat the descriptor keeps naming the authorised object")
        if parent.held is not None:
            with pytest.raises(OSError):
                _swap_directory(live, decoy)()
            assert handles.same_object(os.lstat(str(live)), parent.identity)
            return
        _swap_directory(live, decoy)()  # pragma: no cover - hosts with neither primitive
        with pytest.raises(UnsafeTarget):
            parent.reserve_free_name("a", ".bin")
        with pytest.raises(UnsafeTarget):
            parent.write_file_durably("a.bin", b"payload")
        assert list(live.iterdir()) == [], "a swapped-in directory was written to"
    finally:
        parent.close()


class _DivergingCalls(SystemCalls):
    """Real syscalls, except that the name answers a different object than the fd.

    That divergence is precisely the state a POSIX rename race produces between
    ``open()`` and the reconciling ``lstat``. It cannot be produced with real
    renames on this host -- Windows refuses to unlink or rename a file another
    handle has open, which is itself part of why the handle matters -- so the
    two answers are made to differ directly.
    """

    class _Elsewhere:
        def __init__(self, real):
            self.st_mode = real.st_mode
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino + 1
            self.st_size = real.st_size

    def lstat(self, path, *, dir_fd=None):
        real = super().lstat(path, dir_fd=dir_fd)
        if os.path.basename(str(path)) == "a.jpg":
            return self._Elsewhere(real)
        return real


def test_a_final_component_swapped_between_the_open_and_the_check_is_refused(tmp_path):
    """A descriptor is taken, and the name is then looked at once more to prove
    the descriptor got what the name means. When those two answers name different
    objects, something took the name in between, and it is a refusal."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"authorised")

    directory = handles.open_allowed_directory(root, [root])
    directory._calls = _DivergingCalls()
    try:
        with pytest.raises(UnsafeTarget):
            handles.open_verified_file(directory, "a.jpg")
    finally:
        directory.close()


def test_a_replacement_after_the_open_cannot_change_what_is_sent(tmp_path):
    """The point of holding the handle: once the file is open, the bytes that go
    to Telegram are the bytes that were authorised, whatever the name means by
    the time the upload runs. Windows will not even permit the replacement while
    the handle is held; POSIX will, and the handle keeps naming the old inode."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.jpg"
    target.write_bytes(b"authorised")

    directory = handles.open_allowed_directory(root, [root])
    try:
        source = handles.open_verified_file(directory, "a.jpg", max_bytes=64)
    finally:
        directory.close()
    try:
        try:
            target.unlink()
            (root / "a.jpg").write_bytes(b"substituted-and-much-longer")
        except PermissionError:
            # The open handle is the lock. Nothing to race.
            pass
        assert source.handle.read() == b"authorised"
        assert source.size == len(b"authorised")
    finally:
        source.close()


def test_a_symlinked_source_is_refused(tmp_path):
    """A link is not the file that was checked, whatever it points at today."""
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real.jpg"
    real.write_bytes(b"payload")
    link = root / "link.jpg"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not allow creating symlinks")

    directory = handles.open_allowed_directory(root, [root])
    try:
        with pytest.raises(UnsafeTarget):
            handles.open_verified_file(directory, "link.jpg")
    finally:
        directory.close()


def test_a_reparse_point_is_refused_without_needing_a_real_link(tmp_path):
    """The link check is a flag test, so it is provable where links are not."""
    real = os.lstat(tmp_path)
    assert not handles.is_link(real)

    class _Reparsed:
        st_mode = real.st_mode
        st_ino = real.st_ino
        st_dev = real.st_dev
        st_file_attributes = stat_module.FILE_ATTRIBUTE_REPARSE_POINT

    assert handles.is_link(_Reparsed())


def test_an_unidentifiable_object_is_refused_rather_than_trusted(tmp_path):
    """A filesystem that reports inode 0 cannot answer "is this the same object",
    and an unanswerable identity question is a refusal, not a pass."""

    class _Anonymous:
        st_mode = stat_module.S_IFREG
        st_ino = 0
        st_dev = 1

    assert not handles.same_object(_Anonymous(), _Anonymous())


def test_the_size_cap_is_measured_on_the_open_handle(tmp_path):
    """A cap read off the name is a cap on whatever wore the name at the time."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.bin").write_bytes(b"0123456789")

    directory = handles.open_allowed_directory(root, [root])
    try:
        with pytest.raises(UnsafeTarget):
            handles.open_verified_file(directory, "a.bin", max_bytes=4)
        source = handles.open_verified_file(directory, "a.bin", max_bytes=10)
    finally:
        directory.close()
    try:
        assert source.size == 10
        assert source.handle.read() == b"0123456789"
        # Telethon reads `.name` for the mime type and the filename attribute;
        # the operator's directory layout is not part of either.
        assert source.handle.name == "a.bin"
    finally:
        source.close()


# --- writing: install and cleanup go through the held directory -------------


def test_a_staging_directory_is_created_exclusively_and_removed_through_the_handle(
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    parent = handles.open_allowed_directory(root, [root])
    try:
        name, staging = parent.make_private_subdirectory(".dl-")
        assert (root / name).is_dir()
        (Path(staging.path) / "part.jpg").write_bytes(b"x")
        staging.remove_tree()
        # Through its own handle: the directory this call made, not the name it
        # was given. A pinning host will not let anything delete a directory it
        # is holding open, so removing it by name from the parent cannot work
        # while the staging handle is still live either.
        staging.remove_self()
        assert not (root / name).exists()
    finally:
        parent.close()


def test_cleanup_does_not_follow_a_name_that_now_points_somewhere_else(tmp_path):
    """The transfer directory is removed as the object it was created as. If its
    name has since been given to something else, that something else is not the
    thing this call made and must survive."""
    root = tmp_path / "root"
    root.mkdir()
    parent = handles.open_allowed_directory(root, [root])
    try:
        name, staging = parent.make_private_subdirectory(".dl-")
        (Path(staging.path) / "part").write_bytes(b"mine")

        # Somebody else's directory tries to take over the name.
        stolen = root / "stolen"
        stolen.mkdir()
        (stolen / "precious").write_bytes(b"not mine")
        # One branch per primitive, and the same guarantee out of all three:
        # `precious` is not this call's to delete.
        if staging.held is not None:
            # A Win32 handle: the kernel refuses to move the name at all.
            with pytest.raises(OSError):
                Path(staging.path).rename(root / (name + ".moved"))
            assert (stolen / "precious").read_bytes() == b"not mine"
            return
        Path(staging.path).rename(root / (name + ".moved"))
        stolen.rename(root / name)

        if staging.fd is not None:
            # A descriptor: the theft succeeds and is never consulted. Every
            # removal resolves relative to the object this call opened, so
            # there is nothing to refuse -- which is why asserting a refusal
            # here asserted the Windows mechanism rather than the guarantee.
            staging.remove_tree()
            assert not (
                root / (name + ".moved") / "part"
            ).exists(), "the staged file was not removed from the object that was opened"
        else:  # pragma: no cover - hosts with neither primitive
            with pytest.raises(UnsafeTarget):
                staging.remove_tree()
        assert (root / name / "precious").read_bytes() == b"not mine"
    finally:
        parent.close()


def test_the_install_refuses_when_the_destination_directory_was_replaced(tmp_path):
    """``os.replace`` by name would publish into whatever holds the name now."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    parent = handles.open_allowed_directory(live, [root])
    try:
        _name, staging = parent.make_private_subdirectory(".dl-")
        (Path(staging.path) / "part").write_bytes(b"payload")

        retired = root / "out.retired"
        if parent.held is not None:
            # The kernel will not let the destination be exchanged at all, which
            # is the same guarantee arrived at one layer lower down.
            with pytest.raises(OSError):
                live.rename(retired)
            return
        live.rename(retired)
        decoy.rename(live)

        if parent.fd is not None:
            # The descriptor answers about the directory that was authorised,
            # so the install lands in it under whatever name it wears now, and
            # never in the object that took the old one.
            parent.install(staging, "part", "final.bin")
            assert (retired / "final.bin").read_bytes() == b"payload"
        else:  # pragma: no cover - hosts with neither primitive
            with pytest.raises(UnsafeTarget):
                parent.install(staging, "part", "final.bin")
        assert not (live / "final.bin").exists()
    finally:
        parent.close()


def test_a_reserved_name_is_created_exclusively_and_never_overwrites(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.bin").write_bytes(b"already here")
    parent = handles.open_allowed_directory(root, [root])
    try:
        reserved = parent.reserve_free_name("a", ".bin")
        assert reserved == "a-1.bin"
        assert (root / "a.bin").read_bytes() == b"already here"
    finally:
        parent.close()


def test_durable_write_publishes_through_the_handle_and_leaves_no_partial(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    parent = handles.open_allowed_directory(root, [root])
    try:
        name = parent.reserve_free_name("out", ".bin")
        parent.write_file_durably(name, b"payload")
        assert (root / name).read_bytes() == b"payload"
        assert sorted(p.name for p in root.iterdir()) == [name]
    finally:
        parent.close()


# --- the POSIX branch, which this host cannot execute -----------------------


class _RecordingPosixCalls(SystemCalls):
    """Reports openat support and records how the module calls through it.

    This host has no ``dir_fd``: ``os.supports_dir_fd`` is empty, ``O_NOFOLLOW``
    and ``O_DIRECTORY`` do not exist, and ``os.open`` on a directory fails with
    EACCES. The real ``openat`` sequence therefore cannot run here. What this
    proves is the routing decision -- that with openat available the module opens
    a directory descriptor and passes it as ``dir_fd`` instead of re-resolving a
    path -- and nothing about the kernel's behaviour.
    """

    dir_fd = True
    O_NOFOLLOW = 0x20000
    O_DIRECTORY = 0x10000

    def __init__(self, tmp_path):
        self.calls = []
        self._tmp = tmp_path
        self._fds = {}
        self._next = 900

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        self.calls.append(("open", path, flags, dir_fd))
        if dir_fd is None:
            fd = self._next
            self._next += 1
            self._fds[fd] = Path(path)
            return fd
        fd = self._next
        self._next += 1
        self._fds[fd] = self._fds[dir_fd] / path
        return fd

    def close(self, fd):
        self._fds.pop(fd, None)

    def fstat(self, fd):
        return os.lstat(self._fds[fd])

    def lstat(self, path, *, dir_fd=None):
        self.calls.append(("lstat", path, dir_fd))
        if dir_fd is not None:
            path = self._fds[dir_fd] / path
        return os.lstat(path)


def test_the_posix_branch_passes_a_directory_descriptor(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"payload")
    calls = _RecordingPosixCalls(tmp_path)

    directory = handles.open_allowed_directory(root, [root], calls=calls)

    assert directory.fd is not None
    opens = [c for c in calls.calls if c[0] == "open"]
    assert opens, "the directory was never opened"
    flags = opens[0][2]
    assert flags & calls.O_NOFOLLOW, "the directory open followed links"
    assert flags & calls.O_DIRECTORY, "the open did not insist on a directory"

    calls.calls.clear()
    directory.child_stat("a.jpg")
    assert calls.calls == [
        ("lstat", "a.jpg", directory.fd)
    ], "a child was resolved by path instead of relative to the held descriptor"


class _LinkRefusingCalls(SystemCalls):
    """A kernel that refuses O_NOFOLLOW on a link, which Windows never does.

    ``O_NOFOLLOW`` is 0 on Windows, so the refusal below cannot be produced on
    the host this module was written on. Injecting it is what makes the
    translation provable on both platforms rather than only where it fires.
    """

    def __init__(self, number: int):
        self._errno = number

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        raise OSError(self._errno, os.strerror(self._errno), str(path))


@pytest.mark.parametrize("number", [errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)])
def test_a_kernel_link_refusal_is_reported_as_a_refusal_not_an_errno(tmp_path, number):
    """The caller catches ``UnsafeTarget``; an OSError took a different path.

    On Linux a symlinked source came back as ELOOP from the kernel before any
    check here ran, and the file gate reported it as 'not readable' rather than
    as a link. The refusal was correct and the reason was wrong.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"payload")

    directory = handles.open_allowed_directory(root, [root])
    directory._calls = _LinkRefusingCalls(number)
    try:
        with pytest.raises(UnsafeTarget, match="is a link"):
            directory.open_child("a.jpg")
    finally:
        directory.close()


def test_an_unrelated_open_failure_is_still_an_oserror(tmp_path):
    """Guard the guard: translating every errno would hide real failures."""
    root = tmp_path / "root"
    root.mkdir()

    directory = handles.open_allowed_directory(root, [root])
    directory._calls = _LinkRefusingCalls(errno.EACCES)
    try:
        with pytest.raises(OSError) as raised:
            directory.open_child("a.jpg")
        assert not isinstance(raised.value, UnsafeTarget)
    finally:
        directory.close()
