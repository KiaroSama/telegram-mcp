"""Compatibility entrypoint for the Telegram MCP server.

The implementation lives in the telegram_mcp package. This module keeps the
historic `main` import path and console script target working.
"""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

from telegram_mcp import file_roots as _file_roots
from telegram_mcp import runtime as _runtime  # noqa: F401  (historic `main._runtime`)
from telegram_mcp.runtime import *
from telegram_mcp.runner import _main, main
from telegram_mcp.tools import *

# Backward-compatible alias for callers/tests that monkeypatch main.SERVER_ALLOWED_ROOTS.
# The roots subsystem lives in `telegram_mcp.file_roots`, and `SERVER_ALLOWED_ROOTS` is
# REBOUND rather than mutated - so a patch applied here is a second name for the old
# list and is invisible to the code that reads it. These wrappers exist to copy it back
# onto the owning module before delegating, which is what keeps the historic seam honest.
SERVER_ALLOWED_ROOTS = _file_roots.SERVER_ALLOWED_ROOTS


def _sync_runtime_roots() -> None:
    # Copy the CONTENTS, never rebind. A rebind here outlives the test that caused it:
    # `monkeypatch` restores `main.SERVER_ALLOWED_ROOTS` but knows nothing about a name
    # this function assigned on another module, so the two would stay permanently
    # different objects and every later reader would follow the wrong one.
    _file_roots.SERVER_ALLOWED_ROOTS[:] = SERVER_ALLOWED_ROOTS


async def _get_effective_allowed_roots(ctx):
    _sync_runtime_roots()
    return await _file_roots._get_effective_allowed_roots(ctx)


async def _get_effective_allowed_roots_with_status(ctx):
    _sync_runtime_roots()
    return await _file_roots._get_effective_allowed_roots_with_status(ctx)


async def _ensure_allowed_roots(ctx, tool_name):
    _sync_runtime_roots()
    return await _file_roots._ensure_allowed_roots(ctx, tool_name)


async def _resolve_readable_file_path(*, raw_path, ctx, tool_name):
    _sync_runtime_roots()
    return await _file_roots._resolve_readable_file_path(
        raw_path=raw_path,
        ctx=ctx,
        tool_name=tool_name,
    )


async def _resolve_writable_file_path(*, raw_path, default_filename, ctx, tool_name):
    _sync_runtime_roots()
    return await _file_roots._resolve_writable_file_path(
        raw_path=raw_path,
        default_filename=default_filename,
        ctx=ctx,
        tool_name=tool_name,
    )


def _configure_allowed_roots_from_cli(argv=None) -> None:
    _runtime._configure_allowed_roots_from_cli(argv)
    globals()["SERVER_ALLOWED_ROOTS"] = _runtime.SERVER_ALLOWED_ROOTS


if __name__ == "__main__":
    main()
