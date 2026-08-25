"""Authorisation that holds the object, proved against the filesystem itself.

``tests/test_handle_binding.py`` drives the decision logic through an injected
``SystemCalls``, which is the only way to sequence a race deterministically. It
proves the code *routes* correctly. It cannot prove the operating system agrees,
and on Windows that was the gap: ``DirHandle.fd`` was ``None``, the authorised
directory was remembered as a name plus an ``lstat``, and every later ``open``,
``mkdir``, ``unlink`` and ``replace`` asked that name again. A test asserting
which call was made would have passed throughout.

So nothing here is mocked. Every test performs the substitution a real attacker
would -- rename the authorised directory away and move a different one into its
place, or drop somebody else's file onto a reserved name -- and then asks the
filesystem what happened. The two outcomes that count as correct are:

* the substitution is **refused by the kernel**, because the process is holding a
  handle on the object and the name can no longer be moved off it; or
* the substitution succeeds and the operation that follows **refuses**, because it
  is bound to an object identity rather than to a name.

Either way the payload must not land in, and the cleanup must not delete from,
anything but the object that was authorised.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_mcp import file_roots, handles
from telegram_mcp.handles import SystemCalls, UnsafeTarget
from telegram_mcp.tools import ephemeral as ephemeral_mod
from telegram_mcp.tools import media as media_mod

windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="the pin is a Win32 share-mode guarantee; POSIX holds a real openat descriptor",
)


def _swap(live: Path, replacement: Path) -> None:
    """Give ``replacement`` the name ``live`` wore: same name, different object."""
    live.rename(live.with_name(live.name + ".retired"))
    replacement.rename(live)


# --- the primitive: an authorised directory is an object, not a name ---------


@windows_only
def test_an_authorised_directory_cannot_be_renamed_out_from_under_the_handle(tmp_path):
    """The whole defect in one line. While the gate holds the directory, the
    kernel must refuse to move that name onto anything else -- otherwise every
    later step is resolving a name whose meaning an attacker still controls."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    with handles.open_allowed_directory(live, [root]):
        with pytest.raises(OSError):
            _swap(live, decoy)

    assert live.is_dir(), "the authorised directory did not survive the attempt"


@windows_only
def test_an_ancestor_of_an_authorised_directory_cannot_be_renamed_either(tmp_path):
    """Pinning only the final component leaves the prefix free: rename the parent
    and the same relative name reaches a different object."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)

    with handles.open_allowed_directory(live, [root]):
        with pytest.raises(OSError):
            root.rename(tmp_path / "root.retired")

    assert live.is_dir()


@windows_only
def test_the_pin_is_released_when_the_handle_is_closed(tmp_path):
    """A pin that outlives the call is a handle leak, and a directory the
    operator can no longer rename is a bug of its own."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)

    handles.open_allowed_directory(live, [root]).close()

    live.rename(root / "renamed-fine")
    assert (root / "renamed-fine").is_dir()


def test_the_identity_a_handle_carries_is_read_off_the_object(tmp_path):
    """Not off a name that was ``lstat``-ed once at authorisation time."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as directory:
        named = os.lstat(str(root))
        assert handles.same_object(directory.identity, named)


# --- cleanup is bound to the object it made ----------------------------------


def test_cleanup_will_not_delete_an_object_it_did_not_reserve(tmp_path):
    """``discard`` exists to remove the empty placeholder this call created. If
    that name has since been given to somebody else's file, removing it is not a
    tidy-up -- it is deleting a stranger's data on their behalf."""
    root = tmp_path / "root"
    root.mkdir()
    stranger = tmp_path / "stranger.bin"
    stranger.write_bytes(b"not-ours")

    with handles.open_allowed_directory(root, [root]) as parent:
        reserved = parent.reserve_free_name("out", ".bin")
        os.replace(str(stranger), str(root / reserved))

        parent.discard(reserved)

        assert (root / reserved).exists(), "cleanup deleted an object it never reserved"
        assert (root / reserved).read_bytes() == b"not-ours"


def test_cleanup_still_removes_the_object_it_did_reserve(tmp_path):
    """The refusal above must not be bought by making cleanup a no-op."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as parent:
        reserved = parent.reserve_free_name("out", ".bin")
        assert (root / reserved).exists()

        parent.discard(reserved)

        assert not (root / reserved).exists(), "the placeholder this call made outlived it"


def test_a_private_staging_directory_is_removed_through_its_own_handle(tmp_path):
    """Removing it by name is the same defect one level up: between the last look
    and the ``rmdir`` that name can belong to a directory this call never made."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as parent:
        _name, staging = parent.make_private_subdirectory(".stage-")
        held = Path(staging.path)
        (held / "part.bin").write_bytes(b"payload")

        staging.remove_tree()
        staging.remove_self()
        staging.close()

        assert not held.exists(), "the transfer directory outlived the call"


class _NoPrimitives(SystemCalls):
    """A host with neither ``openat`` nor a directory pin.

    Windows and POSIX each take a different branch, so on any one machine half
    the code is unreachable. This drives the remaining third: the branch that can
    hold nothing and has to prove identity by hand before it removes or replaces
    anything.
    """

    dir_fd = False
    pins = False


def test_cleanup_checks_identity_where_the_host_can_hold_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    stranger = tmp_path / "stranger.bin"
    stranger.write_bytes(b"not-ours")

    with handles.open_allowed_directory(root, [root], calls=_NoPrimitives()) as parent:
        reserved = parent.reserve_free_name("out", ".bin")
        os.replace(str(stranger), str(root / reserved))

        parent.discard(reserved)

        assert (root / reserved).read_bytes() == b"not-ours"


def test_install_checks_identity_where_the_host_can_hold_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    stranger = tmp_path / "stranger.bin"
    stranger.write_bytes(b"not-ours")

    with handles.open_allowed_directory(root, [root], calls=_NoPrimitives()) as parent:
        _name, staging = parent.make_private_subdirectory(".stage-")
        try:
            os.close(staging.create_exclusive("part.bin"))
            reserved = parent.reserve_free_name("out", ".bin")
            os.replace(str(stranger), str(root / reserved))

            with pytest.raises(UnsafeTarget):
                parent.install(staging, "part.bin", reserved)

            assert (root / reserved).read_bytes() == b"not-ours"
        finally:
            staging.remove_tree()
            staging.remove_self()
            staging.close()


# --- install is bound to the object it reserved ------------------------------


def test_install_refuses_to_publish_over_an_object_it_did_not_reserve(tmp_path):
    """The final rename replaces whatever wears the reserved name. Once that is
    no longer the empty placeholder this call created, replacing it destroys a
    file the caller never asked to lose."""
    root = tmp_path / "root"
    root.mkdir()
    stranger = tmp_path / "stranger.bin"
    stranger.write_bytes(b"not-ours")

    with handles.open_allowed_directory(root, [root]) as parent:
        _name, staging = parent.make_private_subdirectory(".stage-")
        try:
            os.close(staging.create_exclusive("part.bin"))
            reserved = parent.reserve_free_name("out", ".bin")
            os.replace(str(stranger), str(root / reserved))

            with pytest.raises(UnsafeTarget):
                parent.install(staging, "part.bin", reserved)

            assert (root / reserved).read_bytes() == b"not-ours", "a stranger's file was clobbered"
        finally:
            staging.remove_tree()
            staging.remove_self()
            staging.close()


def test_install_still_publishes_over_the_placeholder_it_reserved(tmp_path):
    """And the refusal above must not break the ordinary case."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as parent:
        _name, staging = parent.make_private_subdirectory(".stage-")
        try:
            fd = staging.create_exclusive("part.bin")
            os.write(fd, b"payload")
            os.close(fd)
            reserved = parent.reserve_free_name("out", ".bin")

            parent.install(staging, "part.bin", reserved)

            assert (root / reserved).read_bytes() == b"payload"
        finally:
            staging.remove_tree()
            staging.remove_self()
            staging.close()


def test_a_durable_write_still_lands_whole(tmp_path):
    """``write_file_durably`` installs through the same seam, so it has to keep
    working after the seam is bound to an object."""
    root = tmp_path / "root"
    root.mkdir()

    with handles.open_allowed_directory(root, [root]) as parent:
        name = parent.reserve_free_name("out", ".bin")
        parent.write_file_durably(name, b"payload")

        assert (root / name).read_bytes() == b"payload"
        assert sorted(p.name for p in root.iterdir()) == [name], "a part file was left behind"


# --- the writable gate creates nothing it has not authorised -----------------


@pytest.mark.asyncio
async def test_the_writable_gate_creates_no_directory_before_a_handle_exists(
    tmp_path, monkeypatch
):
    """Resolving a path used to run ``parent.mkdir(parents=True)`` -- creating
    real directories by name, before anything held them, and leaving them behind
    when the authorisation that followed refused."""
    root = tmp_path / "root"
    root.mkdir()

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    out, error = await file_roots._resolve_writable_file_path(
        raw_path=None, default_filename="x.bin", ctx=None, tool_name="download_media"
    )

    assert error is None
    assert not (root / "downloads").exists(), "a directory was created before it was authorised"

    async with file_roots._open_verified_directory(
        path=out.parent, ctx=None, tool_name="download_media"
    ) as (directory, dir_error):
        assert dir_error is None
        assert (root / "downloads").is_dir(), "the gate did not create the directory it holds"
        assert Path(directory.path) == (root / "downloads").resolve()


@pytest.mark.asyncio
async def test_a_destination_whose_parent_is_a_file_is_refused(tmp_path, monkeypatch):
    """Creation moved behind the handle, so the failure has to come back as an
    error rather than as an exception out of the gate."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "notadir").write_bytes(b"x")

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_directory(
        path=root / "notadir" / "deeper", ctx=None, tool_name="download_media"
    ) as (directory, error):
        assert directory is None and error


@pytest.mark.asyncio
async def test_the_writable_gate_will_not_create_a_directory_outside_the_roots(
    tmp_path, monkeypatch
):
    """Creation moving behind the handle must not become creation without a
    containment check."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_directory(
        path=outside / "made-up", ctx=None, tool_name="download_media"
    ) as (directory, error):
        assert directory is None and error
        assert not outside.exists(), "a directory was created outside the allowed roots"


# --- a root that names a single file -----------------------------------------


@pytest.mark.asyncio
async def test_a_root_that_names_one_file_can_still_read_that_file(tmp_path, monkeypatch):
    """``_path_is_within_root`` advertises a single-file root, and the read gate
    then opens that file's PARENT -- which no file root contains. The feature was
    therefore refused at the handle, for every file, always."""
    only = tmp_path / "vault" / "allowed.bin"
    only.parent.mkdir()
    only.write_bytes(b"payload")

    async def _roots(ctx, tool_name):
        return [only], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_source(
        raw_path=str(only), ctx=None, tool_name="upload_file"
    ) as (source, error):
        assert error is None, error
        assert source.handle.read() == b"payload"


@pytest.mark.asyncio
async def test_a_root_that_names_one_file_does_not_authorise_its_neighbours(tmp_path, monkeypatch):
    """Reaching the file has to mean reaching that file, not everything beside it."""
    only = tmp_path / "vault" / "allowed.bin"
    only.parent.mkdir()
    only.write_bytes(b"payload")
    neighbour = only.parent / "secret.bin"
    neighbour.write_bytes(b"not-allowed")

    async def _roots(ctx, tool_name):
        return [only], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_source(
        raw_path=str(neighbour), ctx=None, tool_name="upload_file"
    ) as (source, error):
        assert source is None and error, "a neighbour of the file root was handed over"


# --- the caller families -----------------------------------------------------


class _DownloadClient:
    """Writes a real file the way Telethon does, choosing the extension itself."""

    def __init__(self, during=None):
        self._during = during
        self.raised = None

    async def get_messages(self, entity, ids=None):
        return SimpleNamespace(id=ids, media=object(), file=SimpleNamespace(size=None))

    async def download_media(self, message, file=None, progress_callback=None):
        written = Path(str(file) + ".jpg")
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(b"payload")
        if self._during is not None:
            try:
                self._during()
            except OSError as refused:
                self.raised = refused
        return str(written)


@pytest.mark.asyncio
async def test_a_download_holds_its_destination_for_the_whole_transfer(monkeypatch, tmp_path):
    """The write caller family, end to end: while ``download_media`` is running,
    the destination directory must not be exchangeable for another one."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()

    client = _DownloadClient(during=lambda: _swap(live, decoy))
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

    if os.name == "nt":
        assert client.raised is not None, "the destination was swapped mid-transfer"
        assert result.startswith("Media downloaded to"), result
        assert Path(result.split("to ", 1)[1].rstrip(".")).parent == live
    assert not (root / "decoy").exists() or not any(
        p.is_file() for p in (root / "decoy").iterdir()
    ), "the payload landed in the replacement"


@pytest.mark.asyncio
async def test_a_download_releases_the_destination_when_it_is_done(monkeypatch, tmp_path):
    """Holding the directory is the fix; holding it after the call has returned
    is a handle leak that leaves the operator unable to move their own folder
    for the life of the server."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)

    monkeypatch.setattr(media_mod, "get_client", lambda account=None: _DownloadClient())

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
    assert result.startswith("Media downloaded to"), result

    live.rename(root / "moved")
    assert (root / "moved").is_dir()


@pytest.mark.asyncio
async def test_a_save_holds_its_destination_for_the_whole_transfer(monkeypatch, tmp_path):
    """The other write caller family. Same guarantee, different tool."""
    root = tmp_path / "root"
    live = root / "out"
    live.mkdir(parents=True)
    decoy = root / "decoy"
    decoy.mkdir()
    # Taken before the swap: after it, no pathname names this object reliably.
    authorised = live.stat()
    payload = b"secret-bytes"

    refused = []
    client = SimpleNamespace()

    async def _ensure(_client):
        return None

    async def _resolve_entity(chat_id, _client):
        return SimpleNamespace(id=chat_id)

    async def _get_messages(entity, ids=None):
        return SimpleNamespace(id=ids, media=object(), ttl_seconds=30)

    client.get_messages = _get_messages

    async def _download(_cl, _msg, _max_bytes):
        return b"secret-bytes", False

    async def _resolve_path(*, raw_path, default_filename, ctx, tool_name):
        return (live / "loot.jpg"), None

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(ephemeral_mod, "get_client", lambda account=None: client)
    monkeypatch.setattr(ephemeral_mod, "ensure_connected", _ensure)
    monkeypatch.setattr(ephemeral_mod, "resolve_entity", _resolve_entity)
    monkeypatch.setattr(ephemeral_mod, "_ttl_of", lambda msg: 30)
    monkeypatch.setattr(ephemeral_mod, "_describe_ttl", lambda msg: {"ttl_seconds": 30})
    monkeypatch.setattr(
        ephemeral_mod, "describe_media", lambda msg: {"kind": "photo", "extension": ".jpg"}
    )
    monkeypatch.setattr(ephemeral_mod, "_download_capped", _download)
    monkeypatch.setattr(ephemeral_mod, "_resolve_writable_file_path", _resolve_path)
    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    reserve = handles.DirHandle.reserve_free_name

    def _reserve_then_swap(self, stem, suffix):
        name = reserve(self, stem, suffix)
        try:
            _swap(live, decoy)
        except OSError as error:
            refused.append(error)
        return name

    monkeypatch.setattr(handles.DirHandle, "reserve_free_name", _reserve_then_swap)

    await ephemeral_mod.save_disappearing_media(1, 5, preview=False, account="a")

    if os.name == "nt":
        assert refused, "the destination was swapped after it had been authorised"
        assert (live / "loot.jpg").read_bytes() == b"secret-bytes"

    # By identity, not by pathname. Where the swap is refused the authorised
    # directory keeps its name and the two questions have the same answer;
    # where it succeeds the authorised directory is still that object and is
    # simply wearing the decoy's name, so judging by path calls a correct
    # outcome a breach and would have hidden the real one behind the noise.
    landed = [path for path in root.rglob("*") if path.is_file() and path.read_bytes() == payload]
    assert landed, "the payload was never written at all"
    for path in landed:
        holder = path.parent.stat()
        assert (holder.st_dev, holder.st_ino) == (authorised.st_dev, authorised.st_ino), (
            f"the payload landed in {path.parent.name!r}, which is not the object the "
            "roots gate authorised"
        )


@pytest.mark.asyncio
async def test_an_open_source_holds_the_directory_it_was_read_from(monkeypatch, tmp_path):
    """The read caller family. The verdict was about a file inside one directory;
    that directory must not become a different one while the upload is in
    flight."""
    root = tmp_path / "root"
    live = root / "box"
    live.mkdir(parents=True)
    (live / "a.bin").write_bytes(b"payload")
    decoy = root / "decoy"
    decoy.mkdir()

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_source(
        raw_path=str(live / "a.bin"), ctx=None, tool_name="upload_file"
    ) as (source, error):
        assert error is None, error
        if os.name == "nt":
            with pytest.raises(OSError):
                _swap(live, decoy)
        assert source.handle.read() == b"payload"


@pytest.mark.asyncio
async def test_a_read_releases_the_directory_when_it_is_done(monkeypatch, tmp_path):
    """A pinned upload source that is never released would leave the operator
    unable to move their own folder for the life of the server."""
    root = tmp_path / "root"
    live = root / "box"
    live.mkdir(parents=True)
    (live / "a.bin").write_bytes(b"payload")

    async def _roots(ctx, tool_name):
        return [root], None

    monkeypatch.setattr(file_roots, "_ensure_allowed_roots", _roots)

    async with file_roots._open_verified_source(
        raw_path=str(live / "a.bin"), ctx=None, tool_name="upload_file"
    ) as (source, error):
        assert error is None

    live.rename(root / "moved")
    assert (root / "moved").is_dir()
    assert source.handle.closed, "the upload handle outlived the call"
