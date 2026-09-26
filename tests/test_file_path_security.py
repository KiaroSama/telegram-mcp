from pathlib import Path

import pytest

# Patched and called on the module that OWNS these names. `main` used to keep
# copying wrappers so a test could reach them through it; the wrappers existed
# only for this file and one other, and carried a rebind hazard in production
# code to serve a test seam.
from telegram_mcp import file_roots


@pytest.mark.asyncio
async def test_readable_relative_path_resolves_inside_files(_folders_are_the_tests_own):
    # FR-033: a relative path starts in the installation's files/ folder, exactly
    # where the safeguard looked when it judged it.
    target = _folders_are_the_tests_own / "files" / "outbox" / "document.txt"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")

    resolved, error = await file_roots._resolve_readable_file_path(
        raw_path="outbox/document.txt",
        ctx=None,
        tool_name="send_file",
    )

    assert error is None
    assert resolved == target.resolve()


@pytest.mark.asyncio
async def test_readable_path_rejects_traversal(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await file_roots._resolve_readable_file_path(
        raw_path="../etc/passwd",
        ctx=None,
        tool_name="send_file",
    )

    assert resolved is None
    assert error == "Path traversal is not allowed."


@pytest.mark.asyncio
async def test_readable_path_rejects_outside_root(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    outside_root = (tmp_path / "outside").resolve()
    root.mkdir(parents=True)
    outside_root.mkdir(parents=True)

    outside_file = outside_root / "outside.txt"
    outside_file.write_text("no", encoding="utf-8")

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await file_roots._resolve_readable_file_path(
        raw_path=str(outside_file),
        ctx=None,
        tool_name="send_file",
    )

    assert resolved is None
    assert error == "Path is outside allowed roots."


@pytest.mark.asyncio
async def test_writable_default_path_is_files_downloads(_folders_are_the_tests_own):
    resolved, error = await file_roots._resolve_writable_file_path(
        raw_path=None,
        default_filename="example.bin",
        ctx=None,
        tool_name="download_media",
    )

    assert error is None
    downloads = (_folders_are_the_tests_own / "files" / "downloads").resolve()
    assert resolved == downloads / "example.bin"
    # A path nested below it answers a question about a string and creates nothing;
    # only the two fixed files/ folders are made up front. Deeper directories are
    # built by the handle gate, one component at a time.
    nested, error = await file_roots._resolve_writable_file_path(
        raw_path="downloads/2026/x.bin",
        default_filename="ignored.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert error is None
    assert not nested.parent.exists()

    async with file_roots._open_verified_directory(
        path=nested.parent, ctx=None, tool_name="download_media"
    ) as (directory, dir_error):
        assert dir_error is None
        assert nested.parent.is_dir()
        assert Path(directory.path) == nested.parent


@pytest.mark.asyncio
async def test_extension_allowlist_is_enforced_for_sticker(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    file_path = root / "sticker.txt"
    file_path.write_text("bad", encoding="utf-8")

    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root])

    resolved, error = await file_roots._resolve_readable_file_path(
        raw_path=str(file_path),
        ctx=None,
        tool_name="send_sticker",
    )

    assert resolved is None
    assert error is not None
    assert "extension is not allowed" in error
