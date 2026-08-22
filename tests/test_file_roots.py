"""File-path security: which roots are allowed, and what a caller's string resolves to.

Split out of `test_runtime.py` when the code did the same. The subject is
`telegram_mcp/file_roots.py`.

`SERVER_ALLOWED_ROOTS` is patched on `file_roots`, never on `runtime` or `main`: it is
rebound rather than mutated, and those two hold further names for the same list, so a
patch applied there is invisible to the code that reads it.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

import main
from telegram_mcp import file_roots, runtime


def test_path_helper_edges(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    file_root = root / "allowed.txt"
    file_root.write_text("ok", encoding="utf-8")

    assert runtime._dedupe_paths([root, root, file_root]) == [root, file_root]
    assert runtime._contains_forbidden_path_patterns("   ") == "Path must not be empty."
    assert "wildcard" in runtime._contains_forbidden_path_patterns("*.txt")
    assert runtime._contains_forbidden_path_patterns("safe/name.txt") is None
    with pytest.raises(ValueError, match="Unsupported root URI scheme"):
        runtime._coerce_root_uri_to_path("https://example.com/root")
    assert runtime._coerce_root_uri_to_path(root.as_uri()) == root.resolve()
    assert runtime._path_is_within_root(file_root.resolve(), file_root.resolve()) is True
    assert runtime._path_is_within_root(root.resolve(), file_root.resolve()) is False
    assert runtime._first_resolution_root([file_root.resolve()]) == root.resolve()
    assert runtime._ensure_extension_allowed("send_sticker", root / "bad.txt").startswith(
        "File extension is not allowed"
    )
    assert runtime._ensure_extension_allowed("send_file", root / "any.txt") is None

    too_big = root / "big.bin"
    too_big.write_bytes(b"12345")
    monkeypatch.setitem(runtime.MAX_FILE_BYTES, "tiny_tool", 4)
    assert runtime._ensure_size_within_limit("tiny_tool", too_big).startswith("File is too large")
    assert runtime._ensure_size_within_limit("unknown_tool", too_big) is None


@pytest.mark.asyncio
async def test_more_file_resolution_edges(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    file_path = nested / "file.txt"
    file_path.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await runtime._resolve_readable_file_path(
        raw_path="missing.txt", ctx=None, tool_name="send_file"
    )
    assert resolved is None
    assert error == "File not found: missing.txt"

    resolved, error = await runtime._resolve_readable_file_path(
        raw_path="nested", ctx=None, tool_name="send_file"
    )
    assert resolved is None
    assert "Path is not a file" in error

    out_path, error = await runtime._resolve_writable_file_path(
        raw_path="nested/out.bin",
        default_filename="ignored.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert error is None
    assert out_path == (root / "nested" / "out.bin").resolve()

    out_path, error = await runtime._resolve_writable_file_path(
        raw_path="../outside.bin",
        default_filename="ignored.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert out_path is None
    assert error == "Path traversal is not allowed."

    out_path, error = await runtime._resolve_writable_file_path(
        raw_path=str(tmp_path / "outside.bin"),
        default_filename="ignored.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert out_path is None
    assert error == "Path is outside allowed roots."


def test_roots_unsupported_detection():
    assert runtime._is_roots_unsupported_error(NotImplementedError()) is True
    assert runtime._is_roots_unsupported_error(AttributeError("missing list_roots")) is True
    assert runtime._is_roots_unsupported_error(AttributeError("other")) is False
    assert (
        runtime._is_roots_unsupported_error(
            McpError(ErrorData(code=-32000, message="not implemented"))
        )
        is True
    )
    assert runtime._is_roots_unsupported_error(RuntimeError("boom")) is False


def test_configure_allowed_roots_from_cli_updates_runtime_and_main_alias(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    runtime._configure_allowed_roots_from_cli([str(root), str(root)])
    assert runtime.SERVER_ALLOWED_ROOTS == [root.resolve()]

    main._configure_allowed_roots_from_cli([str(root)])
    assert main.SERVER_ALLOWED_ROOTS == [root.resolve()]

    with pytest.raises(SystemExit, match="Allowed root does not exist"):
        runtime._configure_allowed_roots_from_cli([str(tmp_path / "missing")])


def test_main_compatibility_wrappers_are_exported():
    assert main.send_message is not None
    assert main.validate_id is runtime.validate_id
    assert main.log_file_path.endswith("mcp_errors.log")


class _FakeRootsSession:
    def __init__(self, roots):
        self._roots = roots

    async def list_roots(self):
        return SimpleNamespace(roots=list(self._roots))


def _ctx_with_roots(roots):
    return SimpleNamespace(session=_FakeRootsSession(roots))


def test_server_roots_fallback_enabled_parsing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)
    assert runtime._server_roots_fallback_enabled() is False
    assert runtime._server_roots_fallback_enabled("1") is True
    assert runtime._server_roots_fallback_enabled("true") is True
    assert runtime._server_roots_fallback_enabled("off") is False
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "yes")
    assert runtime._server_roots_fallback_enabled() is True


@pytest.mark.asyncio
async def test_empty_client_roots_denies_by_default(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root.resolve()])
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)

    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx_with_roots([]))
    assert roots == []
    assert status == runtime.ROOTS_STATUS_CLIENT_DENY_ALL


@pytest.mark.asyncio
async def test_empty_client_roots_falls_back_to_server_when_enabled(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")

    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx_with_roots([]))
    assert roots == [root.resolve()]
    assert status == runtime.ROOTS_STATUS_SERVER_FALLBACK

    # _ensure_allowed_roots must accept the fallback roots without an error.
    resolved, error = await runtime._ensure_allowed_roots(_ctx_with_roots([]), "download_media")
    assert error is None
    assert resolved == [root.resolve()]


@pytest.mark.asyncio
async def test_empty_client_roots_fallback_noop_without_server_roots(monkeypatch):
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")

    roots, status = await runtime._get_effective_allowed_roots_with_status(_ctx_with_roots([]))
    assert roots == []
    assert status == runtime.ROOTS_STATUS_CLIENT_DENY_ALL


class _FailingRootsSession:
    def __init__(self, error: Exception):
        self._error = error

    async def list_roots(self):
        raise self._error


def _ctx_with_list_roots_error(error: Exception):
    return SimpleNamespace(session=_FailingRootsSession(error))


def test_coerce_paths_from_list_roots_validation_error_recovers_bare_paths(tmp_path):
    """Cursor-style bare absolute paths appear as pydantic url_parsing inputs."""
    from pydantic import ValidationError
    from mcp.types import ListRootsResult

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate(
            {
                "roots": [
                    {"uri": str(root_a)},
                    {"uri": str(root_b)},
                    {"uri": "not-a-path"},
                ]
            }
        )

    recovered = runtime._coerce_paths_from_list_roots_validation_error(exc_info.value)
    assert root_a.resolve() in recovered
    assert root_b.resolve() in recovered


def test_coerce_paths_from_list_roots_validation_error_recovers_windows_paths():
    """A Windows drive letter is reported as url_scheme, not url_parsing.

    ``C:\\Users\\dev\\workspace`` gets far enough through pydantic's URL parsing
    for the drive letter to be taken as the scheme, so validation fails with
    ``url_scheme``. The path is hardcoded rather than derived from ``tmp_path``
    so this case is exercised on POSIX CI as well as on Windows.
    """
    from pydantic import ValidationError
    from mcp.types import ListRootsResult

    windows_root = r"C:\Users\dev\workspace"

    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate({"roots": [{"uri": windows_root}]})

    assert any(item.get("type") == "url_scheme" for item in exc_info.value.errors())

    recovered = runtime._coerce_paths_from_list_roots_validation_error(exc_info.value)
    assert recovered == [Path(windows_root).expanduser().resolve()]


@pytest.mark.asyncio
async def test_list_roots_validation_error_recovers_client_paths(tmp_path, monkeypatch):
    from pydantic import ValidationError
    from mcp.types import ListRootsResult

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [])
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        ListRootsResult.model_validate({"roots": [{"uri": str(root)}]})

    roots, status = await runtime._get_effective_allowed_roots_with_status(
        _ctx_with_list_roots_error(exc_info.value)
    )
    assert status == runtime.ROOTS_STATUS_READY
    assert roots == [root.resolve()]

    resolved, error = await runtime._ensure_allowed_roots(
        _ctx_with_list_roots_error(exc_info.value), "download_media"
    )
    assert error is None
    assert resolved == [root.resolve()]


@pytest.mark.asyncio
async def test_list_roots_unexpected_error_falls_back_when_opt_in(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root.resolve()])
    monkeypatch.setenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", "1")

    roots, status = await runtime._get_effective_allowed_roots_with_status(
        _ctx_with_list_roots_error(RuntimeError("boom"))
    )
    assert status == runtime.ROOTS_STATUS_SERVER_FALLBACK
    assert roots == [root.resolve()]


@pytest.mark.asyncio
async def test_list_roots_unexpected_error_denies_without_opt_in(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root.resolve()])
    monkeypatch.delenv("TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK", raising=False)

    roots, status = await runtime._get_effective_allowed_roots_with_status(
        _ctx_with_list_roots_error(RuntimeError("boom"))
    )
    assert status == runtime.ROOTS_STATUS_ERROR
    assert roots == []
