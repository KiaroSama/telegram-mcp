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


def _dacl_sids(path: str) -> Optional[list]:
    """Every SID with an entry in the object's DACL, read back off the object itself.

    ``None`` means the DACL could not be read, which is not the same as "no other
    identities" and must never be reported as success.
    """
    ctypes, advapi32, kernel32, _tu, _acl, _ace, size_info_type = _api()
    from ctypes import wintypes

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    try:
        if not dacl:
            # A NULL DACL grants everyone everything. It is the loosest possible
            # answer, so it is reported as an identity nobody expects rather than
            # as an empty list.
            return ["<null-dacl>"]
        info = size_info_type()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION
        ):
            return None
        found = []
        for index in range(info.AceCount):
            ace = wintypes.LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            entry = ctypes.cast(ace, ctypes.POINTER(_api()[5])).contents
            sid_pointer = ctypes.addressof(entry) + type(entry).SidStart.offset
            text = _sid_text(ctypes.c_void_p(sid_pointer))
            found.append(text or "<unreadable-sid>")
        return found
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def verify_owner_only(path: Union[str, Path]) -> bool:
    """Whether the object's DACL names this account and nothing else.

    Read off the object, not inferred from the call that set it. A tool exiting 0
    says the tool ran; this says what the file actually allows.
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
        mine = _sid_text(sid)
        present = _dacl_sids(str(path))
    except (OSError, AttributeError, ValueError):
        return False
    if mine is None or present is None:
        return False
    return set(present) == {mine}


def restrict_to_owner_strict(path: Union[str, Path]) -> bool:
    """Replace the object's DACL with one entry for this account, then verify it.

    Returns whether the object is *now* owner-only, which is a different claim from
    "the call succeeded" - and the one every caller actually wanted.
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
    return verify_owner_only(path)


__all__ = ["restrict_to_owner_strict", "verify_owner_only"]
