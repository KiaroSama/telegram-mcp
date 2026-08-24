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
import stat
import sys

import pytest

from telegram_mcp import connection, runtime

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
    monkeypatch.setattr(runtime, "logger", test_logger)
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

    # Raised and caught, the way the tool call sites do it: `exc_info=True`
    # records the exception being handled, traceback and final line included.
    try:
        raise RuntimeError(f"failed for session {SESSION_CANARY} via t.me/+SecretInviteHash")
    except RuntimeError as exc:
        runtime.log_and_format_error("export_chat_invite", exc, chat_id=1)

    written = logged.read_text(encoding="utf-8")
    assert SESSION_CANARY not in written
    assert "SecretInviteHash" not in written
    assert "REDACTED" in written


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
