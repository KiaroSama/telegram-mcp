"""The Windows half of "hold the object, not the name".

POSIX ends a filesystem check holding a descriptor and resolves every later name
*relative to it* -- ``openat``, ``unlinkat``, ``renameat``. Windows has none of
those, and ``os.open`` on a directory fails outright, so the previous code kept
the directory's ``lstat`` and re-checked it before each step. That is a smaller
window than a bare path, not a closed one: the re-check and the operation are
still two separate lookups of the same string.

Windows does give three primitives that close it, and this module is those three
and nothing else.

**A directory handle pins its own name, and its ancestors'.** ``CreateFileW``
with ``FILE_LIST_DIRECTORY`` and a share mask that omits ``FILE_SHARE_DELETE``
makes the kernel refuse any attempt to rename or delete that directory -- and
any attempt to rename a directory above it, because Windows will not move a
directory that contains an open handle. Measured on this project: with the
handle held, renaming the directory fails with ``ERROR_SHARING_VIOLATION`` (32)
and renaming its parent fails with ``ERROR_ACCESS_DENIED`` (5); with the handle
closed, both succeed immediately. So while a handle is held, every path that
runs *through* that directory means what it meant when it was authorised. The
access mask matters and is not decoration: ``FILE_READ_ATTRIBUTES`` alone was
measured NOT to pin -- the rename went through.

**A handle can be deleted rather than a name.** ``SetFileInformationByHandle``
with ``FileDispositionInfo`` marks the object the handle refers to. Cleanup then
removes what it opened and proved, instead of whatever currently answers to a
string.

**A handle can be renamed rather than a name.** ``FileRenameInfo`` moves the
object the handle refers to. ``ReplaceIfExists`` can be turned *off*, which is
the difference between "publish this file" and "publish this file over whatever
happens to be there".

``msvcrt.open_osfhandle`` adopts each handle into an ordinary file descriptor,
so ``os.fstat``, ``os.close`` and ``same_object`` work on it unchanged and the
identity comparison stays one function for both platforms. (``st_dev`` from
``os.fstat`` and ``dwVolumeSerialNumber`` from ``GetFileInformationByHandle`` are
NOT the same number on current CPython -- measured -- so going through ``fstat``
is also the only way to compare like with like.)

``ponytail:`` ``FILE_RENAME_INFO.RootDirectory`` is ignored by
``SetFileInformationByHandle`` -- measured, ``ERROR_INVALID_PARAMETER`` -- so the
destination is a full path rather than a name relative to the held directory.
That is equivalent here only because the directory handle pins the whole prefix;
a truly relative rename needs ``NtSetInformationFile``, which works but drags in
``ntdll`` for no additional guarantee.
"""

from __future__ import annotations

import os
from typing import Optional

# Guarded so the module IMPORTS on any host even though every entry point in it
# is Windows-only. `handles.py` already selects the platform by `os.name`, so
# nothing here is reachable elsewhere - but a module in the package that cannot
# be imported at all breaks anything that walks the package, which is how this
# surfaced: the tool-registry test imports every module and died on `msvcrt`.
if os.name == "nt":  # pragma: no cover - platform-selected at import
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:  # pragma: no cover - platform-selected at import
    ctypes = msvcrt = wintypes = None

# Enough access to list the directory -- which is what makes the kernel treat a
# rename of it as a sharing conflict -- and nothing more. No DELETE: asking for
# it would fail on a destination folder whose ACL denies it, and the pin does not
# need it.
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
# Reading and writing a security descriptor are separate rights, and the pin
# above asks for neither: a handle held only to stop a rename cannot be used to
# change what the object allows.
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004

_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_INFO = 4

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value if ctypes else None

_KERNEL32: Optional[object] = None


def _kernel32():
    """``kernel32`` with explicit signatures, built once.

    Every function gets a ``restype``/``argtypes``. Without them ctypes assumes C
    ``int`` and truncates a 64-bit handle to 32 bits, which works exactly as long
    as the OS keeps handing out small values. This project has been bitten by
    that twice already, so it is spelled out rather than trusted.
    """
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32 = kernel32
    return kernel32


def _fail(path: str) -> OSError:
    error = ctypes.WinError(ctypes.get_last_error())
    error.filename = path
    return error


def _adopt(handle: int, path: str) -> int:
    """Turn a Win32 handle into a file descriptor this process owns.

    Ownership moves with it: ``os.close`` on the descriptor closes the handle,
    and closing the handle is what releases the pin. One lifetime, one call.
    """
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        _kernel32().CloseHandle(wintypes.HANDLE(handle))
        raise


def pin_directory(path: str) -> int:
    """Open a directory so that its name -- and its parents' -- cannot be moved.

    ``FILE_FLAG_OPEN_REPARSE_POINT`` means a junction or symlink standing where
    the directory should be is opened *as the link*, so the caller's own
    reparse-point check sees it instead of being quietly redirected.
    """
    handle = _kernel32().CreateFileW(
        str(path),
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _fail(str(path))
    return _adopt(handle, str(path))


def open_for_security(path: str) -> int:
    """Open an object so its DACL can be written and read back through ONE handle.

    Deliberately not the pin: that handle is held to stop a rename and asks for
    no security rights at all, and adding WRITE_DAC to it would make opening any
    directory require a right the operator's own folders may not grant this
    account - breaking the roots gate for every caller, to serve the few places
    that harden something.

    BACKUP_SEMANTICS so a directory can be opened at all; OPEN_REPARSE_POINT so a
    link standing where the object should be is opened AS the link rather than
    silently followed.
    """
    handle = _kernel32().CreateFileW(
        str(path),
        _READ_CONTROL | _WRITE_DAC,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _fail(str(path))
    return _adopt(handle, str(path))


def open_for_change(path: str) -> int:
    """Open a child so it can be deleted or renamed *as an object*.

    ``FILE_SHARE_DELETE`` is granted here, unlike the directory pin: this handle
    exists in order to remove or move the thing it points at, and refusing to
    share what we are about to do ourselves would only make the operation fail.
    ``FILE_FLAG_BACKUP_SEMANTICS`` so the same call works for a subdirectory.
    """
    handle = _kernel32().CreateFileW(
        str(path),
        _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _fail(str(path))
    return _adopt(handle, str(path))


def mark_deleted(fd: int) -> None:
    """Delete the object behind ``fd``. Works for a file or an empty directory."""
    flag = ctypes.c_ubyte(1)
    ok = _kernel32().SetFileInformationByHandle(
        wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
        _FILE_DISPOSITION_INFO,
        ctypes.byref(flag),
        ctypes.sizeof(flag),
    )
    if not ok:
        raise _fail("")


def rename_to(fd: int, destination: str, *, replace: bool) -> None:
    """Move the object behind ``fd`` to ``destination``.

    ``replace=False`` is the interesting one: it fails rather than overwriting,
    which is what lets an install refuse a name that stopped being the empty
    placeholder it reserved instead of destroying whatever took it.
    """
    destination = str(destination)
    characters = len(destination)

    class _RenameInfo(ctypes.Structure):
        # Field order and natural alignment reproduce FILE_RENAME_INFO: the
        # BOOLEAN is padded out to the pointer alignment of RootDirectory, and
        # FileName is the variable-length tail. Explicit padding would be wrong
        # on a 32-bit build, so ctypes is left to align it.
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (characters + 1)),
        ]

    info = _RenameInfo()
    info.ReplaceIfExists = 1 if replace else 0
    info.RootDirectory = None
    info.FileNameLength = characters * ctypes.sizeof(wintypes.WCHAR)
    info.FileName = destination

    ok = _kernel32().SetFileInformationByHandle(
        wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
        _FILE_RENAME_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        raise _fail(destination)


__all__ = ["mark_deleted", "open_for_change", "pin_directory", "rename_to"]
