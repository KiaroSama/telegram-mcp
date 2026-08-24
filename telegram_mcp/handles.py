"""Holding the object a check was made about, instead of the name it wore.

A filesystem check answers a question about whatever a name reaches *at that
instant*. Every later step -- Telethon reopening a path to upload it, ``open()``
to write, ``os.replace`` to install, ``unlink`` to clean up -- asks the same name
again, and can get a different answer. Between the two lookups a parent
directory can be exchanged, a final component replaced, a symlink dropped in.
The gate then reports on one object while the work happens to another.

So a check ends here holding a descriptor, and everything after it goes through
that descriptor:

* :class:`DirHandle` is an *open directory*. Children are created, opened,
  installed and removed relative to it.
* :func:`open_verified_file` returns a file that was opened without following a
  link, confirmed to be the same object the name still names, confirmed regular,
  and measured with ``fstat`` -- so the size cap is the handle's size and not a
  number read off a name.
* :func:`open_allowed_directory` is the same idea for the roots verdict: resolve,
  judge, open, then prove the thing opened is the thing judged.

**Platform.** POSIX has ``openat``: ``os.supports_dir_fd`` is populated, and
``O_NOFOLLOW``/``O_DIRECTORY`` exist, so a name can be resolved *by the kernel*
relative to a held descriptor and never re-walked from the root. Windows has
none of the three -- ``os.open`` on a directory fails outright -- but it does
report real object identity (``st_dev`` is the volume serial, ``st_ino`` the file
index) and a reparse-point flag. The Windows branch therefore keeps the
directory's identity from the moment it was authorised and re-proves it
immediately before every operation, refusing the moment the name stops naming
that object. That is a smaller window than a bare path, not a descriptor.

``ponytail:`` the Windows branch is identity re-verification, not a kernel
handle. A real ``CreateFileW`` directory handle with
``FILE_FLAG_BACKUP_SEMANTICS`` would close the residual window; it needs ctypes
and is worth it only if this server is ever run somewhere hostile to its own
state directory.

**The syscall seam.** Every primitive goes through :class:`SystemCalls` so the
decisions above can be driven deterministically from a test -- including the
races, which are otherwise timing-dependent, and the POSIX routing, which cannot
execute on a Windows host at all.
"""

import errno
import os
import secrets
import stat as stat_module
from pathlib import Path
from typing import Any, List, Optional, Tuple

# Windows marks junctions, symlinks and every other reparse point with this bit.
# `stat.S_ISLNK` is false for a directory junction, so the flag is the half of
# the link test that matters there.
_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# How many names near a caller's chosen one may be tried before giving up. Two
# saves in the same second collide on their own; a hundred is somebody being
# deliberate.
NAME_ATTEMPTS = 100


class UnsafeTarget(Exception):
    """The object behind a name is not the object that was authorised."""


class SystemCalls:
    """The filesystem primitives this module uses, in one replaceable object.

    Subclass and override to drive a decision from a test. The defaults are the
    real calls; ``dir_fd`` says whether this host can resolve a name relative to
    an open directory, which is what decides the whole strategy below.
    """

    dir_fd = bool(getattr(os, "supports_dir_fd", set()))
    O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
    O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
    O_BINARY = getattr(os, "O_BINARY", 0)
    O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return os.open(path, flags, mode)
        return os.open(path, flags, mode, dir_fd=dir_fd)

    def close(self, fd):
        os.close(fd)

    def fstat(self, fd):
        return os.fstat(fd)

    def lstat(self, path, *, dir_fd=None):
        if dir_fd is None:
            return os.lstat(path)
        return os.lstat(path, dir_fd=dir_fd)

    def mkdir(self, path, mode=0o700, *, dir_fd=None):
        if dir_fd is None:
            return os.mkdir(path, mode)
        return os.mkdir(path, mode, dir_fd=dir_fd)

    def rmdir(self, path, *, dir_fd=None):
        if dir_fd is None:
            return os.rmdir(path)
        return os.rmdir(path, dir_fd=dir_fd)

    def unlink(self, path, *, dir_fd=None):
        if dir_fd is None:
            return os.unlink(path)
        return os.unlink(path, dir_fd=dir_fd)

    def replace(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is None and dst_dir_fd is None:
            return os.replace(src, dst)
        return os.replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def scandir_names(self, path, *, dir_fd=None):
        if dir_fd is None:
            return os.listdir(path)
        return os.listdir(dir_fd)

    def fsync(self, fd):
        os.fsync(fd)

    def write(self, fd, data):
        return os.write(fd, data)


SYSTEM = SystemCalls()


# --- the pure decisions ------------------------------------------------------


def same_object(first: Any, second: Any) -> bool:
    """Whether two stat results describe one filesystem object.

    Inode 0 means the filesystem declines to identify the object, and an
    unanswerable identity question is a refusal: two anonymous objects must not
    compare equal just because neither could be named.
    """
    ino = getattr(first, "st_ino", 0)
    if not ino:
        return False
    return ino == getattr(second, "st_ino", 0) and first.st_dev == second.st_dev


def is_link(entry: Any) -> bool:
    """Whether a stat result is a symlink, junction or any other reparse point."""
    if stat_module.S_ISLNK(entry.st_mode):
        return True
    return bool(getattr(entry, "st_file_attributes", 0) & _REPARSE_POINT)


def _within(candidate: Path, roots: List[Path]) -> bool:
    for root in roots:
        root = Path(root).resolve()
        if candidate == root or root in candidate.parents:
            return True
    return False


# --- an open directory -------------------------------------------------------


class DirHandle:
    """A directory this process has open, and the only way to reach its children.

    On POSIX ``fd`` is a real descriptor and every child name is resolved by the
    kernel relative to it. On Windows ``fd`` is ``None``; ``identity`` is what was
    authorised and :meth:`verify` re-proves it before each operation.
    """

    def __init__(self, path: Path, fd: Optional[int], identity, calls: SystemCalls):
        self.path = Path(path)
        self.fd = fd
        self.identity = identity
        self._calls = calls
        self._closed = False

    # -- plumbing ------------------------------------------------------------

    def _at(self, name: str):
        """The name to pass, and the keyword that binds it to this directory."""
        if self.fd is not None:
            return name, {"dir_fd": self.fd}
        self.verify()
        return str(self.path / name), {}

    def verify(self) -> None:
        """Re-prove that this directory's name still names this directory.

        A no-op where a descriptor is held: the kernel is already answering about
        the object rather than the name.
        """
        if self.fd is not None or self._closed:
            return
        try:
            current = self._calls.lstat(str(self.path))
        except OSError as error:
            raise UnsafeTarget(f"the directory {self.path.name!r} is gone") from error
        if is_link(current) or not same_object(current, self.identity):
            raise UnsafeTarget(
                f"the directory {self.path.name!r} was replaced after it was authorised"
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.fd is not None:
            try:
                self._calls.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self) -> "DirHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- children ------------------------------------------------------------

    def child_stat(self, name: str):
        """``lstat`` a child *through* this directory, never following a link."""
        if self.fd is not None:
            return self._calls.lstat(name, dir_fd=self.fd)
        self.verify()
        return self._calls.lstat(str(self.path / name))

    def open_child(self, name: str) -> int:
        """Open a child read-only without following a link. Returns a descriptor."""
        target, where = self._at(name)
        flags = (
            os.O_RDONLY | self._calls.O_BINARY | self._calls.O_NOFOLLOW | self._calls.O_NOINHERIT
        )
        return self._calls.open(target, flags, **where)

    def create_exclusive(self, name: str, mode: int = 0o600) -> int:
        """Create a child that did not exist. Returns a writable descriptor.

        ``O_EXCL`` is the reservation: it fails if the name appeared between the
        look and the create, so the caller owns what it gets back instead of
        merely having seen the name free a moment ago.
        """
        target, where = self._at(name)
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | self._calls.O_BINARY
            | self._calls.O_NOFOLLOW
            | self._calls.O_NOINHERIT
        )
        return self._calls.open(target, flags, mode, **where)

    def reserve_free_name(self, stem: str, suffix: str) -> Optional[str]:
        """Create and return an unused child name near ``stem+suffix``.

        The default names two tools build are only second-precise, so two saves
        in one second collide without anyone being hostile. Returns ``None`` when
        every candidate is taken.
        """
        for attempt in range(NAME_ATTEMPTS):
            name = f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt}{suffix}"
            try:
                self._calls.close(self.create_exclusive(name))
                return name
            except FileExistsError:
                continue
        return None

    def make_private_subdirectory(self, prefix: str) -> Tuple[str, "DirHandle"]:
        """Create a directory only this call knows about, and open it.

        ``mkdir`` is the reservation -- it fails on a name that exists -- and the
        result is opened straight afterwards so the work goes through a handle
        rather than through the name that was just created.
        """
        for _ in range(NAME_ATTEMPTS):
            name = f"{prefix}{secrets.token_hex(8)}"
            target, where = self._at(name)
            try:
                self._calls.mkdir(target, 0o700, **where)
            except FileExistsError:
                continue
            return name, self.open_subdirectory(name)
        raise UnsafeTarget("could not create a private transfer directory")

    def open_subdirectory(self, name: str) -> "DirHandle":
        """Open a child directory, proving it is the directory just seen there."""
        named = self.child_stat(name)
        if is_link(named) or not stat_module.S_ISDIR(named.st_mode):
            raise UnsafeTarget(f"{name!r} is not a plain directory")
        if self.fd is None:
            self.verify()
            return DirHandle(self.path / name, None, named, self._calls)
        flags = os.O_RDONLY | self._calls.O_NOFOLLOW | self._calls.O_DIRECTORY
        fd = self._calls.open(name, flags, dir_fd=self.fd)
        opened = self._calls.fstat(fd)
        if not same_object(opened, named):
            self._calls.close(fd)
            raise UnsafeTarget(f"{name!r} was replaced while it was being opened")
        return DirHandle(self.path / name, fd, opened, self._calls)

    def unlink(self, name: str) -> None:
        target, where = self._at(name)
        self._calls.unlink(target, **where)

    def rmdir(self, name: str) -> None:
        target, where = self._at(name)
        self._calls.rmdir(target, **where)

    def remove_tree(self) -> None:
        """Empty this directory through its own handle.

        One level deep, because that is all a transfer directory ever has, and
        because recursion by name is exactly the re-resolution this class exists
        to avoid. Refuses outright when the directory this handle was opened for
        is no longer the one its name reaches -- deleting the contents of
        somebody else's directory is the failure mode, not a tidy-up.
        """
        self.verify()
        if self.fd is not None:
            names = self._calls.scandir_names(str(self.path), dir_fd=self.fd)
        else:
            names = self._calls.scandir_names(str(self.path))
        for name in names:
            try:
                self.unlink(name)
            except (IsADirectoryError, PermissionError, OSError) as error:
                if getattr(error, "errno", None) not in (errno.EISDIR, errno.EPERM, errno.EACCES):
                    raise
                self.rmdir(name)

    def install(self, source: "DirHandle", source_name: str, name: str) -> None:
        """Give ``source_name`` its final name in *this* directory, atomically.

        Both ends are bound to a held directory, so neither the origin nor the
        destination can be a name that started meaning something else.
        """
        self.verify()
        source.verify()
        if self.fd is not None and source.fd is not None:
            self._calls.replace(source_name, name, src_dir_fd=source.fd, dst_dir_fd=self.fd)
            return
        self._calls.replace(str(source.path / source_name), str(self.path / name))

    def discard(self, name: str) -> None:
        """Best-effort removal of a child this call made. Never raises.

        Cleanup that throws replaces the failure worth reporting, and a directory
        that has stopped being the authorised one is a directory nothing here may
        delete from -- so a refusal to clean up is also a correct outcome.
        """
        try:
            self.unlink(name)
        except (OSError, UnsafeTarget):
            pass

    def sync_child(self, name: str) -> None:
        """Push a finished child's bytes to durable storage.

        Opened read-write because Windows' ``_commit`` refuses a read-only
        handle, and a file this package is about to install is one it owns.
        """
        target, where = self._at(name)
        flags = os.O_RDWR | self._calls.O_BINARY | self._calls.O_NOFOLLOW | self._calls.O_NOINHERIT
        fd = self._calls.open(target, flags, **where)
        try:
            self._calls.fsync(fd)
        finally:
            self._calls.close(fd)

    def write_file_durably(self, name: str, data: bytes) -> None:
        """Put ``data`` under ``name`` so a reader sees all of it or none of it.

        The bytes go to an unpredictable sibling first, are flushed to storage,
        and only then take ``name``. Writing into the destination directly is
        what left a short file wearing the finished file's name: a full disk does
        not fail at ``write()`` on a delayed-allocation filesystem, it fails at
        the flush, which a direct write never performs.
        """
        temporary = f".{name}.{secrets.token_hex(8)}.part"
        try:
            fd = self.create_exclusive(temporary)
            try:
                view = memoryview(data)
                while view:
                    view = view[self._calls.write(fd, view) :]
                self._calls.fsync(fd)
            finally:
                self._calls.close(fd)
            self.install(self, temporary, name)
            self.sync()
        finally:
            # Whatever happened -- including the CancelledError no `except` here
            # would ever see -- the part file does not outlive the call. After a
            # successful install its name is already gone.
            self.discard(temporary)

    def sync(self) -> None:
        """Make this directory's entries survive a crash, where that is a thing.

        ``os.replace`` publishes a name, and the name is metadata: without this
        the rename can still be in the log when the machine stops, and the file
        the caller was told about is not there when it comes back. Windows
        exposes no directory handle to sync and orders this metadata itself.
        """
        if self.fd is None:
            return
        try:
            self._calls.fsync(self.fd)
        except OSError:
            pass


# --- getting one -------------------------------------------------------------


def open_allowed_directory(path, roots: List[Path], *, calls: SystemCalls = SYSTEM) -> DirHandle:
    """Judge a directory against the roots, then open the directory that was judged.

    The order is the whole point. The verdict is taken about a resolved path; the
    open is done without following a link; the object that came back is then
    reconciled with what the name reaches *now*, and the name is resolved a
    second time and re-judged. A swap before the verdict is caught by the
    verdict, a swap after it by the reconciliation, and a swap after the open
    does not matter because the descriptor is already held.
    """
    resolved = Path(path).resolve(strict=False)
    if not _within(resolved, roots):
        raise UnsafeTarget("the directory is outside the allowed roots")

    if calls.dir_fd:
        flags = os.O_RDONLY | calls.O_NOFOLLOW | calls.O_DIRECTORY
        fd = calls.open(str(resolved), flags)
        try:
            opened = calls.fstat(fd)
            named = calls.lstat(str(resolved))
            if is_link(named) or not same_object(opened, named):
                raise UnsafeTarget("the directory was replaced while it was being opened")
            if not stat_module.S_ISDIR(opened.st_mode):
                raise UnsafeTarget("the destination is not a directory")
            if Path(path).resolve(strict=False) != resolved or not _within(resolved, roots):
                raise UnsafeTarget("the directory moved out of the allowed roots")
        except BaseException:
            calls.close(fd)
            raise
        return DirHandle(resolved, fd, opened, calls)

    # No openat here. Identity taken at authorisation time, re-proved before use.
    named = calls.lstat(str(resolved))
    if is_link(named):
        raise UnsafeTarget("the destination is a link, not a directory")
    if not stat_module.S_ISDIR(named.st_mode):
        raise UnsafeTarget("the destination is not a directory")
    if Path(path).resolve(strict=False) != resolved or not _within(resolved, roots):
        raise UnsafeTarget("the directory moved out of the allowed roots")
    confirm = calls.lstat(str(resolved))
    if not same_object(confirm, named):
        raise UnsafeTarget("the directory was replaced while it was being authorised")
    return DirHandle(resolved, None, named, calls)


class VerifiedFile:
    """A file this process holds open, plus what may be said about it.

    ``handle`` is what goes to Telethon: it accepts any seekable binary stream,
    reads ``name`` for the mime type and the filename attribute, and never
    reopens a path. ``name`` is deliberately the basename -- the operator's
    directory layout is not part of an upload.
    """

    __slots__ = ("handle", "name", "size", "path")

    def __init__(self, handle, name: str, size: int, path: Path):
        self.handle = handle
        self.name = name
        self.size = size
        self.path = path

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError:
            pass

    def __enter__(self) -> "VerifiedFile":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_verified_file(
    directory: DirHandle, name: str, *, max_bytes: Optional[int] = None
) -> VerifiedFile:
    """Open a child of ``directory`` and authorise the object that came back."""
    fd = directory.open_child(name)
    try:
        opened = directory._calls.fstat(fd)
        named = directory.child_stat(name)
        if is_link(named) or not same_object(opened, named):
            raise UnsafeTarget(f"{name!r} was replaced while it was being opened")
        if not stat_module.S_ISREG(opened.st_mode):
            raise UnsafeTarget(f"{name!r} is not a regular file")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise UnsafeTarget(
                f"the file is {opened.st_size} bytes, over the {max_bytes}-byte limit"
            )
    except BaseException:
        directory._calls.close(fd)
        raise
    handle = open(fd, "rb")
    try:
        handle.raw.name = name
    except (AttributeError, TypeError):  # pragma: no cover - a wrapper without a raw
        pass
    return VerifiedFile(handle, name, opened.st_size, directory.path / name)
