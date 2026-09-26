"""Which directories a file tool may touch, and turning a string into a path inside one.

Every tool that reads or writes a file on the operator's machine comes through here.
The contract is deliberately narrow: a caller supplies a string, and gets back either a
resolved path that is provably inside a configured root, or a refusal that says why.
Nothing else in the package is allowed to build a filesystem path from caller input.

Which folders count is the safeguard's rule (``safeguard/folders.py``, FR-030..FR-034):
``files/downloads`` and ``files/outbox``, the owner's configured folders
(``TELEGRAM_FILE_ROOTS`` / ``--allowed-root``), folders granted "always allow", and a
folder the owner allowed for this one call. The MCP client's roots grant nothing alone.

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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from mcp.server.mcpserver import Context

from telegram_mcp.handles import (
    DirHandle,
    UnsafeTarget,
    VerifiedFile,
    open_allowed_directory,
    open_verified_file,
)
from telegram_mcp.safe_log import log_event
from telegram_mcp.safeguard import folders

# File-path tool security configuration
SERVER_ALLOWED_ROOTS: list[Path] = []
DEFAULT_DOWNLOAD_SUBDIR = "downloads"

# The name rules live next door (what a path may contain); this module keeps where it
# may point. Re-exported so every existing import keeps working.
from telegram_mcp.file_names import (  # noqa: E402,F401  (re-exported)
    DISALLOWED_PATH_PATTERNS,
    EXTENSION_ALLOWLISTS,
    MAX_FILE_BYTES,
    _contains_forbidden_path_patterns,
    _ensure_extension_allowed,
    _ensure_size_within_limit,
    safe_suffix,
    target_path,
)

ROOTS_STATUS_READY = "ready"


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


def _path_is_within_root(candidate: Path, root: Path) -> bool:
    root = root.resolve()
    if root.is_file():
        return candidate == root
    return candidate == root or root in candidate.parents


def _path_is_within_any_root(candidate: Path, roots: List[Path]) -> bool:
    return any(_path_is_within_root(candidate, root) for root in roots)


def _relative_base() -> Path:
    """Where a relative path points: the installation's ``files/`` folder.

    The safeguard resolves the same way (FR-033), so the folder the owner is asked
    about is the folder the tool opens.
    """
    return folders.files_dir()


async def _get_effective_allowed_roots_with_status(
    ctx: Optional[Context] = None,
) -> tuple[List[Path], str]:
    """The folders a file tool may use in this call (FR-030/FR-031).

    ``files/downloads`` and ``files/outbox``, the owner's configured folders, the
    folders granted "always allow", and a folder the owner allowed for this call only.
    The MCP client's own roots grant nothing by themselves: a folder outside the
    project reaches a tool only through the owner's answer to the safeguard.
    """
    refresh_server_roots()
    for free in folders.free_folders():
        try:
            free.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            log_event(logging.WARNING, "could not create a files folder", error=error)
    return _dedupe_paths(folders.allowed_roots()), ROOTS_STATUS_READY


async def _get_effective_allowed_roots(ctx: Optional[Context] = None) -> List[Path]:
    roots, _ = await _get_effective_allowed_roots_with_status(ctx)
    return roots


async def _ensure_allowed_roots(
    ctx: Optional[Context], tool_name: str
) -> tuple[List[Path], Optional[str]]:
    roots, _ = await _get_effective_allowed_roots_with_status(ctx)
    if not roots:
        return [], f"{tool_name} has no usable folder: files/ could not be created."
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
        candidate = _relative_base() / candidate

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
            candidate = _relative_base() / candidate
    else:
        safe_name = Path(default_filename).name
        candidate = folders.default_download_dir() / safe_name

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


# The roots this process was STARTED with, kept apart from the ones the file
# names so a refresh can rebuild the list without losing them.
_CLI_ROOTS: list[str] = []

# The file's raw value as last APPLIED. A refresh that cannot tell 'unchanged'
# from 'never read' rebuilds on every file-tool call, and a rebuild discards
# whatever the list holds that did not come from argv or the file.
_last_named_roots: Optional[str] = None


def _roots_from_file() -> Optional[str]:
    """`TELEGRAM_FILE_ROOTS` as the configuration file currently spells it.

    Not `os.getenv`: `load_dotenv` copied the file's value into the process
    environment once, at startup, and never again - so reading the environment
    answers what the file said when the server booted, which is exactly the
    question a refresh is not asking. A value the PROCESS supplied still wins,
    because `settings` recorded that before the two were merged.
    """
    from telegram_mcp import account_snapshot
    from telegram_mcp.settings import PROCESS_FILE_ROOTS

    if PROCESS_FILE_ROOTS:
        return PROCESS_FILE_ROOTS
    return account_snapshot.file_value("TELEGRAM_FILE_ROOTS")


def _roots_from_file_quietly() -> Optional[str]:
    """The file's value, or None when it cannot be read. Startup only."""
    try:
        return _roots_from_file()
    except Exception:
        return None


def refresh_server_roots() -> bool:
    """Re-read the allow-list so a folder can be allowed without a restart.

    Returns whether the list changed. A file that cannot be read leaves the
    current roots exactly as they are and says nothing: this runs on the path of
    every file tool, and a `.env` being rewritten must not turn into a refusal
    for an operation that was already permitted.
    """
    global _last_named_roots
    try:
        named = _roots_from_file()
    except Exception:
        return False
    if named == _last_named_roots:
        # Nothing was edited, so nothing is rebuilt. This runs on the path of
        # every file tool, and a rebuild that fires regardless would discard any
        # root the list holds for a reason this function does not know about.
        return False
    _last_named_roots = named
    wanted: List[Path] = []
    for raw_root in list(_CLI_ROOTS) + [p for p in (named or "").split(os.pathsep) if p.strip()]:
        root = Path(raw_root).expanduser()
        if not root.exists():
            # A typo in a live edit is not a reason to drop the roots that work.
            log_event(
                logging.WARNING,
                "an allowed root named in the configuration does not exist; ignoring it",
                root=str(root),
            )
            continue
        wanted.append(root.resolve(strict=True))
    wanted = _dedupe_paths(wanted)
    if wanted == list(SERVER_ALLOWED_ROOTS):
        return False
    # In place: `runtime` and `main` hold this same list object.
    SERVER_ALLOWED_ROOTS[:] = wanted
    return True


def _roots_from_environment() -> List[str]:
    """Allowed roots named by ``TELEGRAM_FILE_ROOTS``, split on this OS's separator.

    The command line was the only way in, and it is the one route an MCP client
    config makes awkward: a client launches the server with its own argv and
    offers an ``env`` block beside it, so "start the server with directories as
    positional arguments" was advice a caller often could not take. The file
    tools then reported themselves disabled with a fix the operator could not
    apply, which is how `send_file` came to look broken rather than unconfigured.

    Same allow-list, same validation, same refusal of a path that does not exist
    - only a second way to say it. Empty entries are dropped so a trailing
    separator is not an error.
    """
    raw = os.getenv("TELEGRAM_FILE_ROOTS", "")
    return [part for part in raw.split(os.pathsep) if part.strip()]


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

    # Kept so `refresh_server_roots` can rebuild the list from the file without
    # losing what the command line named.
    _CLI_ROOTS[:] = list(parsed.allowed_roots)
    global _last_named_roots
    _last_named_roots = _roots_from_file_quietly()

    resolved_roots: List[Path] = []
    # Both sources, argv first so a command line stays the explicit override when
    # something sets the variable in the environment underneath it.
    for raw_root in list(parsed.allowed_roots) + _roots_from_environment():
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
    "ROOTS_STATUS_READY",
    "SERVER_ALLOWED_ROOTS",
    "_configure_allowed_roots_from_cli",
    "_roots_from_environment",
    "_contains_forbidden_path_patterns",
    "_dedupe_paths",
    "_ensure_allowed_roots",
    "_ensure_extension_allowed",
    "_ensure_size_within_limit",
    "_get_effective_allowed_roots",
    "_get_effective_allowed_roots_with_status",
    "_open_verified_directory",
    "_open_verified_source",
    "_path_is_within_any_root",
    "_path_is_within_root",
    "_resolve_readable_file_path",
    "_resolve_writable_file_path",
    "open_allowed_directory",
    "open_verified_file",
]
