"""What a caller-supplied path or a sender-supplied extension may look like.

Split out of ``file_roots``: that module decides WHERE a file tool may go; this one
decides what a name may contain wherever it goes - no wildcards or traversal, no NTFS
stream colons, no suffix Windows would execute - and how large a file each tool accepts.
Re-exported from ``file_roots`` so nobody's import moves.
"""

import re
from pathlib import Path
from typing import Optional

__all__ = [
    "DISALLOWED_PATH_PATTERNS",
    "EXTENSION_ALLOWLISTS",
    "MAX_FILE_BYTES",
    "_contains_forbidden_path_patterns",
    "_ensure_extension_allowed",
    "_ensure_size_within_limit",
    "safe_suffix",
    "target_path",
]

DISALLOWED_PATH_PATTERNS = ("*", "?", "[", "]", "{", "}", "~", "\x00")
EXTENSION_ALLOWLISTS: dict[str, set[str]] = {
    "send_voice": {".ogg", ".opus"},
    "send_sticker": {".webp"},
    "set_profile_photo": {".jpg", ".jpeg", ".png", ".webp"},
    "edit_chat_photo": {".jpg", ".jpeg", ".png", ".webp"},
}
# A leading dot then 1-7 ASCII alphanumerics. That admits every real media
# extension (.jpg, .webm, .ogg, .tgs, .sticker) while rejecting colons, spaces,
# path separators, inner dots and the empty suffix.
_WELL_FORMED_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,7}$")

# Well formed and still dangerous: Windows runs or follows these when the
# operator double-clicks the saved file in the folder they chose. The rule above
# cannot catch them -- ".hta" is a dot and three ASCII letters, exactly like
# ".jpg".
#
# A denylist is normally the weaker shape and is chosen deliberately here. This
# tool saves *arbitrary* media -- a PDF, a zip, an mp3 -- so an allowlist of
# media extensions would refuse legitimate documents the operator asked to save.
# The threat answered is narrow and its members are enumerable: a suffix Windows
# itself executes or follows. That, and only that, justifies adding one.
_SHELL_INTERPRETED_SUFFIXES = frozenset(
    {
        ".hta",
        ".cmd",
        ".bat",
        ".com",
        ".exe",
        ".scr",
        ".pif",
        ".msi",
        ".ps1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".reg",
        ".lnk",
        ".url",
    }
)


def safe_suffix(candidate: str) -> str:
    """The candidate suffix if it is well formed, else ``.bin``.

    The suffix arrives from Telethon's ``File.ext``, i.e. from the mime type or
    filename the *sender* chose, and it is concatenated into a real filename
    written into one of the operator's configured roots. ".webm:ads" is the case
    this closes: on Windows that makes NTFS create an alternate data stream, so
    the visible file looks empty while the payload lives in the stream and the
    reported path carries the ":stream" suffix. Separators, spaces, inner dots
    and an over-long or empty suffix go the same way.

    The second rule answers the other threat: ".hta" is well formed, so the
    first rule keeps it, and double-clicking the saved file would then run it.
    Shell-interpreted suffixes are replaced even though their shape is fine.

    It lives here rather than in a tool module because more than one tool saves
    sender-named bytes: ``save_disappearing_media`` had this guard and
    ``download_media`` did not, which is the whole reason it moved.

    ``visual/frames.py`` guards the temp-file path with a decoder allowlist. It
    can, because it only ever decodes. This tool saves arbitrary media, so the
    shape here is well-formedness plus a narrow denylist rather than a fixed set
    of decodable types.
    """
    if not _WELL_FORMED_SUFFIX.match(candidate):
        return ".bin"
    # Case-folded: a sender can send ".HTA" as easily as ".hta".
    if candidate.lower() in _SHELL_INTERPRETED_SUFFIXES:
        return ".bin"
    return candidate


def target_path(out_path: Path, suffix: str) -> tuple:
    """The path to write, with the media's extension enforced over the caller's.

    Returns ``(path, replaced_suffix)``; ``replaced_suffix`` is None when the
    caller's own extension already agreed.

    A caller-supplied suffix used to win outright, so ``file_path="note.exe"``
    wrote sender-controlled bytes into a file Windows executes on double-click.
    That is the same hole the sender-side guard above closes, entered through the
    other door - and the comment two lines up already claimed the extension comes
    from the media and never from the caller.
    """
    if not out_path.suffix:
        return out_path.with_suffix(suffix), None
    if out_path.suffix.lower() == suffix.lower():
        return out_path, None
    return out_path.with_suffix(suffix), out_path.suffix


MAX_FILE_BYTES: dict[str, int] = {
    "send_file": 200 * 1024 * 1024,  # 200 MB
    "upload_file": 200 * 1024 * 1024,
    "send_voice": 100 * 1024 * 1024,
    "send_sticker": 10 * 1024 * 1024,
    "set_profile_photo": 50 * 1024 * 1024,
    "edit_chat_photo": 50 * 1024 * 1024,
}


def _contains_forbidden_path_patterns(raw_path: str) -> Optional[str]:
    value = raw_path.strip()
    if not value:
        return "Path must not be empty."
    if any(token in value for token in DISALLOWED_PATH_PATTERNS):
        return "Path contains disallowed wildcard/shell patterns."
    candidate = Path(value)
    if ".." in candidate.parts:
        return "Path traversal is not allowed."
    # A colon separates a DRIVE and nothing else. Inside a component it names an
    # NTFS alternate data stream, so `file_path="notes:hidden"` writes the bytes
    # into a stream of `notes` and leaves a visible, EMPTY `notes` behind -
    # measured: the folder listed one 0-byte file and the payload was not in it.
    # `safe_suffix` already refuses this in the sender's extension and says why;
    # the caller's own name reached `create_exclusive` unchecked, which is the
    # same hole through the other door. Refused on every platform, as the
    # extension rules above are, so one answer holds wherever the server runs.
    parts = candidate.parts
    if candidate.drive or candidate.root:
        parts = parts[1:]
    if any(":" in part for part in parts):
        return (
            "Path components must not contain ':' - on Windows that names an "
            "alternate data stream rather than a file."
        )
    return None


def _ensure_extension_allowed(tool_name: str, candidate: Path) -> Optional[str]:
    allowlist = EXTENSION_ALLOWLISTS.get(tool_name)
    if not allowlist:
        return None
    if candidate.suffix.lower() not in allowlist:
        allowed = ", ".join(sorted(allowlist))
        return f"File extension is not allowed for {tool_name}. Allowed: {allowed}."
    return None


def _ensure_size_within_limit(tool_name: str, candidate: Path) -> Optional[str]:
    max_bytes = MAX_FILE_BYTES.get(tool_name)
    if not max_bytes:
        return None
    size = candidate.stat().st_size
    if size > max_bytes:
        return f"File is too large for {tool_name}: {size} bytes " f"(limit: {max_bytes} bytes)."
    return None
