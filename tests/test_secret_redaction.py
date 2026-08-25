"""Nothing secret may reach a log file, a backup, or an operator's terminal.

Reproduced from the audit: `log_and_format_error` interpolated every keyword it
was handed with a bare `str`, and the call sites hand it queries, phone numbers,
contact names, aliases, chat titles, file paths, poll questions and captions. A
planted canary came back out of `mcp_errors.log` verbatim, in a file the default
umask left readable by every account on the machine, with no size ceiling on it.

The canaries below are synthetic. Nothing here touches a real session.
"""

import logging
import os
import re
import stat
import subprocess
import sys

import pytest

from telegram_mcp import aliases, connection, runtime, safe_log

CANARY = "canary-9Yb3Qw-do-not-log"
SESSION_CANARY = "1" + "A" * 48  # shaped like a Telethon StringSession

posix_modes_only = pytest.mark.skipif(
    os.name == "nt",
    # `os.chmod` on Windows toggles only the read-only flag: it cannot clear the
    # read bit and `st_mode` never reports 0o600, so this asserts the platform
    # rather than the code.
    reason="POSIX permission bits; Windows os.chmod cannot express them",
)


@pytest.fixture
def logged(tmp_path, monkeypatch):
    """A real handler over a throwaway file, wired in place of the global one."""
    path = tmp_path / "mcp_errors.log"
    handler = connection._make_file_handler(str(path))
    test_logger = logging.getLogger("telegram_mcp.test-redaction")
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False
    test_logger.addHandler(handler)
    # `safe_log` owns the logger and is the only module that calls a method on
    # it, so that is the name a test has to replace.
    monkeypatch.setattr(safe_log, "logger", test_logger)
    try:
        yield path
    finally:
        test_logger.removeHandler(handler)
        handler.close()


def test_a_planted_context_value_never_reaches_the_log(logged, capsys):
    returned = runtime.log_and_format_error(
        "search_messages",
        ValueError("boom"),
        query=CANARY,
        caption=CANARY,
        phone="+15550001111",
        chat_id=-1001234567890,
    )

    written = logged.read_text(encoding="utf-8")
    captured = capsys.readouterr()

    assert CANARY not in written
    assert CANARY not in returned
    assert CANARY not in captured.out + captured.err
    assert "+15550001111" not in written
    # The numeric id is not user content and is what makes a report actionable.
    assert "-1001234567890" in written


def test_a_secret_inside_the_exception_text_is_scrubbed(logged, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", SESSION_CANARY)

    # Raised and caught, the way the tool call sites do it.
    try:
        raise RuntimeError(f"failed for session {SESSION_CANARY} via t.me/+SecretInviteHash")
    except RuntimeError as exc:
        runtime.log_and_format_error("export_chat_invite", exc, chat_id=1)

    written = logged.read_text(encoding="utf-8")
    assert SESSION_CANARY not in written
    assert "SecretInviteHash" not in written


def test_the_shape_scrubber_still_marks_what_it_catches(monkeypatch):
    """`log_and_format_error` no longer writes exception text at all, so the
    marker cannot appear from that path. The scrubber still guards every OTHER
    record - the console handler, and anything logged without going through it."""
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", SESSION_CANARY)

    cleaned = connection.redact(f"session {SESSION_CANARY} via t.me/+SecretInviteHash")

    assert SESSION_CANARY not in cleaned
    assert "SecretInviteHash" not in cleaned
    assert "REDACTED" in cleaned


def test_an_arbitrary_canary_in_exception_text_never_reaches_the_log(logged):
    """The case the shape-based scrubber cannot win.

    `exc_info=True` wrote the exception's own text and its whole traceback, and
    an exception carries whatever the failing call was given - a caption, a
    filename, a contact's name. `redact()` can only catch shapes it was told
    about, so anything else came straight back out of the file.
    """
    try:
        raise RuntimeError(f"upload of {CANARY} failed")
    except RuntimeError as exc:
        runtime.log_and_format_error("send_file", exc, chat_id=1)

    written = logged.read_text(encoding="utf-8")

    assert CANARY not in written
    # Not by writing nothing: a report has to stay actionable.
    assert "RuntimeError" in written
    assert "send_file" in written


def test_a_canary_in_a_chained_cause_never_reaches_the_log(logged):
    """`raise X from Y` renders BOTH exceptions, so scrubbing only the outer one
    leaks the inner text that the outer was raised to hide."""
    try:
        try:
            raise ValueError(f"inner {CANARY}")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        runtime.log_and_format_error("download_media", exc, chat_id=1)

    written = logged.read_text(encoding="utf-8")

    assert CANARY not in written
    assert "ValueError" in written, "the cause's type is the useful half"


def test_two_failures_with_the_same_text_share_a_digest(logged):
    """What replaces the text has to be enough to say "these are the same bug"."""
    for _ in range(2):
        try:
            raise RuntimeError(f"upload of {CANARY} failed")
        except RuntimeError as exc:
            runtime.log_and_format_error("send_file", exc, chat_id=1)

    digests = re.findall(r"#([0-9a-f]{8})", logged.read_text(encoding="utf-8"))

    assert len(digests) == 2 and digests[0] == digests[1]


@posix_modes_only
def test_the_log_file_is_owner_only(logged):
    runtime.log_and_format_error("get_chat", ValueError("boom"), chat_id=1)

    assert stat.S_IMODE(logged.stat().st_mode) == 0o600


def test_the_log_file_cannot_grow_without_bound(logged):
    handler = logging.getLogger("telegram_mcp.test-redaction").handlers[0]

    assert handler.maxBytes > 0, "no size ceiling: one bad night fills the disk"
    assert handler.backupCount > 0, "rotation with no retention keeps nothing"


def test_the_production_logger_is_bounded_and_redacting():
    """The wiring, not just the factory: the global handlers must carry both."""
    file_handlers = [h for h in connection.logger.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers, "no file handler is installed"
    for handler in file_handlers:
        assert getattr(handler, "maxBytes", 0) > 0
    for handler in connection.logger.handlers:
        assert any(
            isinstance(f, connection.RedactingFilter) for f in handler.filters
        ), f"{handler!r} can emit unredacted text"


# --- owner-only on Windows too, not just on POSIX -----------------------------
#
# `os.chmod(path, 0o600)` cannot clear the read bit on Windows: it toggles the
# read-only attribute and nothing else. Every private file this project writes -
# the alias store, `.env`, its backups - was therefore readable by every account
# on the machine this project targets first, while the POSIX-only tests passed.

windows_acls_only = pytest.mark.skipif(
    os.name != "nt", reason="Windows ACLs; there is no icacls on the Linux runner"
)


def test_hardening_reports_what_the_object_allows_not_what_the_call_returned():
    """The old test asserted the icacls ARGV and passed for months while the
    command it described left foreign entries in place. `restrict_to_owner` now
    reads the DACL back off the object, so True is a statement about the file.

    Checked here without a real ACL so it holds on every host; the Windows test
    that seeds an `Everyone` entry and proves its removal lives beside it."""
    from telegram_mcp import owner_only

    assert aliases.restrict_to_owner is not None
    assert not hasattr(
        aliases, "_owner_only_acl_command"
    ), "the icacls argv builder is gone; nothing may reconstruct it"
    assert (
        owner_only.verify_owner_only("no-such-path-here") is False
    ), "an unreadable object must never verify as private"


@windows_acls_only
def test_a_private_file_ends_up_with_one_access_entry(tmp_path):
    path = tmp_path / "private.json"
    path.write_text("secret", encoding="utf-8")

    assert aliases.restrict_to_owner(path) is True

    listing = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, timeout=30, check=True
    )
    # One ACE, whatever this host's language calls its groups.
    assert listing.stdout.count(":(") == 1
    assert os.environ["USERNAME"] in listing.stdout


@windows_acls_only
def test_the_alias_store_is_owner_only_on_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    runtime.save_aliases({"андрей": 1})

    listing = subprocess.run(
        ["icacls", str(runtime.aliases_file_path())],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert listing.stdout.count(":(") == 1


@windows_acls_only
def test_env_and_backup_are_owner_only_on_windows(generator, tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    backup = generator.write_env_value("TELEGRAM_SESSION_STRING", SESSION_CANARY, env)

    for path in (env, backup):
        listing = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, timeout=30, check=True
        )
        assert listing.stdout.count(":(") == 1, f"{path} is not owner-only"


def test_restricting_an_absent_file_reports_failure_rather_than_raising(tmp_path):
    """A caller that cannot lock a file down has to be able to say so; raising
    here would take down a tool call over a permissions detail."""
    assert aliases.restrict_to_owner(tmp_path / "nothing-here.json") is False


# --- the session generator's own files ---------------------------------------


@pytest.fixture
def generator():
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "session_string_generator", repo / "session_string_generator.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@posix_modes_only
def test_env_and_backup_are_written_owner_only(generator, tmp_path):
    """Both hold a full login to every configured account."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")
    os.chmod(env, 0o644)

    backup = generator.write_env_value("TELEGRAM_SESSION_STRING", SESSION_CANARY, env)

    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_old_env_backups_are_pruned(generator, tmp_path):
    """Every backup is a complete set of logins; keeping all of them forever
    turns one leaked directory into a leak of every session ever generated."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    for index in range(generator.ENV_BACKUP_RETENTION + 3):
        generator.write_env_value("TELEGRAM_SESSION_STRING", f"1AAA{index}", env)

    backups = sorted(tmp_path.glob(".env.backup-*"))
    assert len(backups) <= generator.ENV_BACKUP_RETENTION


def test_a_failed_write_leaves_no_temporary_file_behind(generator, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(generator.os, "replace", _boom)

    with pytest.raises(OSError):
        generator.write_env_value("TELEGRAM_SESSION_STRING", SESSION_CANARY, env)

    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".env.") and ".tmp" in p.name
    ]
    assert leftovers == []
    assert SESSION_CANARY not in env.read_text(encoding="utf-8")


def test_no_echo_reports_the_result_without_printing_the_session(generator, capsys):
    """A terminal is scrollback, a screen share and often a shell log."""
    generator._report_session("TELEGRAM_SESSION_STRING_WORK", SESSION_CANARY, echo=False)

    printed = capsys.readouterr().out
    assert SESSION_CANARY not in printed
    assert "TELEGRAM_SESSION_STRING_WORK" in printed


def test_without_no_echo_the_session_is_still_shown(generator, capsys):
    """The default stays what it was: some operators copy it by hand."""
    generator._report_session("TELEGRAM_SESSION_STRING", SESSION_CANARY, echo=True)

    assert SESSION_CANARY in capsys.readouterr().out


def test_the_no_echo_flag_is_accepted(generator, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["session_string_generator.py", "--no-echo"])
    assert generator._parse_args().no_echo is True


class _AuthorizedClient:
    """A client that is already signed in, so no login path is exercised."""

    def __init__(self, *args, **kwargs):
        self.session = object()
        self.disconnected = False

    def connect(self):
        return None

    def is_user_authorized(self):
        return True

    def disconnect(self):
        self.disconnected = True


def test_a_whole_no_echo_run_saves_the_session_without_showing_it(
    generator, tmp_path, monkeypatch, capsys
):
    """End to end: the canary reaches `.env` and nothing else."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["session_string_generator.py", "--qr", "--label", "work", "--no-echo"]
    )
    monkeypatch.setattr(generator, "_check_installation", lambda: None)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "dummy_hash")
    monkeypatch.setattr(generator, "TelegramClient", _AuthorizedClient)
    monkeypatch.setattr(
        generator,
        "StringSession",
        type("_S", (), {"save": staticmethod(lambda _s: SESSION_CANARY)}),
    )

    def _no_input(_prompt=""):  # pragma: no cover - a prompt here is the failure
        raise AssertionError("a --no-echo run must not stop to ask anything")

    monkeypatch.setattr("builtins.input", _no_input)

    generator.main()

    printed = capsys.readouterr().out
    assert SESSION_CANARY not in printed
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert saved.strip() == f"TELEGRAM_SESSION_STRING_WORK={SESSION_CANARY}"
