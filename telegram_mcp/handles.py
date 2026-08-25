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
relative to a held descriptor and never re-walked from the root.

Windows has none of the three -- ``os.open`` on a directory fails outright -- so
it holds the object a different way, through :mod:`telegram_mcp.win_handles`. A
``CreateFileW`` handle on the directory, opened with ``FILE_LIST_DIRECTORY`` and
a share mask that omits ``FILE_SHARE_DELETE``, makes the kernel refuse to rename
or delete that directory *or any directory above it* for as long as the handle
lives. Measured: the rename fails with ``ERROR_SHARING_VIOLATION``, the parent's
with ``ERROR_ACCESS_DENIED``, and both succeed the moment the handle closes. So
a child name built from the held directory's path resolves through a prefix that
cannot have changed -- which is the guarantee ``openat`` gives, obtained from the
one primitive Windows does provide. The final component is not covered by the
pin, so the operations that touch it -- install and every kind of removal -- open
that child, prove it is the object this call created, and then act on the
handle rather than on the name.

Identity is compared through ``os.fstat`` on both platforms. The Windows handle
is adopted into a real descriptor for exactly that reason: ``st_dev`` from
``fstat`` and ``dwVolumeSerialNumber`` from ``GetFileInformationByHandle`` are
different numbers on current CPython, so ``same_object`` has to see one of them
consistently.

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
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

if os.name == "nt":  # pragma: no cover - platform-selected at import
    from telegram_mcp import win_handles
else:  # pragma: no cover - platform-selected at import
    win_handles = None

# Windows marks junctions, symlinks and every other reparse point with this bit.
# `stat.S_ISLNK` is false for a directory junction, so the flag is the half of
# the link test that matters there.
_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# How many names near a caller's chosen one may be tried before giving up. Two
# saves in the same second collide on their own; a hundred is somebody being
# deliberate.
NAME_ATTEMPTS = 100

# "look up what this call created" as distinct from "expect nothing in
# particular", which `None` already means.
_UNSET = object()


class UnsafeTarget(Exception):
    """The object behind a name is not the object that was authorised."""


class SystemCalls:
    """The filesystem primitives this module uses, in one replaceable object.

    Subclass and override to drive a decision from a test. The defaults are the
    real calls; ``dir_fd`` says whether this host can resolve a name relative to
    an open directory, which is what decides the whole strategy below.
    """

    dir_fd = bool(getattr(os, "supports_dir_fd", set()))
    #: Whether this host can hold a directory open so its name cannot be moved.
    #: Where neither this nor ``dir_fd`` is available there is no way to bind an
    #: authorisation to an object, and :func:`open_allowed_directory` refuses.
    pins = win_handles is not None
    O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
    O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
    O_BINARY = getattr(os, "O_BINARY", 0)
    O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)

    # -- holding an object on a host without openat --------------------------

    def pin_directory(self, path) -> int:
        """A descriptor whose existence stops this directory's name from moving."""
        return win_handles.pin_directory(str(path))

    def open_for_change(self, path) -> int:
        """A descriptor on a child, opened so the child can be moved or removed."""
        return win_handles.open_for_change(str(path))

    def mark_deleted(self, fd) -> None:
        """Delete the object behind ``fd``, whatever its name now says."""
        win_handles.mark_deleted(fd)

    def rename_object(self, fd, destination, *, replace: bool) -> None:
        """Move the object behind ``fd`` to ``destination``."""
        win_handles.rename_to(fd, str(destination), replace=replace)

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


# O_NOFOLLOW makes the KERNEL refuse a name that reaches a link, and it refuses
# before any check in this module runs. The errno differs by system: ELOOP on
# Linux and macOS, EMLINK on the BSDs. Windows has no O_NOFOLLOW at all, so the
# flag is 0 and the refusal never arrives that way - which is why this went unseen
# until the suite first ran on Linux, where a symlinked source came back as a bare
# OSError and the caller reported it as 'not readable' instead of as a link.
_LINK_REFUSED = frozenset({errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)})


def _open_no_link(calls: "SystemCalls", target, flags, *rest, **where):
    """Open with O_NOFOLLOW, speaking the kernel's link refusal in this module's terms.

    Whatever the name reached, it is not the regular file or directory that was
    authorised, so callers get :class:`UnsafeTarget` rather than an errno to decode.
    """
    try:
        return calls.open(target, flags, *rest, **where)
    except OSError as error:
        if error.errno in _LINK_REFUSED:
            raise UnsafeTarget(
                f"{Path(target).name!r} is a link, not the object that was authorised"
            ) from error
        raise


def permitted_children(candidate: Path, roots: List[Path]) -> Optional[FrozenSet[str]]:
    """Which children of ``candidate`` the roots authorise. Raises if none do.

    ``None`` means all of them: ``candidate`` is a configured root, or lives
    inside one. A set of names means the only thing authorising ``candidate`` is
    that a root names a single **file** in it -- which the roots layer has always
    advertised (``_path_is_within_root`` special-cases ``root.is_file()``) and
    which used to be unreachable, because the read gate opens the file's parent
    and no file root contains its own parent. So a single-file root refused every
    read, always. The parent is opened now, and the root's own name is the whole
    of what may be done in it.
    """
    named: set = set()
    for root in roots:
        root = Path(root).resolve()
        if candidate == root or root in candidate.parents:
            return None
        if root.parent == candidate and root.is_file():
            named.add(root.name)
    if named:
        return frozenset(named)
    raise UnsafeTarget("the directory is outside the allowed roots")


# --- an open directory -------------------------------------------------------


class DirHandle:
    """A directory this process has open, and the only way to reach its children.

    On POSIX ``fd`` is a real descriptor and every child name is resolved by the
    kernel relative to it.

    On Windows ``fd`` is ``None`` and ``held`` is a descriptor on a Win32
    directory handle whose existence stops the kernel letting anyone rename this
    directory or any directory above it. Child names are therefore built from
    ``path``, but the part of that path an attacker could change is nailed down
    for as long as the handle lives. The final component is not, so anything that
    removes or replaces a child opens it, proves it is the object this call
    created, and acts on that handle.

    ``permitted`` is ``None`` for an ordinary directory and a set of names when
    the only thing authorising this directory is a root that names one file in
    it. ``made`` records what this call created, so cleanup can tell its own
    placeholder from whatever later took the name.
    """

    def __init__(
        self,
        path: Path,
        fd: Optional[int],
        identity,
        calls: SystemCalls,
        *,
        held: Optional[int] = None,
        permitted: Optional[FrozenSet[str]] = None,
        parent: Optional["DirHandle"] = None,
    ):
        self.path = Path(path)
        self.fd = fd
        self.held = held
        self.identity = identity
        self.permitted = permitted
        self._parent = parent
        self._calls = calls
        self._made: Dict[str, Any] = {}
        self._closed = False

    # -- plumbing ------------------------------------------------------------

    def _permit(self, name: str) -> None:
        """Refuse a child the roots did not authorise.

        Only ever restrictive: ``permitted`` is ``None`` unless a root named a
        single file, in which case that name is the entire authorisation and its
        neighbours are not covered by it.
        """
        if self.permitted is not None and name not in self.permitted:
            raise UnsafeTarget(f"{name!r} is not the file the allowed roots name")

    def _at(self, name: str):
        """The name to pass, and the keyword that binds it to this directory."""
        self._permit(name)
        if self.fd is not None:
            return name, {"dir_fd": self.fd}
        self.verify()
        return str(self.path / name), {}

    def verify(self) -> None:
        """Re-prove that this directory's name still names this directory.

        A no-op where a POSIX descriptor is held: the kernel is already answering
        about the object rather than the name. Where a Win32 handle is held the
        kernel has already refused to let the name move, and this proves that
        against the *object behind the handle* rather than against an ``lstat``
        remembered at authorisation time -- so a filesystem that did not honour
        the pin is caught rather than assumed away.
        """
        if self.fd is not None or self._closed:
            return
        reference = self.identity
        try:
            if self.held is not None:
                reference = self._calls.fstat(self.held)
            current = self._calls.lstat(str(self.path))
        except OSError as error:
            raise UnsafeTarget(f"the directory {self.path.name!r} is gone") from error
        if is_link(current) or not same_object(current, reference):
            raise UnsafeTarget(
                f"the directory {self.path.name!r} was replaced after it was authorised"
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for attribute in ("fd", "held"):
            descriptor = getattr(self, attribute)
            if descriptor is None:
                continue
            try:
                self._calls.close(descriptor)
            except OSError:
                pass
            setattr(self, attribute, None)

    def __enter__(self) -> "DirHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- children ------------------------------------------------------------

    def child_stat(self, name: str):
        """``lstat`` a child *through* this directory, never following a link."""
        self._permit(name)
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
        return _open_no_link(self._calls, target, flags, **where)

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
        fd = _open_no_link(self._calls, target, flags, mode, **where)
        try:
            # What this call created, so a later removal or install can tell it
            # apart from whatever may be wearing the name by then.
            self._made[name] = self._calls.fstat(fd)
        except OSError:  # pragma: no cover - a substituted SystemCalls in a test
            pass
        return fd

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

    def make_subdirectory(self, name: str) -> "DirHandle":
        """Create a child directory if it is missing, and open it, through here.

        The write gate used to run ``parent.mkdir(parents=True)`` while resolving
        a *pathname*: real directories created by name, before anything held
        them, and left behind when the authorisation that followed refused. Each
        level is now created through the handle on the level above it, so the
        chain is bound from the first component to the last.
        """
        target, where = self._at(name)
        try:
            self._calls.mkdir(target, 0o700, **where)
        except FileExistsError:
            pass
        return self.open_subdirectory(name)

    def open_subdirectory(self, name: str) -> "DirHandle":
        """Open a child directory, proving it is the directory just seen there."""
        named = self.child_stat(name)
        if is_link(named) or not stat_module.S_ISDIR(named.st_mode):
            raise UnsafeTarget(f"{name!r} is not a plain directory")
        if self.fd is None:
            self.verify()
            if not self._calls.pins:
                return DirHandle(self.path / name, None, named, self._calls, parent=self)
            held = self._calls.pin_directory(self.path / name)
            try:
                opened = self._calls.fstat(held)
                if not same_object(opened, named):
                    raise UnsafeTarget(f"{name!r} was replaced while it was being opened")
            except BaseException:
                self._calls.close(held)
                raise
            return DirHandle(self.path / name, None, opened, self._calls, held=held, parent=self)
        flags = os.O_RDONLY | self._calls.O_NOFOLLOW | self._calls.O_DIRECTORY
        fd = _open_no_link(self._calls, name, flags, dir_fd=self.fd)
        opened = self._calls.fstat(fd)
        if not same_object(opened, named):
            self._calls.close(fd)
            raise UnsafeTarget(f"{name!r} was replaced while it was being opened")
        return DirHandle(self.path / name, fd, opened, self._calls, parent=self)

    def _remove_object(self, name: str, expect) -> bool:
        """Remove a child by opening it and deleting the handle. Windows only.

        Returns ``False`` where this host has no such primitive, so the caller
        falls back to the name-based call. ``expect`` is what this call created;
        when it is known and no longer matches, the name has been given to
        something else and removing it would be destroying a stranger's file
        rather than tidying up after this one.
        """
        if self.held is None or not self._calls.pins:
            return False
        self._permit(name)
        self.verify()
        target = self._calls.open_for_change(self.path / name)
        try:
            if expect is not None and not same_object(self._calls.fstat(target), expect):
                raise UnsafeTarget(f"{name!r} is no longer the object this call created")
            self._calls.mark_deleted(target)
        finally:
            self._calls.close(target)
        return True

    def _remove(self, name: str, expect, directory: bool) -> None:
        """Remove a child, bound to the object this call created where it knows one.

        ``expect`` defaults to whatever :meth:`create_exclusive` recorded under
        this name. Passing it explicitly is for a caller that knows the identity
        by another route -- :meth:`remove_self`, which took it off its own
        handle -- and ``None`` for one that deliberately knows nothing, such as
        emptying a staging directory somebody else's library wrote into.
        """
        expect = self._made.get(name) if expect is _UNSET else expect
        if not self._remove_object(name, expect):
            if expect is not None and not same_object(self.child_stat(name), expect):
                raise UnsafeTarget(f"{name!r} is no longer the object this call created")
            target, where = self._at(name)
            remove = self._calls.rmdir if directory else self._calls.unlink
            remove(target, **where)
        self._made.pop(name, None)

    def unlink(self, name: str, expect=_UNSET) -> None:
        self._remove(name, expect, directory=False)

    def rmdir(self, name: str, expect=_UNSET) -> None:
        self._remove(name, expect, directory=True)

    def remove_self(self) -> None:
        """Remove the directory this handle holds, as the object it holds.

        Removing it by name from the caller is the same defect one level up:
        between the last look and the ``rmdir``, that name can belong to a
        directory this call never made. The pin has to be released first --
        nothing can delete a directory this process is holding open -- so the
        identity taken from the handle is what the removal is checked against.
        """
        if self._parent is None:
            raise UnsafeTarget("this directory was not opened through a parent handle")
        identity = self.identity
        name = self.path.name
        self.close()
        self._parent.rmdir(name, expect=identity)

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
        """Give ``source_name`` its final name in *this* directory.

        Both ends are bound to a held directory, so neither the origin nor the
        destination can be a name that started meaning something else.

        Where ``name`` is a placeholder this call reserved, the object wearing it
        is proved to still be that placeholder. Publishing over a name that has
        since been given to somebody else's file is not an install -- it is
        deleting data the caller never offered up. On Windows the check and the
        move are the same act: the placeholder is deleted through its own handle
        and the staged file is renamed in with ``ReplaceIfExists`` **off**, so a
        name that changed hands in between makes the rename fail rather than
        clobber. POSIX keeps ``renameat`` between two held descriptors, which is
        atomic and cannot be redirected.
        """
        self.verify()
        source.verify()
        self._permit(name)
        expect = self._made.get(name)
        if self.held is not None and source.held is not None and self._calls.pins:
            if expect is not None:
                self._remove_object(name, expect)
                self._made.pop(name, None)
            moving = self._calls.open_for_change(source.path / source_name)
            try:
                self._calls.rename_object(moving, self.path / name, replace=expect is None)
            finally:
                self._calls.close(moving)
            source._made.pop(source_name, None)
            return
        if expect is not None and not same_object(self.child_stat(name), expect):
            raise UnsafeTarget(f"{name!r} is no longer the placeholder this call reserved")
        if self.fd is not None and source.fd is not None:
            self._calls.replace(source_name, name, src_dir_fd=source.fd, dst_dir_fd=self.fd)
        else:
            self._calls.replace(str(source.path / source_name), str(self.path / name))
        self._made.pop(name, None)
        source._made.pop(source_name, None)

    def discard(self, name: str) -> None:
        """Best-effort removal of a child this call made. Never raises.

        Cleanup that throws replaces the failure worth reporting, and a directory
        that has stopped being the authorised one -- or a name that has stopped
        being the object this call created -- is something nothing here may
        delete, so a refusal to clean up is also a correct outcome.
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
        fd = _open_no_link(self._calls, target, flags, **where)
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


def _open_judged(path, roots: List[Path], calls: SystemCalls) -> DirHandle:
    """Judge an existing directory against the roots, then open the one judged.

    The order is the whole point. The verdict is taken about a resolved path; the
    open is done without following a link; the object that came back is then
    reconciled with what the name reaches *now*, and the name is resolved a
    second time and re-judged. A swap before the verdict is caught by the
    verdict, a swap after it by the reconciliation, and a swap after the open
    does not matter because the object is already held.
    """
    resolved = Path(path).resolve(strict=False)
    permitted = permitted_children(resolved, roots)

    def _still_inside():
        if Path(path).resolve(strict=False) != resolved:
            raise UnsafeTarget("the directory moved out of the allowed roots")
        permitted_children(resolved, roots)

    if calls.dir_fd:
        flags = os.O_RDONLY | calls.O_NOFOLLOW | calls.O_DIRECTORY
        fd = _open_no_link(calls, str(resolved), flags)
        try:
            opened = calls.fstat(fd)
            named = calls.lstat(str(resolved))
            if is_link(named) or not same_object(opened, named):
                raise UnsafeTarget("the directory was replaced while it was being opened")
            if not stat_module.S_ISDIR(opened.st_mode):
                raise UnsafeTarget("the destination is not a directory")
            _still_inside()
        except BaseException:
            calls.close(fd)
            raise
        return DirHandle(resolved, fd, opened, calls, permitted=permitted)

    if not calls.pins:
        # Neither primitive: nothing here can bind a verdict to an object, and a
        # verdict about a name is the defect this module exists to remove.
        named = calls.lstat(str(resolved))
        if is_link(named):
            raise UnsafeTarget("the destination is a link, not a directory")
        if not stat_module.S_ISDIR(named.st_mode):
            raise UnsafeTarget("the destination is not a directory")
        _still_inside()
        confirm = calls.lstat(str(resolved))
        if not same_object(confirm, named):
            raise UnsafeTarget("the directory was replaced while it was being authorised")
        return DirHandle(resolved, None, named, calls, permitted=permitted)

    # Hold the directory first: from here on the kernel will not let this name,
    # or any name above it, be moved onto a different object.
    held = calls.pin_directory(resolved)
    try:
        opened = calls.fstat(held)
        named = calls.lstat(str(resolved))
        if is_link(named) or bool(getattr(opened, "st_file_attributes", 0) & _REPARSE_POINT):
            raise UnsafeTarget("the destination is a link, not a directory")
        if not same_object(opened, named):
            raise UnsafeTarget("the directory was replaced while it was being opened")
        if not stat_module.S_ISDIR(opened.st_mode):
            raise UnsafeTarget("the destination is not a directory")
        _still_inside()
    except BaseException:
        calls.close(held)
        raise
    return DirHandle(resolved, None, opened, calls, held=held, permitted=permitted)


def open_allowed_directory(
    path, roots: List[Path], *, calls: SystemCalls = SYSTEM, create: bool = False
) -> DirHandle:
    """Open the directory ``path`` names, bound to the object the roots allowed.

    With ``create``, a destination that does not exist yet is built one component
    at a time *through the handle on the component above it*, starting from the
    deepest ancestor that already exists and is inside a root. Creating it by
    name first -- which is what the write gate used to do while it was still
    resolving a string -- puts real directories on disk before anything holds
    them, and leaves them behind when the authorisation that follows says no.
    """
    resolved = Path(path).resolve(strict=False)
    if not create:
        return _open_judged(resolved, roots, calls)

    missing: List[str] = []
    probe = resolved
    while not probe.exists():
        if probe.parent == probe:
            raise UnsafeTarget("the destination has no existing parent")
        missing.append(probe.name)
        probe = probe.parent

    handle = _open_judged(probe, roots, calls)
    for name in reversed(missing):
        try:
            child = handle.make_subdirectory(name)
        except BaseException:
            handle.close()
            raise
        handle.close()
        handle = child
    return handle


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
