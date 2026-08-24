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

from telegram_mcp import file_roots
from telegram_mcp.tools import media as mod


class _Client:
    """Writes a real file, the way Telethon does, so the cleanup can be observed."""

    def __init__(self, advertised_size=None, streamed=None, explode=None, returns=None):
        self.calls = []
        self.advertised_size = advertised_size
        # Byte counts to feed the progress callback, as Telethon does per chunk.
        self.streamed = streamed
        self.explode = explode
        self.returns = returns

    async def get_messages(self, entity, ids=None):
        return SimpleNamespace(
            id=ids,
            media=object(),
            file=SimpleNamespace(size=self.advertised_size, ext=".jpg"),
        )

    async def download_media(self, message, file=None, progress_callback=None):
        self.calls.append(file)
        # Telethon appends the extension it detects from the content, so the file
        # that lands is not the path the pre-flight check validated.
        written = Path(str(file) + ".jpg")
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"x")
        for received in self.streamed or []:
            written.write_bytes(b"x" * received)
            if progress_callback:
                progress_callback(received, self.advertised_size or 0)
        if self.explode:
            raise self.explode
        return str(written) if self.returns is None else self.returns


@pytest.fixture
def _wire(monkeypatch, tmp_path):
    """Wire the tool at the seams, and hand back the path Telethon will write."""

    def wire(roots_error=None, within_roots=True, client=None):
        client = client or _Client()
        monkeypatch.setattr(mod, "get_client", lambda account=None: client)

        async def _resolve_entity(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
            return (tmp_path / (raw_path or default_filename)), None

        # A root that does not contain the destination, rather than a stubbed
        # containment predicate: the refusal now comes out of the real handle
        # gate, so the test has to give it a real reason to refuse.
        allowed = [tmp_path] if within_roots else [tmp_path / "elsewhere"]

        async def _roots(ctx, tool_name):
            return (None, roots_error) if roots_error else (allowed, None)

        monkeypatch.setattr(mod, "resolve_entity", _resolve_entity)
        monkeypatch.setattr(mod, "_resolve_writable_file_path", _resolve_path)
        # Patched where the gate reads it: `_open_verified_directory` lives in
        # file_roots and calls this by its own module name.
        monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)
        return client

    return wire


def _written_file(tmp_path):
    return next(tmp_path.glob("*.jpg"), None)


def _leftovers(tmp_path):
    """Everything in the download directory, so a stray part file is visible."""
    return sorted(p.name for p in tmp_path.rglob("*") if p.is_file())


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
    assert _leftovers(tmp_path) == [written.name], "a partial file was left behind"


# --- a size cap, and nothing half-written ----------------------------------


@pytest.mark.asyncio
async def test_a_file_larger_than_the_cap_is_refused_before_anything_downloads(_wire, tmp_path):
    """Telegram advertises the size up front; a 3GB file does not need to reach the
    disk before the tool notices."""
    client = _wire(client=_Client(advertised_size=3 * 1024**3))

    result = await mod.download_media(1, 5, account="a")

    assert "max_bytes" in result
    assert client.calls == [], "the oversized download started anyway"
    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_a_stream_that_outgrows_the_cap_is_stopped_and_cleaned_up(_wire, tmp_path):
    """A size the server understates has to be caught while the bytes arrive."""
    _wire(client=_Client(advertised_size=10, streamed=[10, 5000]))

    result = await mod.download_media(1, 5, max_bytes=1000, account="a")

    assert "max_bytes" in result or "larger" in result
    assert _leftovers(tmp_path) == [], "the over-cap partial file survived"


@pytest.mark.asyncio
async def test_an_exception_after_a_partial_write_leaves_no_partial_file(_wire, tmp_path):
    """Telethon raising mid-download used to leave the bytes it had already written,
    under a name the caller was never told."""
    _wire(client=_Client(explode=OSError("connection reset")))

    result = await mod.download_media(1, 5, account="a")

    assert "error" in result.lower()
    assert _leftovers(tmp_path) == [], "a partial download survived the failure"


@pytest.mark.asyncio
async def test_a_cancelled_download_leaves_no_partial_file(_wire, tmp_path):
    """Cancellation is not an error path the generic handler sees; it still has to
    take the partial file with it."""
    import asyncio

    _wire(client=_Client(explode=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await mod.download_media(1, 5, account="a")

    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_a_download_that_returns_nothing_leaves_no_partial_file(_wire, tmp_path):
    _wire(client=_Client(returns=""))

    result = await mod.download_media(1, 5, account="a")

    assert "failed" in result.lower()
    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_an_existing_file_is_not_overwritten(_wire, tmp_path):
    """The default name was second-precision, so two downloads in the same second
    silently replaced each other."""
    (tmp_path / "keep.jpg").write_bytes(b"original")
    _wire()

    result = await mod.download_media(1, 5, file_path="keep.jpg", account="a")

    assert (tmp_path / "keep.jpg").read_bytes() == b"original", "the existing file was replaced"
    assert (tmp_path / "keep-1.jpg").read_bytes() == b"x", "the download did not land beside it"
    assert "keep-1.jpg" in result
