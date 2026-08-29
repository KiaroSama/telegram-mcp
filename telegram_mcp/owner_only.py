"""Make a file or directory reachable by its owner and no one else, and prove it.

`icacls <path> /inheritance:r /grant:r <user>:(F)` looks like it does this and does
not. `/inheritance:r` drops the INHERITED entries; `/grant:r` replaces the explicit
entry for the principal it names. Neither touches an explicit entry belonging to
anyone else, so a file that already carried `Everyone:(R)` kept it -- and the call
returned 0, so every caller was told the file was private.

Measured on this project before the change: seeding `Everyone:(R)` on a temp file
and then "hardening" it left `Everyone:(R)` in the DACL, while the helper returned
True. Sessions, the alias store, `.env` backups, the event feed and the log all
depended on that answer.

The fix is not a better command line. A DACL is a list, and the only way to be sure
what is in it is to write the whole list: `SetNamedSecurityInfoW` with a freshly
built ACL and `PROTECTED_DACL_SECURITY_INFORMATION` replaces every entry and stops
inheritance from adding more. Then :func:`verify_owner_only` reads the DACL back off
the same object and checks it, because a call that returns success is evidence about
a call, not about a file.

Only ``advapi32``/``kernel32`` through ctypes -- no new dependency, and no elevation
for an object the caller owns.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

# The identity contract, stated once: the account this process runs as, and nobody
# else. Not SYSTEM, not Administrators - neither is needed to read a file this
# process wrote, and an Administrator can take ownership regardless, so naming them
# would widen the DACL without buying anything.
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

_ACL_REVISION = 2
_FILE_ALL_ACCESS = 0x001F01FF

# So that files created inside a hardened directory start out hardened too, rather
# than relying on every writer remembering to call this.
_OBJECT_INHERIT_ACE = 0x01
_ACCESS_ALLOWED_ACE_TYPE = 0x00
# The control bit that says the DACL is detached from inheritance. Without it a
# list that reads as owner-only today gains whatever the parent grants tomorrow,
# so the SID set alone was never the whole answer.
_SE_DACL_PROTECTED = 0x1000
_CONTAINER_INHERIT_ACE = 0x02

_ACL_SIZE_INFORMATION = 2

_WINDOWS_API: Optional[tuple] = None


def _api():
    """ctypes declarations for the security calls, built once and cached.

    Every function gets an explicit ``restype``/``argtypes``. Without them ctypes
    assumes C ``int`` and truncates a 64-bit pointer or handle to 32 bits, which
    works for as long as the OS hands out small values and then fails. This project
    has already been bitten by exactly that twice - in the Win32 capture path and in
    the test runner's job objects - so it is spelled out here rather than trusted.
    """
    global _WINDOWS_API
    if _WINDOWS_API is not None:
        return _WINDOWS_API

    import ctypes
    from ctypes import wintypes

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    class ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]

    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]

    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    # The HANDLE forms. A pathname is resolved again by every call that takes
    # one, so applying a descriptor by name and then verifying it by name proves
    # nothing about a single object: the name can change hands in between. These
    # take the handle the caller already holds, and that handle IS the object.
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    advapi32.SetSecurityInfo.restype = wintypes.DWORD
    advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]

    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    _WINDOWS_API = (
        ctypes,
        advapi32,
        kernel32,
        TOKEN_USER,
        ACL,
        ACCESS_ALLOWED_ACE,
        ACL_SIZE_INFORMATION,
    )
    return _WINDOWS_API


def _current_user_sid() -> Optional[Any]:
    """A copy of this process's user SID, owned by this module.

    Copied out of the token buffer rather than pointed into it: the buffer is a
    local, and a SID that dangles once it is freed would be a use-after-free in the
    one place that decides who may read a session file.
    """
    ctypes, advapi32, kernel32, token_user_type, *_ = _api()
    from ctypes import wintypes

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, needed.value, ctypes.byref(needed)
        ):
            return None
        source = ctypes.cast(buffer, ctypes.POINTER(token_user_type)).contents.User.Sid
        length = advapi32.GetLengthSid(source)
        if not length:
            return None
        owned = ctypes.create_string_buffer(length)
        if not advapi32.CopySid(length, owned, source):
            return None
        return owned
    finally:
        kernel32.CloseHandle(token)


def _sid_text(sid) -> Optional[str]:
    """`S-1-5-21-...` for a SID, for messages and tests. None if it cannot be read."""
    ctypes, advapi32, kernel32, *_ = _api()
    from ctypes import wintypes

    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    try:
        return text.value
    finally:
        kernel32.LocalFree(text)


def _build_owner_only_acl(sid, inheritable: bool):
    """A DACL containing exactly one entry: full control for ``sid``."""
    ctypes, advapi32, _kernel32, _tu, acl_type, ace_type, _asi = _api()

    sid_length = advapi32.GetLengthSid(sid)
    # The ACE carries the SID inline, and SidStart is the first DWORD of it - so the
    # sizeof() already counts four of the SID's bytes.
    size = ctypes.sizeof(acl_type) + ctypes.sizeof(ace_type) - ctypes.sizeof(ctypes.c_ulong)
    size += sid_length
    buffer = ctypes.create_string_buffer(size)
    if not advapi32.InitializeAcl(buffer, size, _ACL_REVISION):
        return None
    flags = (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE) if inheritable else 0
    if not advapi32.AddAccessAllowedAceEx(buffer, _ACL_REVISION, flags, _FILE_ALL_ACCESS, sid):
        return None
    return buffer


def _read_dacl(target, by_handle: bool):
    """``(entries, protected)`` read off the object, or ``(None, None)``.

    Each entry is ``{sid, mask, flags, type}``. The SID alone was what this used to
    return, and a SID set passes three lists that are not owner-only in any useful
    sense: one still attached to inheritance, one whose single entry is a DENY, and
    one on a directory whose entry does not propagate - so files born inside inherit
    nothing and fall back to the creator's token.

    ``None`` means the DACL could not be read, which is not the same as "no other
    identities" and must never be reported as success.
    """
    ctypes, advapi32, kernel32, _tu, _acl, ace_type, size_info_type = _api()
    from ctypes import wintypes

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    if by_handle:
        status = advapi32.GetSecurityInfo(
            wintypes.HANDLE(target),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    else:
        status = advapi32.GetNamedSecurityInfoW(
            str(target),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    if status != 0:
        return None, None
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            return None, None
        protected = bool(control.value & _SE_DACL_PROTECTED)

        if not dacl:
            # A NULL DACL grants everyone everything. The loosest possible answer,
            # so it is reported as an identity nobody expects rather than as an
            # empty list.
            return ([{"sid": "<null-dacl>", "mask": 0, "flags": 0, "type": 0}], protected)

        info = size_info_type()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION
        ):
            return None, None

        found = []
        for index in range(info.AceCount):
            ace = wintypes.LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return None, None
            entry = ctypes.cast(ace, ctypes.POINTER(ace_type)).contents
            sid_pointer = ctypes.addressof(entry) + type(entry).SidStart.offset
            found.append(
                {
                    "sid": _sid_text(ctypes.c_void_p(sid_pointer)) or "<unreadable-sid>",
                    "mask": entry.Mask,
                    "flags": entry.Header.AceFlags,
                    "type": entry.Header.AceType,
                }
            )
        return found, protected
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _is_owner_only(entries, protected, mine, inheritable: bool) -> bool:
    """Whether what was read back is a protected, owner-only, usable list."""
    if entries is None or protected is None or mine is None:
        return False
    # Protection is required of a DIRECTORY and not of a file, and the asymmetry
    # is the design rather than an oversight. A directory that is not protected
    # allows today whatever its parent grants tomorrow, and everything born
    # inside it inherits that. A FILE inside such a directory is private BY
    # inheritance - that is how a SQLite -wal file gets its permissions at all -
    # so demanding protection of it would refuse the very mechanism this module
    # exists to establish. What is demanded of a file is that the list, however
    # it arrived, names this account and nobody else.
    if inheritable and not protected:
        return False
    if len(entries) != 1:
        return False

    only = entries[0]
    if only["sid"] != mine:
        return False
    # A single DENY entry for this account also has a one-element SID set.
    if only["type"] != _ACCESS_ALLOWED_ACE_TYPE:
        return False
    # And an entry granting this account nothing useful locks the owner out of the
    # file it is supposed to own.
    if only["mask"] & _FILE_ALL_ACCESS != _FILE_ALL_ACCESS:
        return False

    if inheritable:
        # Without these the DIRECTORY is private and everything born in it is not:
        # a SQLite -wal or -shm file inherits nothing and falls back to whatever
        # the creator's token grants.
        wanted = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
        if only["flags"] & wanted != wanted:
            return False
    return True


def verify_owner_only(path: Union[str, Path]) -> bool:
    """Whether the object's DACL names this account, and nothing else, usably.

    Read off the object, not inferred from the call that set it. A tool exiting 0
    says the tool ran; this says what the file actually allows - and for a
    directory, what the files born inside it will allow.
    """
    if os.name != "nt":
        try:
            return (os.stat(path).st_mode & 0o077) == 0
        except OSError:
            return False
    try:
        sid = _current_user_sid()
        if sid is None:
            return False
        entries, protected = _read_dacl(str(path), by_handle=False)
        return _is_owner_only(entries, protected, _sid_text(sid), Path(path).is_dir())
    except (OSError, AttributeError, ValueError):
        return False


def verify_handle_owner_only(handle: int, *, inheritable: bool) -> bool:
    """The same question asked of an OPEN HANDLE rather than of a name.

    Windows only; there is no handle-addressed equivalent on POSIX, where the
    caller uses the path form and the mode bits answer it.
    """
    if os.name != "nt":
        return False
    try:
        sid = _current_user_sid()
        if sid is None:
            return False
        entries, protected = _read_dacl(handle, by_handle=True)
        return _is_owner_only(entries, protected, _sid_text(sid), inheritable)
    except (OSError, AttributeError, ValueError):
        return False


def restrict_handle_to_owner(handle: int, *, inheritable: bool) -> bool:
    """Write the whole DACL through a HELD HANDLE, then read it back through it.

    The pathname form resolves the name once to apply and once to verify, and
    between those two calls the name can be given to a different object - so it can
    report success about something it never wrote to. A handle IS the object; there
    is nothing left to re-resolve.
    """
    if os.name != "nt":
        return False
    try:
        ctypes, advapi32, *_ = _api()
        from ctypes import wintypes

        sid = _current_user_sid()
        if sid is None:
            return False
        acl = _build_owner_only_acl(sid, inheritable=inheritable)
        if acl is None:
            return False
        status = advapi32.SetSecurityInfo(
            wintypes.HANDLE(handle),
            _SE_FILE_OBJECT,
            # PROTECTED is the half that makes this a replacement rather than an
            # addition: it detaches the object from inheritance, so nothing flows
            # back in behind the new list.
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.cast(acl, ctypes.c_void_p),
            None,
        )
        if status != 0:
            return False
    except (OSError, AttributeError, ValueError):
        return False
    return verify_handle_owner_only(handle, inheritable=inheritable)


def restrict_to_owner_strict(path: Union[str, Path]) -> bool:
    """Replace the object's DACL with one entry for this account, then verify it.

    Returns whether the object is *now* owner-only, which is a different claim from
    "the call succeeded" - and the one every caller actually wanted.

    Prefer :func:`restrict_handle_to_owner` where a handle is already held: this
    form resolves the name twice and cannot prove both times found the same object.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
        except OSError:
            return False
        return verify_owner_only(path)

    if not os.path.exists(path):
        return False
    try:
        ctypes, advapi32, *_ = _api()
        sid = _current_user_sid()
        if sid is None:
            return False
        acl = _build_owner_only_acl(sid, inheritable=Path(path).is_dir())
        if acl is None:
            return False
        status = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.cast(acl, ctypes.c_void_p),
            None,
        )
        if status != 0:
            return False
    except (OSError, AttributeError, ValueError):
        return False
    return verify_owner_only(path)


__all__ = [
    "restrict_handle_to_owner",
    "restrict_to_owner_strict",
    "verify_handle_owner_only",
    "verify_owner_only",
]
