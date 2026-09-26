"""File-path security: which roots are allowed, and what a caller's string resolves to.

Split out of `test_runtime.py` when the code did the same. The subject is
`telegram_mcp/file_roots.py`.

`SERVER_ALLOWED_ROOTS` is patched on `file_roots`, never on `runtime` or `main`: it is
rebound rather than mutated, and those two hold further names for the same list, so a
patch applied there is invisible to the code that reads it.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

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
    assert runtime._path_is_within_root(file_root.resolve(), file_root.resolve()) is True
    assert runtime._path_is_within_root(root.resolve(), file_root.resolve()) is False
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

    # Absolute: a relative path starts in the installation's files/ folder (FR-033).
    missing = str(root / "missing.txt")
    resolved, error = await runtime._resolve_readable_file_path(
        raw_path=missing, ctx=None, tool_name="send_file"
    )
    assert resolved is None
    assert error == f"File not found: {missing}"

    resolved, error = await runtime._resolve_readable_file_path(
        raw_path=str(nested), ctx=None, tool_name="send_file"
    )
    assert resolved is None
    assert "Path is not a file" in error

    out_path, error = await runtime._resolve_writable_file_path(
        raw_path=str(root / "nested" / "out.bin"),
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


def test_configure_allowed_roots_from_cli_updates_runtime_and_main_alias(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    runtime._configure_allowed_roots_from_cli([str(root), str(root)])
    assert runtime.SERVER_ALLOWED_ROOTS == [root.resolve()]

    runtime._configure_allowed_roots_from_cli([str(root)])
    # Asserted on the owning module: the list is mutated in place precisely so
    # every star-importer keeps seeing the same object.
    assert file_roots.SERVER_ALLOWED_ROOTS == [root.resolve()]

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


class _FailingRootsSession:
    def __init__(self, error: Exception):
        self._error = error

    async def list_roots(self):
        raise self._error


def _ctx_with_list_roots_error(error: Exception):
    return SimpleNamespace(session=_FailingRootsSession(error))


class _SilentRootsSession:
    """A client that accepts `roots/list` and never answers it.

    Not a hypothetical: the request is sent on the connection's STANDALONE
    channel (the SDK selects it whenever no `related_request_id` is given), so a
    client that only reads the stream its own POST returned never sees it.
    """

    def __init__(self):
        self.cancelled = False

    async def list_roots(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_a_stream_name_is_refused_before_it_can_reach_the_disk(tmp_path, monkeypatch):
    """`file_path="notes:hidden"` wrote an NTFS alternate data stream.

    The colon does not name a file, it names a stream OF one: the bytes land in
    `notes:hidden.bin` and the folder shows a single, visible, EMPTY `notes`.
    Measured before the fix - one 0-byte file in the listing and the payload
    nowhere a reader would look, with the reply reporting a path that describes
    nothing on disk. `safe_suffix` already refuses this in the SENDER's
    extension and says exactly why; the caller's own name reached
    `create_exclusive` unchecked, which is the same hole through the other door.

    Guarded in `_contains_forbidden_path_patterns`, so the READ gate refuses it
    too rather than each caller remembering to.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(file_roots, "SERVER_ALLOWED_ROOTS", [root.resolve()])

    for raw in ("notes:hidden", "notes:hidden.bin", "sub/notes:hidden.bin"):
        assert "':'" in (
            runtime._contains_forbidden_path_patterns(raw) or ""
        ), f"{raw!r} was not recognised as a stream name"

    resolved, error = await file_roots._resolve_writable_file_path(
        raw_path="notes:hidden",
        default_filename="fallback.bin",
        ctx=None,
        tool_name="download_media",
    )
    assert resolved is None, f"a stream name resolved to {resolved}"
    assert "':'" in error

    # A drive letter is the one colon that IS a path separator, and refusing it
    # would disable every absolute path on Windows.
    assert runtime._contains_forbidden_path_patterns(str(root / "plain.bin")) is None


# --- TELEGRAM_FILE_ROOTS ------------------------------------------------------
#
# The command line was the only way to name a server-side root, and it is the one
# route an MCP client config makes awkward: the client supplies its own argv and
# offers an `env` block beside it. So the file tools reported themselves disabled
# with a fix the operator could not apply, and `send_file` looked broken rather
# than unconfigured.


@pytest.fixture(autouse=True)
def _own_roots_list():
    """Restore the CONTENTS, never the name.

    `runtime` and `main` re-export `SERVER_ALLOWED_ROOTS` and hold the same list
    object, which is why the module slice-assigns into it. Rebinding the name here
    - the obvious way to isolate a test - is the one thing that breaks that, and
    it took the alias test with it.
    """
    before = list(file_roots.SERVER_ALLOWED_ROOTS)
    yield
    file_roots.SERVER_ALLOWED_ROOTS[:] = before


def test_the_environment_can_name_the_allowed_roots(monkeypatch, tmp_path):
    one, two = tmp_path / "media", tmp_path / "docs"
    one.mkdir()
    two.mkdir()
    monkeypatch.setenv("TELEGRAM_FILE_ROOTS", os.pathsep.join([str(one), str(two)]))

    file_roots._configure_allowed_roots_from_cli([])

    assert file_roots.SERVER_ALLOWED_ROOTS == [one.resolve(), two.resolve()]


def test_a_trailing_separator_is_not_a_root(monkeypatch, tmp_path):
    """An operator writing a list ends it with a separator; that is not an error."""
    one = tmp_path / "media"
    one.mkdir()
    monkeypatch.setenv("TELEGRAM_FILE_ROOTS", str(one) + os.pathsep + "  " + os.pathsep)

    file_roots._configure_allowed_roots_from_cli([])

    assert file_roots.SERVER_ALLOWED_ROOTS == [one.resolve()]


def test_the_command_line_and_the_environment_combine(monkeypatch, tmp_path):
    from_argv, from_env = tmp_path / "argv", tmp_path / "env"
    from_argv.mkdir()
    from_env.mkdir()
    monkeypatch.setenv("TELEGRAM_FILE_ROOTS", str(from_env))

    file_roots._configure_allowed_roots_from_cli([str(from_argv)])

    assert file_roots.SERVER_ALLOWED_ROOTS == [from_argv.resolve(), from_env.resolve()]


def test_a_root_from_the_environment_that_does_not_exist_stops_the_server(monkeypatch, tmp_path):
    """Same refusal as the command line. A root that is not there is a typo, and
    silently allowing nothing is how a deny-all gets mistaken for a bug."""
    monkeypatch.setenv("TELEGRAM_FILE_ROOTS", str(tmp_path / "not-here"))

    with pytest.raises(SystemExit) as stopped:
        file_roots._configure_allowed_roots_from_cli([])

    assert "not-here" in str(stopped.value)


def test_no_variable_means_no_server_roots(monkeypatch):
    monkeypatch.delenv("TELEGRAM_FILE_ROOTS", raising=False)

    file_roots._configure_allowed_roots_from_cli([])

    assert file_roots.SERVER_ALLOWED_ROOTS == []


# --- allowing a folder without a restart --------------------------------------
#
# `_configure_allowed_roots_from_cli` runs once, from the runner, so until now a
# new folder meant restarting the server. The operator cannot change a RUNNING
# process's environment from outside, which is why re-reading `os.environ` would
# refresh nothing: `load_dotenv` copied the file's value in at startup and never
# again. The file itself is the live source.


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """Point the snapshot reader at a `.env` this test owns."""
    from telegram_mcp import account_config, account_snapshot
    from telegram_mcp import settings as settings_mod

    path = tmp_path / ".env"

    def write(body):
        path.write_text(body, encoding="utf-8")
        return path

    monkeypatch.setattr(account_config, "_env_file", lambda: str(path))
    monkeypatch.setattr(file_roots, "_CLI_ROOTS", [])
    monkeypatch.setattr(settings_mod, "PROCESS_FILE_ROOTS", None, raising=False)
    account_snapshot.forget_accepted_source()
    yield write
    account_snapshot.forget_accepted_source()


def test_a_root_added_to_the_file_is_picked_up_without_a_restart(env_file, tmp_path):
    allowed = tmp_path / "media"
    allowed.mkdir()
    env_file(f"TELEGRAM_FILE_ROOTS={allowed}\n")

    changed = file_roots.refresh_server_roots()

    assert changed is True
    assert file_roots.SERVER_ALLOWED_ROOTS == [allowed.resolve()]


def test_a_second_refresh_with_no_edit_reports_no_change(env_file, tmp_path):
    allowed = tmp_path / "media"
    allowed.mkdir()
    env_file(f"TELEGRAM_FILE_ROOTS={allowed}\n")
    file_roots.refresh_server_roots()

    assert file_roots.refresh_server_roots() is False


def test_a_value_the_process_supplied_still_wins(env_file, tmp_path, monkeypatch):
    """`load_dotenv` does not override a real process variable, so neither does
    this - otherwise a refresh would quietly replace a deliberate one."""
    from telegram_mcp import settings as settings_mod

    from_process, from_file = tmp_path / "process", tmp_path / "file"
    from_process.mkdir()
    from_file.mkdir()
    env_file(f"TELEGRAM_FILE_ROOTS={from_file}\n")
    monkeypatch.setattr(settings_mod, "PROCESS_FILE_ROOTS", str(from_process))

    file_roots.refresh_server_roots()

    assert file_roots.SERVER_ALLOWED_ROOTS == [from_process.resolve()]


def test_a_root_that_does_not_exist_does_not_drop_the_ones_that_do(env_file, tmp_path):
    """A typo in a live edit must not disable file tools that were working."""
    good = tmp_path / "media"
    good.mkdir()
    env_file(f"TELEGRAM_FILE_ROOTS={good}{os.pathsep}{tmp_path / 'typo'}\n")

    file_roots.refresh_server_roots()

    assert file_roots.SERVER_ALLOWED_ROOTS == [good.resolve()]


def test_a_file_that_cannot_be_read_leaves_the_roots_alone(env_file, tmp_path, monkeypatch):
    """This runs on the path of every file tool. A `.env` mid-rewrite must not
    turn into a refusal for an operation that was already permitted."""
    from telegram_mcp import account_snapshot

    allowed = tmp_path / "media"
    allowed.mkdir()
    env_file(f"TELEGRAM_FILE_ROOTS={allowed}\n")
    file_roots.refresh_server_roots()
    before = list(file_roots.SERVER_ALLOWED_ROOTS)

    monkeypatch.setattr(
        account_snapshot, "_open_file_bytes", lambda _p: (_ for _ in ()).throw(OSError("busy"))
    )

    assert file_roots.refresh_server_roots() is False
    assert list(file_roots.SERVER_ALLOWED_ROOTS) == before


def test_command_line_roots_survive_a_refresh(env_file, tmp_path, monkeypatch):
    """A refresh rebuilds the list; the roots the server was STARTED with are
    part of it, not something the file can take away."""
    from_cli, from_file = tmp_path / "cli", tmp_path / "file"
    from_cli.mkdir()
    from_file.mkdir()
    monkeypatch.setattr(file_roots, "_CLI_ROOTS", [str(from_cli)])
    env_file(f"TELEGRAM_FILE_ROOTS={from_file}\n")

    file_roots.refresh_server_roots()

    assert file_roots.SERVER_ALLOWED_ROOTS == [from_cli.resolve(), from_file.resolve()]
