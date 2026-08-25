"""Which directories a file tool may touch, and turning a string into a path inside one.

Every tool that reads or writes a file on the operator's machine comes through here.
The contract is deliberately narrow: a caller supplies a string, and gets back either a
resolved path that is provably inside a configured root, or a refusal that says why.
Nothing else in the package is allowed to build a filesystem path from caller input.

Roots come from the MCP client when it implements `roots/list`, and otherwise from the
server's own `--allowed-root` arguments. A client that answers with an empty list is
saying "nothing is permitted" and is obeyed - that is a different state from a client
that cannot answer at all, which is why the status constants distinguish them.

**`SERVER_ALLOWED_ROOTS` is rebound, not mutated**, by `_configure_allowed_roots_from_cli`.
A rebind is visible only through the module that owns the name, so anything patching it
for a test must patch THIS module - `runtime` and `main` hold second names for the same
list and rebinding those changes nothing here. `main.py` keeps a sync shim for the
historic seam.
"""

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError

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
    if ".." in Path(value).parts:
        return "Path traversal is not allowed."
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
    if isinstance(error, McpError):
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
