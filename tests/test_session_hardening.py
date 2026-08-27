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

The protection has to be in place BEFORE the database exists, not applied to it
afterwards. Telethon's constructor is what creates the file, so a fix that runs
after it has already published an unprotected auth key -- and the `-journal`,
`-wal` and `-shm` siblings SQLite makes later are not covered by any after-the-
fact sweep at all. What covers them is the directory: an owner-only directory
with inheritable entries means every file born in it is born owner-only, which
is measured here against a real SQLite session rather than asserted.

And because a session file IS the account, a protection failure stops that
account from starting. Running anyway would mean serving Telegram requests from
credentials the tests have just established are readable by somebody else.

`os.chmod` cannot express owner-only on Windows: it toggles the read-only
attribute and cannot clear the read bit. The mode assertions are therefore POSIX
and are marked as such; the Windows mechanism is a DACL written from scratch by
`telegram_mcp.owner_only`, and the tests that pin it read the DACL back off the
object.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from telethon.sessions import SQLiteSession

from telegram_mcp import connection, settings
from telegram_mcp.owner_only import restrict_to_owner_strict, verify_owner_only

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


def test_a_session_already_beside_the_installation_is_moved_somewhere_private(
    tmp_path, monkeypatch
):
    """It used to be left where it was, on the reasoning that moving a session
    database is moving the account. But the directory it was left in is a git
    checkout, or whatever directory the client happened to launch the server
    from, and neither can be made private without stripping the permissions off
    everything else in it -- measured, on this project's own checkout.

    So the account moves, once, into the directory this server owns. It arrives
    with every sidecar it had, and the name it used to answer to is gone rather
    than left behind holding a readable copy of the auth key."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    # Beside the INSTALLATION, which is the case this is about - the working
    # directory is a separate fallback with its own test.
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(connection, "script_dir", str(install))
    legacy = _real_session(install, name="telegram_session")
    (install / (legacy.name + "-wal")).write_bytes(b"pages")

    resolved = connection.session_file_path("telegram_session")
    assert resolved == settings.state_dir() / "telegram_session.session"

    connection.adopt_legacy_session(resolved)

    assert resolved.exists(), "the account did not arrive at its new home"
    assert not legacy.exists(), "a readable copy of the auth key was left behind"
    assert (resolved.parent / (resolved.name + "-wal")).exists(), "a sidecar was left behind"
    assert not (install / (legacy.name + "-wal")).exists()


def test_a_legacy_session_is_left_alone_when_a_managed_one_already_exists(tmp_path, monkeypatch):
    """Two databases with one name are two authorisations. Overwriting the one
    this server has been using with an older copy from beside the installation
    would swap the account out from under a running client."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(connection, "script_dir", str(install))
    legacy = _real_session(install, name="telegram_session")
    legacy.write_bytes(b"the-old-one")
    managed = settings.state_dir() / "telegram_session.session"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"the-one-in-use")

    connection.adopt_legacy_session(managed)

    assert managed.read_bytes() == b"the-one-in-use"
    assert legacy.exists(), "the legacy database was removed without being adopted"


def test_a_legacy_session_that_cannot_be_moved_is_a_refusal(tmp_path, monkeypatch):
    """Carrying on would mean running the account out of a directory that has
    just been shown to be unprotectable."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(connection, "script_dir", str(install))
    _real_session(install, name="telegram_session")

    def _refuse(self, target):
        raise OSError(13, "in use")

    monkeypatch.setattr(Path, "replace", _refuse)

    with pytest.raises(connection.SessionNotProtected):
        connection.adopt_legacy_session(settings.state_dir() / "telegram_session.session")


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


@pytest.mark.skipif(os.name != "nt", reason="POSIX has no DACL to leave a foreign entry on")
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


# --- the whole lifecycle, against a real SQLite session ----------------------


def test_hardening_a_session_that_does_not_exist_yet_does_not_claim_success(tmp_path, monkeypatch):
    """The call runs BEFORE Telethon's constructor, so there is no database and
    no sidecar to restrict. It used to walk the sidecar list, find nothing,
    never touch the directory the file was about to be created in, and return
    True -- reporting success for having restricted nothing at all."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    elsewhere = tmp_path / "a-directory-of-their-own"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)  # POSIX: explicitly not owner-only. A no-op on Windows.
    assert not verify_owner_only(elsewhere), "the fixture directory was already private"

    assert connection.harden_session_files(elsewhere / "work.session") is False


def _assert_born_private(target, directory, what: str) -> None:
    """Unreachable by anyone else, by whichever mechanism the platform provides.

    Windows hardens the directory with inheritable entries, so a file born inside
    it carries an owner-only DACL of its own and can be asked directly. POSIX has
    no such inheritance: SQLite makes its sidecars at whatever the umask allows,
    and what keeps them private is that the 0700 directory cannot be entered. So
    the file's mode is simply not the control there, and asserting it asserted ACL
    inheritance rather than who can read the auth key.
    """
    if os.name == "nt":
        assert verify_owner_only(target), f"{what} was not born owner-only"
        return
    assert verify_owner_only(
        directory
    ), f"{what} is private only while the directory holding it is, and it is not"


def test_a_session_in_the_managed_directory_is_born_owner_only(tmp_path, monkeypatch):
    """Not restricted afterwards: born that way. The database is created by
    Telethon's constructor, and anything applied after that has already left the
    auth key readable for as long as the constructor took."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = settings.state_dir() / "probe.session"

    assert connection.harden_session_files(path) is True

    session = SQLiteSession(str(path))
    session.set_dc(2, "149.154.167.51", 443)
    session.save()
    try:
        assert path.exists(), "the real session database was not created"
        _assert_born_private(path, settings.state_dir(), "the database")
    finally:
        session.close()


def test_a_sidecar_created_after_startup_is_owner_only_too(tmp_path, monkeypatch):
    """`-journal`, `-wal` and `-shm` hold pages of the same database. SQLite
    makes them when it feels like it, which is long after any startup sweep has
    run, so the thing that has to cover them is the directory they are born in."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = settings.state_dir() / "probe.session"
    assert connection.harden_session_files(path) is True

    session = SQLiteSession(str(path))
    session.set_dc(2, "149.154.167.51", 443)
    session.save()
    try:
        # A write after the client is up, which is when SQLite creates a sibling.
        session.set_dc(4, "149.154.167.92", 443)
        session.save()
        appeared = [
            p for p in settings.state_dir().iterdir() if p.name.startswith("probe.session")
        ]
        assert appeared
        for sibling in appeared:
            _assert_born_private(sibling, settings.state_dir(), sibling.name)
    finally:
        session.close()


def test_a_session_in_a_directory_the_operator_chose_is_refused_unless_it_is_private(
    tmp_path, monkeypatch
):
    """The database can be locked down after the fact; the directory it lives in
    cannot, because locking down a directory the operator picked strips the
    permissions off everything else they keep there. So the sidecars SQLite has
    not created yet are unprotectable, and saying so is the only honest answer."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    elsewhere = tmp_path / "a-directory-of-their-own"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)  # POSIX: explicitly not owner-only. A no-op on Windows.
    path = _real_session(elsewhere)
    assert not verify_owner_only(elsewhere), "the fixture directory was already private"

    assert connection.harden_session_files(path) is False
    assert verify_owner_only(path), "the database itself was left alone as well"


def test_a_private_directory_the_operator_chose_is_accepted(tmp_path, monkeypatch):
    """The refusal above is about the directory's permissions, not about who
    picked it. An operator who has already made it private is taken at the
    evidence."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    elsewhere = tmp_path / "a-vault-of-their-own"
    elsewhere.mkdir()
    assert restrict_to_owner_strict(elsewhere), "could not make the fixture private"
    path = _real_session(elsewhere)

    assert connection.harden_session_files(path) is True


def test_a_protection_failure_stops_the_account_from_starting(tmp_path, monkeypatch):
    """A session file IS the account: whoever can read it is signed in, with no
    password and no second factor. Building a client over one that could not be
    protected serves Telegram requests out of a credential this process has just
    established is readable by somebody else."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(connection, "restrict_to_owner", lambda target: False)
    monkeypatch.setattr(connection, "TelegramClient", lambda *a, **k: object())

    with pytest.raises(connection.SessionNotProtected):
        connection._build_client("work", "work")


def test_a_string_session_is_not_affected_by_the_file_checks(tmp_path, monkeypatch):
    """There is no file, so there is nothing to protect and nothing to refuse."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(connection, "restrict_to_owner", lambda target: False)
    monkeypatch.setattr(connection, "TelegramClient", lambda *a, **k: "built")

    assert connection._build_client(object(), "work") == "built"


def test_a_built_file_session_is_owner_only_end_to_end(tmp_path, monkeypatch):
    """The real constructor, the real database, the real DACL."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(connection, "script_dir", str(tmp_path / "install"))

    built = {}

    def _client(session, *args, **kwargs):
        real = SQLiteSession(str(session))
        real.set_dc(2, "149.154.167.51", 443)
        real.save()
        built["session"] = real
        return real

    monkeypatch.setattr(connection, "TelegramClient", _client)

    connection._build_client("work", "work")
    try:
        path = settings.state_dir() / "work.session"
        assert path.exists()
        assert verify_owner_only(path)
        assert verify_owner_only(settings.state_dir())
    finally:
        built["session"].close()


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
