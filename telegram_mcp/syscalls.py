"""The operating-system surface this package opens files through, in one place.

Split out of ``handles.py`` because it is a different KIND of thing from the
rest of that module. Everything else there is policy - what a path is allowed to
be, whether two handles name the same object, which children a root permits.
This is the port underneath it: the raw ``os`` calls, named, so the policy can
be tested against an injected substitute rather than against a real filesystem.

The tests rely on exactly that. ``tests/test_handle_binding.py`` subclasses this
to make a rename land BETWEEN two syscalls, which is the only way to provoke the
race the policy above exists to lose safely; a real filesystem cannot be asked
to do that on demand.

Re-exported from ``handles`` so ``handles.SystemCalls`` and
``from telegram_mcp.handles import SystemCalls`` both keep working - moving code
should not move anyone's import.
"""

import os
import stat as stat_module

_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

if os.name == "nt":  # pragma: no cover - platform-selected at import
    from telegram_mcp import win_handles
else:  # pragma: no cover - platform-selected at import
    win_handles = None


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

    def restrict_directory(self, path) -> bool:
        """Make a just-created directory owner-only, and say whether it now is.

        POSIX: ``mkdir(0o700)`` already did it - umask can only clear bits, never
        add them - so this confirms rather than acts.

        Windows: the mode argument to ``mkdir`` is ignored ENTIRELY. Without this
        a transfer directory inherits whatever its parent grants, and the bytes
        staged in it are readable by every account that inherits, for as long as
        the download takes.
        """
        if os.name != "nt":
            return (stat_module.S_IMODE(os.stat(path).st_mode) & 0o077) == 0

        import msvcrt

        from telegram_mcp import win_handles
        from telegram_mcp.owner_only import restrict_handle_to_owner

        # Through ONE handle, not through the name twice. The pathname form
        # resolves the name to apply the descriptor and again to read it back, so
        # it can report success about an object it never wrote to. This handle is
        # opened for exactly this and closed straight after; the pin taken by
        # `open_subdirectory` deliberately asks for no security rights, because
        # requiring WRITE_DAC to open a directory would break the roots gate on
        # every folder an operator did not create.
        try:
            descriptor = win_handles.open_for_security(str(path))
        except OSError:
            return False
        try:
            return restrict_handle_to_owner(msvcrt.get_osfhandle(descriptor), inheritable=True)
        finally:
            os.close(descriptor)

    def fsync(self, fd):
        os.fsync(fd)

    def write(self, fd, data):
        return os.write(fd, data)
