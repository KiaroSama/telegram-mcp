"""Which directories a file tool may touch, and turning a string into a path inside one.

Every tool that reads or writes a file on the operator's machine comes through here.
The contract is deliberately narrow: a caller supplies a string, and gets back either a
resolved path that is provably inside a configured root, or a refusal that says why.
Nothing else in the package is allowed to build a filesystem path from caller input.

Roots come from the MCP client when it implements `roots/list`, and otherwise from the
server's own `--allowed-root` arguments. A client that answers with an empty list is
saying "nothing is permitted" and is obeyed - that is a different state from a client
that cannot answer at all, which is why the status constants distinguish them.

**`SERVER_ALLOWED_ROOTS` is mutated IN PLACE, never rebound** - see
`_configure_allowed_roots_from_cli`, which slice-assigns into it. That is deliberate:
`runtime` and `main` star-import this name, so they hold second references to the same
list object, and mutating it keeps all three in step. Rebinding it would give this
module a new list that the other two cannot see.

The rule that follows for tests: patch the CONTENTS (`SERVER_ALLOWED_ROOTS[:] = [...]`),
not the name. And inside `_configure_allowed_roots_from_cli` a bare assignment would
create a function-local binding - there is no `global` statement - leaving the real list
empty and every file tool silently unconfigured.
"""

import argparse
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError

from telegram_mcp.handles import (
    DirHandle,
    UnsafeTarget,
    VerifiedFile,
    open_allowed_directory,
    open_verified_file,
)
from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import _parse_bool_env

# File-path tool security configuration
SERVER_ALLOWED_ROOTS: list[Path] = []
DEFAULT_DOWNLOAD_SUBDIR = "downloads"
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
ROOTS_UNSUPPORTED_ERROR_CODES = {-32601}
ROOTS_STATUS_READY = "ready"
ROOTS_STATUS_NOT_CONFIGURED = "not_configured"
ROOTS_STATUS_UNSUPPORTED_FALLBACK = "unsupported_fallback"
ROOTS_STATUS_CLIENT_DENY_ALL = "client_deny_all"
ROOTS_STATUS_SERVER_FALLBACK = "server_fallback"
ROOTS_STATUS_ERROR = "error"


def _dedupe_paths(paths: List[Path]) -> List[Path]:
    seen: set[str] = set()
    result: List[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


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


def _coerce_root_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"Unsupported root URI scheme: {parsed.scheme}")

    decoded_path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        decoded_path = f"//{parsed.netloc}{decoded_path}"
    if os.name == "nt" and decoded_path.startswith("/") and len(decoded_path) > 2:
        # file:///C:/tmp -> C:/tmp on Windows
        if decoded_path[2] == ":":
            decoded_path = decoded_path[1:]
    return Path(decoded_path).resolve(strict=True)


def _path_is_within_root(candidate: Path, root: Path) -> bool:
    root = root.resolve()
    if root.is_file():
        return candidate == root
    return candidate == root or root in candidate.parents


def _path_is_within_any_root(candidate: Path, roots: List[Path]) -> bool:
    return any(_path_is_within_root(candidate, root) for root in roots)


def _first_resolution_root(roots: List[Path]) -> Path:
    first = roots[0]
    return first if first.is_dir() else first.parent


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


async def _get_effective_allowed_roots(ctx: Optional[Context]) -> List[Path]:
    roots, _status = await _get_effective_allowed_roots_with_status(ctx)
    return roots


def _is_roots_unsupported_error(error: Exception) -> bool:
    if isinstance(error, MCPError):
        error_code = getattr(getattr(error, "error", None), "code", None)
        error_message = (
            getattr(getattr(error, "error", None), "message", None) or str(error)
        ).lower()
        if error_code in ROOTS_UNSUPPORTED_ERROR_CODES:
            return True
        return "method not found" in error_message or "not implemented" in error_message

    if isinstance(error, NotImplementedError):
        return True
    if isinstance(error, AttributeError):
        return "list_roots" in str(error)
    return False


def _coerce_paths_from_list_roots_validation_error(error: Exception) -> List[Path]:
    """Recover absolute filesystem roots when a client sends bare paths.

    Some MCP clients (notably Cursor) return workspace roots as plain absolute
    paths instead of ``file://`` URIs. The MCP SDK then fails pydantic validation
    of ``ListRootsResult`` even though the roots themselves are usable. Extract
    those paths from the validation error payload so file-path tools keep working.

    Which error pydantic reports depends on the path's shape. A POSIX path like
    ``/home/dev/ws`` has no scheme at all and yields ``url_parsing``, but on a
    Windows path like ``C:\\Users\\dev\\ws`` the drive letter parses as a scheme,
    so pydantic gets far enough to reject it as ``url_scheme`` instead. Accept
    both, or the Windows branch below is unreachable.
    """
    errors_fn = getattr(error, "errors", None)
    if not callable(errors_fn):
        return []

    try:
        details = errors_fn()
    except Exception:
        return []

    recovered: List[Path] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("url_parsing", "url_scheme"):
            continue
        value = item.get("input")
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not (candidate.startswith("/") or (len(candidate) > 2 and candidate[1] == ":")):
            # Unix absolute path, or Windows drive path like C:\...
            continue
        try:
            recovered.append(Path(candidate).expanduser().resolve())
        except Exception:
            continue
    return _dedupe_paths(recovered)


def _server_roots_fallback_enabled(value: Optional[str] = None) -> bool:
    """Whether server CLI roots may replace unusable/empty client Roots.

    Opt-in via the ``TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK`` environment variable.
    Applies when the client returns an empty roots list, or when ``list_roots``
    fails with an unexpected error (after any recoverable client paths are tried).
    Defaults to ``False`` to preserve the safe deny-all behavior.
    """
    raw_value = os.getenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK") if value is None else value
    return _parse_bool_env(raw_value, False)


async def _get_effective_allowed_roots_with_status(
    ctx: Optional[Context],
) -> tuple[List[Path], str]:
    fallback_roots = list(SERVER_ALLOWED_ROOTS)
    if ctx is None:
        if fallback_roots:
            return fallback_roots, ROOTS_STATUS_READY
        return [], ROOTS_STATUS_NOT_CONFIGURED

    try:
        list_roots_result = await ctx.session.list_roots()
    except Exception as error:
        recovered_roots = _coerce_paths_from_list_roots_validation_error(error)
        if recovered_roots:
            log_event(
                logging.WARNING,
                "recovered non-URI roots from the client",
                count=len(recovered_roots),
            )
            return recovered_roots, ROOTS_STATUS_READY
        if _is_roots_unsupported_error(error):
            if fallback_roots:
                return fallback_roots, ROOTS_STATUS_UNSUPPORTED_FALLBACK
            return [], ROOTS_STATUS_NOT_CONFIGURED
        # Unexpected list_roots failures (e.g. malformed client payloads that we
        # could not recover). Match empty-list behavior: opt-in server fallback.
        if fallback_roots and _server_roots_fallback_enabled():
            log_event(
                logging.WARNING,
                "roots request failed; falling back to server CLI roots "
                "(TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK)",
                error=error,
            )
            return fallback_roots, ROOTS_STATUS_SERVER_FALLBACK
        log_event(
            logging.ERROR,
            "roots request failed; disabling file-path tools for safety",
            error=error,
        )
        return [], ROOTS_STATUS_ERROR

    client_roots: List[Path] = []
    for root in list_roots_result.roots:
        try:
            client_roots.append(_coerce_root_uri_to_path(str(root.uri)))
        except Exception:
            # Ignore invalid root entries supplied by a client.
            continue

    if client_roots:
        return _dedupe_paths(client_roots), ROOTS_STATUS_READY

    # Roots API succeeded but returned an empty list. By default this is an
    # explicit deny-all. Some clients (e.g. ones that implement the Roots
    # capability but expose no roots) advertise an empty list even though the
    # operator configured server-side CLI roots; for those, an opt-in lets the
    # server-side roots take effect instead of disabling file tools entirely.
    if fallback_roots and _server_roots_fallback_enabled():
        return fallback_roots, ROOTS_STATUS_SERVER_FALLBACK
    return [], ROOTS_STATUS_CLIENT_DENY_ALL


async def _ensure_allowed_roots(
    ctx: Optional[Context], tool_name: str
) -> tuple[List[Path], Optional[str]]:
    roots, status = await _get_effective_allowed_roots_with_status(ctx)
    if not roots:
        if status == ROOTS_STATUS_CLIENT_DENY_ALL:
            return (
                [],
                (
                    f"{tool_name} is disabled because the client provided an empty "
                    "MCP Roots list (deny-all)."
                ),
            )
        if status == ROOTS_STATUS_ERROR:
            return (
                [],
                (
                    f"{tool_name} is disabled because MCP Roots could not be verified safely. "
                    "Check MCP client/server logs."
                ),
            )
        return (
            [],
            (
                f"{tool_name} is disabled until allowed roots are configured. "
                "Provide server CLI roots and/or client MCP Roots."
            ),
        )
    return roots, None


async def _resolve_readable_file_path(
    *,
    raw_path: str,
    ctx: Optional[Context],
    tool_name: str,
) -> tuple[Optional[Path], Optional[str]]:
    roots, error = await _ensure_allowed_roots(ctx, tool_name)
    if error:
        return None, error

    pattern_error = _contains_forbidden_path_patterns(raw_path)
    if pattern_error:
        return None, pattern_error

    candidate = Path(raw_path.strip())
    if not candidate.is_absolute():
        candidate = _first_resolution_root(roots) / candidate

    try:
        candidate = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, f"File not found: {raw_path}"

    if not _path_is_within_any_root(candidate, roots):
        return None, "Path is outside allowed roots."
    if not candidate.is_file():
        return None, f"Path is not a file: {candidate}"
    if not os.access(candidate, os.R_OK):
        return None, f"File is not readable: {candidate}"

    extension_error = _ensure_extension_allowed(tool_name, candidate)
    if extension_error:
        return None, extension_error

    size_error = _ensure_size_within_limit(tool_name, candidate)
    if size_error:
        return None, size_error

    return candidate, None


async def _resolve_writable_file_path(
    *,
    raw_path: Optional[str],
    default_filename: str,
    ctx: Optional[Context],
    tool_name: str,
) -> tuple[Optional[Path], Optional[str]]:
    roots, error = await _ensure_allowed_roots(ctx, tool_name)
    if error:
        return None, error

    if raw_path and raw_path.strip():
        pattern_error = _contains_forbidden_path_patterns(raw_path)
        if pattern_error:
            return None, pattern_error
        candidate = Path(raw_path.strip())
        if not candidate.is_absolute():
            candidate = _first_resolution_root(roots) / candidate
    else:
        safe_name = Path(default_filename).name
        candidate = _first_resolution_root(roots) / DEFAULT_DOWNLOAD_SUBDIR / safe_name

    candidate = candidate.resolve(strict=False)
    parent = candidate.parent.resolve(strict=False)
    if not _path_is_within_any_root(candidate, roots) or not _path_is_within_any_root(
        parent, roots
    ):
        return None, "Path is outside allowed roots."

    extension_error = _ensure_extension_allowed(tool_name, candidate)
    if extension_error:
        return None, extension_error

    # Nothing is created here. Resolving a path is answering a question about a
    # string, and this used to answer it by running `mkdir(parents=True)` -- real
    # directories on disk, made by name, before anything held them, and left
    # behind when the authorisation that follows refused. `_open_verified_directory`
    # builds the chain through handles instead, so what gets created is what was
    # judged.
    return candidate, None


# --- from a verdict about a name to a handle on the object -------------------
#
# Everything above answers a question about a *pathname*, and a pathname is only
# an instruction to look something up. The two gates below end that lookup: they
# take the verdict, open the object it was about, prove the object they got is
# the object that was judged, and hand back the open handle. Nothing downstream
# -- Telethon, `open`, `os.replace`, `unlink` -- resolves the name a second time.


@asynccontextmanager
async def _open_verified_source(*, raw_path: str, ctx: Optional[Context], tool_name: str):
    """Yield ``(source, error)``: an OPEN, verified file, never a pathname.

    ``source.handle`` is what goes to Telethon. It accepts any seekable binary
    stream and reads ``name`` for the mime type and the filename attribute, so
    handing it the descriptor costs nothing and removes the reopen entirely --
    along with the operator's directory layout, which used to travel with the
    upload as a full path.

    The size ceiling is applied to ``fstat`` of that descriptor. Measured off the
    name it was a ceiling on whatever wore the name at the time of measuring.
    """
    candidate, error = await _resolve_readable_file_path(
        raw_path=raw_path, ctx=ctx, tool_name=tool_name
    )
    if error:
        yield None, error
        return

    roots, roots_error = await _ensure_allowed_roots(ctx, tool_name)
    if roots_error:
        yield None, roots_error
        return

    try:
        with open_allowed_directory(candidate.parent, roots) as directory:
            source = open_verified_file(
                directory, candidate.name, max_bytes=MAX_FILE_BYTES.get(tool_name)
            )
    except UnsafeTarget as unsafe:
        yield None, f"{tool_name} refused this file: {unsafe}."
        return
    except OSError:
        yield None, f"File is not readable: {candidate}"
        return

    try:
        yield source, None
    finally:
        source.close()


@asynccontextmanager
async def _open_verified_directory(*, path: Path, ctx: Optional[Context], tool_name: str):
    """Yield ``(directory, error)``: an OPEN directory to create children in.

    A write is a sequence -- reserve a name, stage the bytes, install them, clean
    up -- and every step of it used to re-resolve the same string. Bound to a
    handle instead, a directory swapped halfway through stops the sequence rather
    than redirecting it.

    Missing components are created here rather than while the path was being
    resolved, so each one is made through the handle on the one above it and the
    whole chain is inside a root that was judged first.
    """
    roots, roots_error = await _ensure_allowed_roots(ctx, tool_name)
    if roots_error:
        yield None, roots_error
        return

    try:
        directory = open_allowed_directory(path, roots, create=True)
    except UnsafeTarget as unsafe:
        yield None, f"{tool_name} refused this destination: {unsafe}."
        return
    except OSError:
        yield None, f"Directory not writable: {path}"
        return

    try:
        yield directory, None
    finally:
        directory.close()


def _configure_allowed_roots_from_cli(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="telegram-mcp",
        add_help=False,
        description=(
            "Optional positional arguments define server-side allowed roots "
            "for file-path tools."
        ),
    )
    parser.add_argument("allowed_roots", nargs="*")
    parsed, _unknown = parser.parse_known_args(argv or [])

    resolved_roots: List[Path] = []
    for raw_root in parsed.allowed_roots:
        root = Path(raw_root).expanduser()
        if not root.exists():
            raise SystemExit(f"Allowed root does not exist: {root}")
        resolved = root.resolve(strict=True)
        resolved_roots.append(resolved)

    # In place, deliberately. `runtime` and `main` re-export this name, so they hold
    # further references to the SAME list; rebinding here would update only this
    # module's name and leave theirs pointing at the old, empty list.
    SERVER_ALLOWED_ROOTS[:] = _dedupe_paths(resolved_roots)


# Re-export shared runtime names for tool modules that use star imports.


__all__ = [
    "safe_suffix",
    "target_path",
    "DEFAULT_DOWNLOAD_SUBDIR",
    "DirHandle",
    "UnsafeTarget",
    "VerifiedFile",
    "DISALLOWED_PATH_PATTERNS",
    "EXTENSION_ALLOWLISTS",
    "MAX_FILE_BYTES",
    "ROOTS_STATUS_CLIENT_DENY_ALL",
    "ROOTS_STATUS_ERROR",
    "ROOTS_STATUS_NOT_CONFIGURED",
    "ROOTS_STATUS_READY",
    "ROOTS_STATUS_SERVER_FALLBACK",
    "ROOTS_STATUS_UNSUPPORTED_FALLBACK",
    "ROOTS_UNSUPPORTED_ERROR_CODES",
    "SERVER_ALLOWED_ROOTS",
    "_coerce_paths_from_list_roots_validation_error",
    "_coerce_root_uri_to_path",
    "_configure_allowed_roots_from_cli",
    "_contains_forbidden_path_patterns",
    "_dedupe_paths",
    "_ensure_allowed_roots",
    "_ensure_extension_allowed",
    "_ensure_size_within_limit",
    "_first_resolution_root",
    "_get_effective_allowed_roots",
    "_get_effective_allowed_roots_with_status",
    "_is_roots_unsupported_error",
    "_open_verified_directory",
    "_open_verified_source",
    "_path_is_within_any_root",
    "_path_is_within_root",
    "_resolve_readable_file_path",
    "_resolve_writable_file_path",
    "_server_roots_fallback_enabled",
    "open_allowed_directory",
    "open_verified_file",
]
