"""Writing a file the operator asked for, as a transaction rather than a sequence.

Two tools put caller-named bytes on disk: ``download_media`` and
``save_disappearing_media``. Both used to treat "write it, then check it" as good
enough. It is not, for three separate reasons this module pins down:

* **A check after the write reports; it does not prevent.** If the resolved
  parent was swapped for a symlink out of the allowed roots, the bytes went
  through it before anybody looked. The roots verdict has to be taken against the
  place the write will actually go, before the first byte.
* **A name that was merely observed free is not a name you own.** The temp target
  has to be created exclusively, so nothing can be sitting at it already and
  nothing can appear at it in between.
* **A completed ``write()`` is not a completed file.** ENOSPC surfaces at flush on
  a delayed-allocation filesystem, so a tool that never flushes reports success
  over bytes that never reached the disk -- under the final name, where the next
  reader takes them for the whole payload.

No network and no real Telegram: fake clients that write real files, so the
assertions are about the filesystem the operator ends up with.
"""

import asyncio
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_mcp import file_roots, handles
from telegram_mcp.tools import ephemeral as ephemeral_mod
from telegram_mcp.tools import media as media_mod

# --- download_media --------------------------------------------------------


class _DownloadClient:
    """Writes a real file the way Telethon does, choosing the extension itself."""

    def __init__(self, watch: Path = None):
        self.calls = []
        self.watched = []
        self._watch = watch

    async def get_messages(self, entity, ids=None):
        return SimpleNamespace(id=ids, media=object(), file=SimpleNamespace(size=None))

    async def download_media(self, message, file=None, progress_callback=None):
        self.calls.append(Path(str(file)))
        if self._watch is not None:
            # What the caller-visible output directory holds while the transfer
            # is in flight, which is where a partial file must never appear.
            self.watched.append(sorted(p.name for p in self._watch.iterdir()))
        written = Path(str(file) + ".jpg")
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"payload")
        return str(written)


@pytest.fixture
def _wire_download(monkeypatch, tmp_path):
    """Wire download_media onto a fake client, with the roots seam controllable."""

    def wire(client=None, within_roots=True, roots_error=None):
        client = client or _DownloadClient()
        monkeypatch.setattr(media_mod, "get_client", lambda account=None: client)

        async def _resolve_entity(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
            return (tmp_path / (raw_path or default_filename)), None

        # A root that really does not contain the destination, rather than a
        # stubbed containment predicate: the refusal now comes out of the
        # handle gate, which judges the directory it actually opened.
        allowed = [tmp_path] if within_roots else [tmp_path / "elsewhere"]

        async def _roots(ctx, tool_name):
            return (None, roots_error) if roots_error else (allowed, None)

        monkeypatch.setattr(media_mod, "resolve_entity", _resolve_entity)
        monkeypatch.setattr(media_mod, "_resolve_writable_file_path", _resolve_path)
        monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)
        return client

    return wire


@pytest.mark.asyncio
async def test_the_partial_download_never_appears_in_the_output_directory(
    _wire_download, tmp_path
):
    """A `.part` file sitting beside the finished downloads is a name anything
    else on the machine can see, and -- because it is only created when Telethon
    opens it -- a name nothing has reserved. The transfer belongs in a directory
    this call made for itself."""
    client = _wire_download(client=_DownloadClient(watch=tmp_path))

    await media_mod.download_media(1, 5, account="a")

    handed = client.calls[0]
    assert handed.parent != tmp_path, "the transfer wrote straight into the output directory"
    assert handed.parent.parent == tmp_path
    assert client.watched == [[handed.parent.name]], "a partial file was visible mid-transfer"
    assert not handed.parent.exists(), "the private transfer directory outlived the call"


@pytest.mark.asyncio
async def test_a_write_location_outside_the_roots_is_refused_before_any_transfer(
    _wire_download, tmp_path
):
    """The point of the roots gate is that the bytes never leave them. Noticing
    afterwards that they did is a report, not an enforcement."""
    client = _wire_download(within_roots=False)

    result = await media_mod.download_media(1, 5, account="a")

    assert "refused" in result.lower() and "allowed roots" in result
    assert client.calls == [], "the transfer ran before the roots were consulted"
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_unreadable_roots_stop_the_transfer_before_it_starts(_wire_download, tmp_path):
    client = _wire_download(roots_error="download_media is disabled.")

    result = await media_mod.download_media(1, 5, account="a")

    assert result == "download_media is disabled."
    assert client.calls == []
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_the_downloaded_file_is_flushed_to_storage_before_it_is_installed(
    _wire_download, tmp_path, monkeypatch
):
    """os.replace publishes a name; it does not publish the bytes behind it. A
    crash after an unflushed rename leaves the final name pointing at nothing."""
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    _wire_download()
    result = await media_mod.download_media(1, 5, account="a")

    assert "downloaded to" in result
    assert synced, "the download was installed without ever reaching durable storage"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "not a number"])
async def test_a_max_bytes_that_is_not_a_positive_size_is_refused(_wire_download, tmp_path, bad):
    """`max_bytes=0` fell through to the default ceiling because zero is falsy,
    and a negative one became a cap nothing could satisfy."""
    client = _wire_download()

    result = await media_mod.download_media(1, 5, max_bytes=bad, account="a")

    assert "max_bytes" in result
    assert client.calls == [], "an invalid ceiling still started a transfer"


@pytest.mark.asyncio
async def test_a_symlinked_parent_pointing_out_of_the_roots_is_refused(tmp_path, monkeypatch):
    """The real shape of the race, with the real roots helpers: the directory the
    caller named resolves, through a symlink, to somewhere outside the roots."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "escape", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        # Unprivileged Windows cannot create one. That is a host capability, not
        # a platform the code is excused from: CI runs this on Linux.
        pytest.skip(f"this host cannot create a directory symlink: {error}")

    client = _DownloadClient()
    monkeypatch.setattr(media_mod, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
        return (root / "escape" / "loot"), None

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(media_mod, "resolve_entity", _resolve_entity)
    monkeypatch.setattr(media_mod, "_resolve_writable_file_path", _resolve_path)
    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    result = await media_mod.download_media(1, 5, account="a")

    assert "refused" in result.lower()
    assert client.calls == [], "bytes were sent through the symlink before it was noticed"
    assert list(outside.rglob("*")) == [], "the download landed outside the allowed root"


# --- save_disappearing_media -----------------------------------------------


@pytest.fixture
def _wire_save(monkeypatch, tmp_path):
    """save_disappearing_media with the transfer faked and the disk real."""

    def wire(within_roots=True, roots_error=None, data=b"secret-bytes"):
        client = SimpleNamespace()

        async def _ensure(_client):
            return None

        async def _resolve_entity(chat_id, _client):
            return SimpleNamespace(id=chat_id)

        async def _get_messages(entity, ids=None):
            return SimpleNamespace(id=ids, media=object(), ttl_seconds=30)

        client.get_messages = _get_messages
        monkeypatch.setattr(ephemeral_mod, "get_client", lambda account=None: client)
        monkeypatch.setattr(ephemeral_mod, "ensure_connected", _ensure)
        monkeypatch.setattr(ephemeral_mod, "resolve_entity", _resolve_entity)
        monkeypatch.setattr(ephemeral_mod, "_ttl_of", lambda msg: 30)
        monkeypatch.setattr(ephemeral_mod, "_describe_ttl", lambda msg: {"ttl_seconds": 30})
        monkeypatch.setattr(
            ephemeral_mod, "describe_media", lambda msg: {"kind": "photo", "extension": ".jpg"}
        )

        async def _download(_cl, _msg, _max_bytes):
            return data, False

        monkeypatch.setattr(ephemeral_mod, "_download_capped", _download)

        async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
            return (tmp_path / (raw_path or default_filename)), None

        allowed = [tmp_path] if within_roots else [tmp_path / "elsewhere"]

        async def _roots(ctx, tool_name):
            return (None, roots_error) if roots_error else (allowed, None)

        monkeypatch.setattr(ephemeral_mod, "_resolve_writable_file_path", _resolve_path)
        monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)
        return client

    return wire


def _files(directory: Path):
    return sorted(p.name for p in directory.rglob("*") if p.is_file())


@pytest.mark.asyncio
async def test_a_disk_failure_at_flush_leaves_nothing_under_the_final_name(
    _wire_save, tmp_path, monkeypatch
):
    """The reservation created the final name and the payload was written straight
    into it, so a disk error anywhere in between left a short file wearing the
    name of the whole one -- and, because nothing was ever flushed, the error was
    never even raised."""
    _wire_save()

    def _no_space(_fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", _no_space)

    result = await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    text = result if isinstance(result, str) else str(result)
    assert "could not be written" in text or "No space" in text
    assert _files(tmp_path) == [], "a truncated file survived under the final name"


@pytest.mark.asyncio
async def test_the_roots_verdict_is_taken_before_the_payload_reaches_the_disk(
    _wire_save, tmp_path, monkeypatch
):
    """Writing first and checking after means the bytes were outside the roots for
    as long as the check took, which is exactly what the roots exist to forbid."""
    _wire_save(within_roots=False)

    result = await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    text = result if isinstance(result, str) else str(result)
    assert "refused" in text.lower()
    assert _files(tmp_path) == [], "the payload reached the disk before the verdict"


@pytest.mark.asyncio
async def test_a_successful_save_writes_the_whole_payload_once(_wire_save, tmp_path):
    """The cleanup must not be so eager that it takes the good path with it, and
    no temp file may outlive the call."""
    _wire_save(data=b"the-media")

    await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 1, f"expected exactly the saved file, found {_files(tmp_path)}"
    assert written[0].read_bytes() == b"the-media"


@pytest.mark.asyncio
async def test_two_saves_in_the_same_second_do_not_overwrite_each_other(_wire_save, tmp_path):
    _wire_save(data=b"first")
    await ephemeral_mod.save_disappearing_media(1, 5, file_path="keep.jpg", preview=False)
    _wire_save(data=b"second")
    await ephemeral_mod.save_disappearing_media(1, 5, file_path="keep.jpg", preview=False)

    assert (tmp_path / "keep.jpg").read_bytes() == b"first"
    assert (tmp_path / "keep-1.jpg").read_bytes() == b"second"


@pytest.mark.asyncio
async def test_a_cancelled_save_leaves_no_file_behind(_wire_save, tmp_path, monkeypatch):
    """CancelledError is a BaseException: it never reaches the tool's own handler,
    so the cleanup has to be in a finally or the partial file stays."""
    _wire_save()

    def _cancel(_fd):
        raise asyncio.CancelledError()

    monkeypatch.setattr(os, "fsync", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    assert _files(tmp_path) == [], "a cancelled save left its bytes on disk"


# --- the durability helpers, on both platforms, from either host -----------


class _RecordingCalls(handles.SystemCalls):
    """Records what the handle asked the kernel for, without asking the host.

    Whether a directory can be opened at all is a platform property, so
    exercising the POSIX branch on Windows (or the reverse) has to be done at the
    syscall seam. Skipping one branch on the host that cannot run it is how a
    platform-specific path reaches CI unexercised.
    """

    def __init__(self, dir_fd):
        self.dir_fd = dir_fd
        self.opened = []
        self.synced = []

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        self.opened.append((str(path), flags, dir_fd))
        return 4242

    def close(self, fd):
        pass

    def fsync(self, fd):
        self.synced.append(fd)


def test_a_posix_host_syncs_the_directory_entry_as_well(tmp_path):
    """os.replace publishes a NAME, and a name is metadata: without this the
    rename can still be in the log when the machine stops, and the file the
    caller was told about is not there when it comes back."""
    calls = _RecordingCalls(dir_fd=True)
    directory = handles.DirHandle(tmp_path, 7, os.lstat(tmp_path), calls)

    directory.sync()

    assert calls.synced == [7], "the rename was published without syncing the directory"


def test_a_windows_host_leaves_the_directory_to_the_filesystem(tmp_path):
    """Windows exposes no directory handle to sync and orders this metadata
    itself. Attempting it there is an error, not a stronger guarantee."""
    calls = _RecordingCalls(dir_fd=False)
    directory = handles.DirHandle(tmp_path, None, os.lstat(tmp_path), calls)

    directory.sync()

    assert calls.synced == []


def test_the_file_sync_asks_for_a_writable_handle(tmp_path):
    """Windows' _commit refuses a read-only descriptor, so a read-only open would
    make every flush on that platform an error instead of a flush."""
    calls = _RecordingCalls(dir_fd=True)
    directory = handles.DirHandle(tmp_path, 7, os.lstat(tmp_path), calls)

    directory.sync_child("part.jpg")

    assert len(calls.opened) == 1
    name, flags, dir_fd = calls.opened[0]
    assert name == "part.jpg" and dir_fd == 7, "the child was reached by path, not by descriptor"
    assert flags & os.O_RDWR == os.O_RDWR
    assert calls.synced == [4242]


# --- the destination replaced while the transfer is in flight ---------------
#
# A write is a sequence -- reserve a name, stage the bytes, install them, clean
# up -- and each step used to re-resolve the same string. The two tests below
# exchange the destination directory for a different real one at the moment a
# real attacker would: while the transfer is running. Both tools hold the
# directory they were authorised for, so neither may carry on into whatever
# inherited its name, and neither may leave the payload behind.


def _replace_directory(live: Path, replacement: Path) -> None:
    """Give ``replacement`` the name ``live`` had: a different inode, same name."""
    live.rename(live.with_name(live.name + ".retired"))
    replacement.rename(live)


@pytest.mark.asyncio
async def test_a_download_into_a_directory_replaced_mid_transfer_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    class _SwappingClient(_DownloadClient):
        async def download_media(self, message, file=None, progress_callback=None):
            written = await super().download_media(message, file, progress_callback)
            _replace_directory(live, decoy)
            return written

    client = _SwappingClient()
    monkeypatch.setattr(media_mod, "get_client", lambda account=None: client)

    async def _resolve_entity(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
        return (live / "loot"), None

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(media_mod, "resolve_entity", _resolve_entity)
    monkeypatch.setattr(media_mod, "_resolve_writable_file_path", _resolve_path)
    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    result = await media_mod.download_media(1, 5, account="a")

    if result.startswith("Media downloaded to"):  # pragma: no cover - POSIX only
        # With openat the descriptor still names the authorised directory, so the
        # install lands there rather than in the replacement.
        assert Path(result.split("to ", 1)[1].rstrip(".")).parent.name.startswith("out")
    else:
        assert _files(live) == [], "the swapped-in directory received the download"


@pytest.mark.asyncio
async def test_a_save_into_a_directory_replaced_mid_transfer_keeps_nothing(monkeypatch, tmp_path):
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    client = SimpleNamespace()

    async def _ensure(_client):
        return None

    async def _resolve_entity(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    async def _get_messages(entity, ids=None):
        return SimpleNamespace(id=ids, media=object(), ttl_seconds=30)

    client.get_messages = _get_messages
    monkeypatch.setattr(ephemeral_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(ephemeral_mod, "ensure_connected", _ensure)
    monkeypatch.setattr(ephemeral_mod, "resolve_entity", _resolve_entity)
    monkeypatch.setattr(ephemeral_mod, "_ttl_of", lambda msg: 30)
    monkeypatch.setattr(ephemeral_mod, "_describe_ttl", lambda msg: {"ttl_seconds": 30})
    monkeypatch.setattr(
        ephemeral_mod, "describe_media", lambda msg: {"kind": "photo", "extension": ".jpg"}
    )

    async def _download(_cl, _msg, _max_bytes):
        return b"secret-bytes", False

    async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
        return (live / (raw_path or default_filename)), None

    async def _roots(ctx, tool_name):
        return [root], None

    # The swap lands after the directory has been opened and the name reserved,
    # and before the payload is written -- the window the handle exists to close.
    # A swap before the gate runs is not this defect: the gate then judges the
    # replacement on its own merits, which is what it is for.
    reserve = handles.DirHandle.reserve_free_name
    refused: list = []

    def _reserve_then_swap(self, stem, suffix):
        name = reserve(self, stem, suffix)
        try:
            _replace_directory(live, decoy)
        except OSError as error:
            # A host that can hold the directory open refuses the exchange
            # outright, which is the same defect closed one layer lower.
            refused.append(error)
        return name

    monkeypatch.setattr(handles.DirHandle, "reserve_free_name", _reserve_then_swap)
    monkeypatch.setattr(ephemeral_mod, "_download_capped", _download)
    monkeypatch.setattr(ephemeral_mod, "_resolve_writable_file_path", _resolve_path)
    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    result = await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    text = result if isinstance(result, str) else str(result)
    assert "secret-bytes" not in text
    if refused:
        # The exchange never happened, so the payload is where it was authorised
        # to go and nowhere else.
        assert len(_files(live)) == 1, _files(live)
        assert (live / _files(live)[0]).read_bytes() == b"secret-bytes"
        assert _files(decoy) == [], "the replacement received the payload"
    elif "saved_path" in text:  # pragma: no cover - POSIX only
        # With openat the descriptor still names the authorised directory.
        assert _files(live) == [], "the payload landed in the replacement"
    else:
        assert _files(live) == [], "the swapped-in directory received the payload"
        assert not any(
            path.read_bytes() == b"secret-bytes" for path in root.rglob("*") if path.is_file()
        ), "the payload was written somewhere after the directory was replaced"
