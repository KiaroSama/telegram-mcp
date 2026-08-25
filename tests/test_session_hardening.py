"""A session file IS the account, and `.env` is the key to making more of them.

`TELEGRAM_SESSION_NAME=telegram_session` makes Telethon create
`telegram_session.session`: a SQLite database holding the auth key. Whoever can
read it is logged in as that account, with no password and no second factor.
Telethon's own docstring says so. It was being created wherever the process
happened to start, with whatever the umask gave it -- 0644 on a normal host, and
on Windows readable by every account on the machine.

`.env` is the same problem one step earlier: the documented `cp .env.example .env`
copies the example's mode, and that file holds `TELEGRAM_API_HASH` and, in the
single-account setup the README shows first, a full session string.

So both are hardened at startup, and a session with no explicit location is
created inside the private state directory rather than beside the source.

The SQLite session in these tests is a REAL `telethon.sessions.SQLiteSession`
over a real temporary file, because the thing being checked is what Telethon
actually puts on disk -- the database plus whatever `-journal`, `-wal` or `-shm`
sibling SQLite decides to make -- and a mock would only assert this module's
opinion of that.

`os.chmod` cannot express owner-only on Windows: it toggles the read-only
attribute and cannot clear the read bit. The mode assertions are therefore POSIX
and are marked as such; the Windows path is `icacls`, and the tests that pin it
assert the command rather than a mode.
"""

import os
import stat
import subprocess

import pytest
from telethon.sessions import SQLiteSession

from telegram_mcp import connection, settings

posix_modes_only = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits; Windows os.chmod cannot express them",
)


def _real_session(directory, name="probe"):
    """A genuine Telethon session database, written to and closed."""
    path = directory / f"{name}.session"
    session = SQLiteSession(str(path))
    session.set_dc(2, "149.154.167.51", 443)
    session.save()
    session.close()
    return path


# --- where a session lives ---------------------------------------------------


def test_a_bare_session_name_lands_in_the_private_state_directory(tmp_path, monkeypatch):
    """Beside the source it sits in the git checkout, is created by whatever
    process happened to start there, and inherits that directory's permissions."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # An empty stand-in for the installation directory, so the result does not
    # depend on whether this checkout has ever been used to run the server.
    monkeypatch.setattr(connection, "script_dir", str(tmp_path / "install"))

    resolved = connection.session_file_path("telegram_session")

    assert resolved == settings.state_dir() / "telegram_session.session"


def test_an_explicit_path_is_honoured_where_the_operator_put_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    explicit = tmp_path / "vault" / "work.session"

    assert connection.session_file_path(str(explicit)) == explicit


def test_a_session_already_beside_the_installation_keeps_working(tmp_path, monkeypatch):
    """An existing install must not be broken by the new default, and the file
    must not be moved out from under a live client that may hold it open."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    # Beside the INSTALLATION, which is the case this is about - the working
    # directory is a separate fallback with its own test.
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(connection, "script_dir", str(install))
    legacy = install / "telegram_session.session"
    legacy.write_bytes(b"")

    assert connection.session_file_path("telegram_session") == legacy


def test_the_extension_is_not_doubled(tmp_path, monkeypatch):
    """Telethon appends `.session` itself unless the name already ends in it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(connection, "script_dir", str(tmp_path / "install"))

    resolved = connection.session_file_path("telegram_session.session")

    assert resolved.name == "telegram_session.session"


# --- who may read it ---------------------------------------------------------


def test_a_real_session_database_and_its_directory_are_restricted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory = settings.state_dir()
    directory.mkdir(parents=True)
    path = _real_session(directory)

    restricted = []
    connection.harden_session_files(path, restrict=lambda target: restricted.append(str(target)))

    assert str(path) in restricted, "the session database was left as the umask made it"
    assert str(directory) in restricted, "the directory holding the session was left open"


def test_a_directory_the_server_did_not_choose_is_left_alone(tmp_path, monkeypatch):
    """A session the operator put somewhere of their own lives in a directory
    that is theirs. Locking it down strips the permissions off everything else in
    it -- measured: with a legacy session in the working directory this took the
    inherited ACL off the whole project checkout."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    elsewhere = tmp_path / "a-directory-of-their-own"
    elsewhere.mkdir()
    path = _real_session(elsewhere)

    restricted = []
    connection.harden_session_files(path, restrict=lambda target: restricted.append(str(target)))

    assert str(path) in restricted, "the session database was left as the umask made it"
    assert str(elsewhere) not in restricted, "the operator's own directory was locked down"


def test_every_sidecar_sqlite_leaves_behind_is_restricted_too(tmp_path, monkeypatch):
    """A `-journal`/`-wal` file holds pages of the same database, so restricting
    only the `.session` restricts nothing while a write is in flight."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory = settings.state_dir()
    directory.mkdir(parents=True)
    path = _real_session(directory)
    for suffix in ("-journal", "-wal", "-shm"):
        (directory / (path.name + suffix)).write_bytes(b"pages")

    restricted = []
    connection.harden_session_files(path, restrict=lambda target: restricted.append(str(target)))

    for suffix in ("-journal", "-wal", "-shm"):
        assert str(directory / (path.name + suffix)) in restricted, suffix


@posix_modes_only
def test_a_real_session_database_ends_up_mode_600(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory = settings.state_dir()
    directory.mkdir(parents=True)
    path = _real_session(directory)
    (directory / (path.name + "-journal")).write_bytes(b"pages")

    assert connection.harden_session_files(path) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / (path.name + "-journal")).stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "nt", reason="the Windows owner-only mechanism is icacls")
def test_session_hardening_leaves_no_foreign_entry_on_the_object(tmp_path, monkeypatch):
    """Seeds an explicit `Everyone` entry first, because that is the case the
    previous implementation silently failed: it dropped INHERITED entries and
    replaced only the named principal's own, so a file that already carried a
    broad explicit entry stayed readable by every account on the machine - and
    reported success.

    Asserted against the real DACL rather than a mocked subprocess: a mock can
    only ever confirm that the code called what it already intended to call."""
    import subprocess as _subprocess

    from telegram_mcp.owner_only import verify_owner_only

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory = settings.state_dir()
    directory.mkdir(parents=True)
    path = _real_session(directory)

    seeded = _subprocess.run(
        ["icacls", str(path), "/grant", "*S-1-1-0:(R)"], capture_output=True, text=True
    )
    assert seeded.returncode == 0, f"could not seed the Everyone entry: {seeded.stderr}"
    assert not verify_owner_only(
        path
    ), "the seeded Everyone entry was not visible, so this proves nothing"

    connection.harden_session_files(path)

    listing = _subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    assert "Everyone" not in listing, listing
    assert "S-1-1-0" not in listing, listing
    assert verify_owner_only(path), f"still not owner-only:{chr(10)}{listing}"


def test_an_unrepairable_session_is_reported_rather_than_passed_over(tmp_path, caplog):
    directory = tmp_path / "state"
    directory.mkdir()
    path = _real_session(directory)

    with caplog.at_level("WARNING", logger="telegram_mcp"):
        assert connection.harden_session_files(path, restrict=lambda target: False) is False

    assert any("session" in record.getMessage() for record in caplog.records)


# --- the credential file -----------------------------------------------------


def test_the_env_file_is_restricted_at_startup(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_HASH=not-a-real-hash\n", encoding="utf-8")

    restricted = []
    connection.harden_env_file(env, restrict=lambda target: restricted.append(str(target)) or True)

    assert restricted == [str(env)]


def test_hardening_the_env_file_never_opens_it(tmp_path, monkeypatch):
    """A permissions fix must not become a second place that reads the secret."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_HASH=not-a-real-hash\n", encoding="utf-8")

    real_open = open

    def _refuse(path, *args, **kwargs):
        if str(path) == str(env):
            raise AssertionError("the credential file was opened while being hardened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _refuse)
    connection.harden_env_file(env, restrict=lambda target: True)


def test_a_missing_env_file_is_not_an_error(tmp_path):
    """A string-session install configured entirely through real environment
    variables has no `.env`, and that is a supported setup, not a failure."""
    connection.harden_env_file(tmp_path / "nothing-here", restrict=lambda target: False)


def test_an_unrepairable_env_file_is_reported(tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_HASH=not-a-real-hash\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="telegram_mcp"):
        connection.harden_env_file(env, restrict=lambda target: False)

    assert caplog.records, "an unfixable credential file was passed over in silence"


def test_the_warning_does_not_quote_the_credential_path(tmp_path, caplog):
    """The log line says what is wrong, not where the operator keeps their key."""
    env = tmp_path / "very-distinctive-directory-name" / ".env"
    env.parent.mkdir()
    env.write_text("TELEGRAM_API_HASH=not-a-real-hash\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="telegram_mcp"):
        connection.harden_env_file(env, restrict=lambda target: False)

    written = " ".join(record.getMessage() for record in caplog.records)
    assert "very-distinctive-directory-name" not in written
