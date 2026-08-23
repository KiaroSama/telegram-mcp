"""Downloading media: what survives on disk when the placement is refused.

download_media hands Telethon a stem and lets it pick the extension, so the file
that actually appears is not the path the pre-flight gate validated. That is why
the roots check runs again *after* the write — and why every refusing return has
to take the file with it. A refusal that leaves the bytes outside the operator's
allowed roots has enforced nothing.

The assertions here are about the filesystem, not the returned string: the tool
already returns a refusal today, so only "the file is gone" tells fixed from
broken.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_mcp.tools import media as mod


class _Client:
    """Writes a real file, the way Telethon does, so the cleanup can be observed."""

    def __init__(self):
        self.calls = []

    async def get_messages(self, entity, ids=None):
        return SimpleNamespace(id=ids, media=object())

    async def download_media(self, message, file=None):
        self.calls.append(file)
        # Telethon appends the extension it detects from the content, so the file
        # that lands is not the path the pre-flight check validated.
        written = Path(str(file) + ".jpg")
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"x")
        return str(written)


@pytest.fixture
def _wire(monkeypatch, tmp_path):
    """Wire the tool at the seams, and hand back the path Telethon will write."""

    def wire(roots_error=None, within_roots=True):
        client = _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve_entity(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
            return (tmp_path / (raw_path or default_filename)), None

        async def _roots(ctx, tool_name):
            return (None, roots_error) if roots_error else ([tmp_path], None)

        monkeypatch.setattr(mod, "resolve_entity", _resolve_entity)
        monkeypatch.setattr(mod, "_resolve_writable_file_path", _resolve_path)
        monkeypatch.setattr(mod, "_ensure_allowed_roots", _roots)
        monkeypatch.setattr(mod, "_path_is_within_any_root", lambda path, roots: within_roots)
        return client

    return wire


def _written_file(tmp_path):
    return next(tmp_path.glob("*.jpg"), None)


@pytest.mark.asyncio
async def test_a_download_refused_by_the_root_check_leaves_nothing_on_disk(_wire, tmp_path):
    """The refusal is the whole point of the gate; the bytes outlasting it are not."""
    _wire(within_roots=False)

    result = await mod.download_media(1, 5, account="a")

    assert "refused" in result.lower()
    assert "allowed roots" in result
    assert _written_file(tmp_path) is None, "the refused download is still on disk"


@pytest.mark.asyncio
async def test_a_download_that_cannot_read_the_roots_leaves_nothing_on_disk(_wire, tmp_path):
    """An unreadable roots configuration is a refusal too, and the file has already
    been written by the time it is discovered."""
    _wire(roots_error="Path is outside allowed roots.")

    result = await mod.download_media(1, 5, account="a")

    assert result == "Path is outside allowed roots."
    assert _written_file(tmp_path) is None, "the refused download is still on disk"


@pytest.mark.asyncio
async def test_an_accepted_download_keeps_the_file_and_reports_its_path(_wire, tmp_path):
    """A cleanup applied too eagerly would delete every successful download and the
    refusal tests above would not notice."""
    _wire()

    result = await mod.download_media(1, 5, account="a")

    written = _written_file(tmp_path)
    assert written is not None, "the accepted download was deleted"
    assert written.read_bytes() == b"x"
    assert str(written.resolve()) in result
