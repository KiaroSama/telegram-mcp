"""The alias file on disk: addressing it, protecting it, reading and writing it.

Split out of ``aliases.py``, which was carrying four jobs at once. This is the
one with a filesystem in it, and it is the LOWER layer - it imports nothing from
``aliases``, so the dependency runs one way and there is no import cycle.

That is why the addressing lives here rather than upstairs. Which file to open
(``aliases_file_path``), which account scopes a row (``account_key``,
``sole_account_label``), what an alias normalises to as a key (``alias_key``) and
who is allowed to read the result (``restrict_to_owner``) are all part of
answering "where is this row and may I touch it" - the same question the store
exists to answer.

The locking earns its place too: two processes share one account's file, so a
read-modify-write must hold an exclusive lock or a concurrent save silently
drops the other's row. The lock has a timeout, because a stale lock file from a
crashed process must not wedge every later write.

Re-exported from ``aliases`` so nothing's import moves: two tool modules and
seven test files reach these names through that module.
"""

import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union


from telegram_mcp.safe_log import log_event
from telegram_mcp.owner_only import restrict_to_owner_strict
from telegram_mcp.settings import state_dir
from sanitize import sanitize_name

try:  # POSIX advisory locking.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None

try:  # the Windows equivalent; one of the two is always present.
    import msvcrt
except ImportError:  # pragma: no cover - platform dependent
    msvcrt = None


_ALIASES_ENV = "TELEGRAM_ALIASES_FILE"


_LEGACY_ALIASES_FILE = Path(__file__).resolve().parent.parent / "aliases.json"


def restrict_to_owner(path: Union[str, Path]) -> bool:
    """Make a file or directory reachable by its owner alone, and prove it.

    Delegates to :mod:`telegram_mcp.owner_only`. The previous implementation ran
    `icacls /inheritance:r /grant:r <user>:(F)` and returned whether the command
    exited 0 - which was not the same question. `/inheritance:r` drops INHERITED
    entries and `/grant:r` replaces the entry for the principal it names; an
    explicit entry belonging to anyone else survives untouched.

    Measured before the change: a file carrying `Everyone:(R)` still carried it
    afterwards, and this function returned True. The alias store, `.env` backups,
    file sessions, the event feed and the log all trusted that answer.

    True now means the object's DACL was read back and names this account only.
    Never raises: a permissions detail must not take a tool down - but a caller
    holding something sensitive must treat False as a refusal, not a warning.
    """
    return restrict_to_owner_strict(path)


def aliases_file_path() -> Path:
    """Runtime data location, never the install directory (may be read-only)."""
    override = os.getenv(_ALIASES_ENV)
    if override:
        return Path(override)
    return state_dir() / "aliases.json"


def alias_key(text: str) -> str:
    """Normalize an alias so visually identical spellings collide on purpose."""
    key = unicodedata.normalize("NFC", text).strip().lstrip("@").lower()
    key = key.replace("ё", "е")
    return " ".join(key.split())


def account_key(account: Optional[str]) -> Optional[str]:
    """Canonical form of an account label, matching what `get_client` looks up.

    Deliberately the SAME rule as the client registry rather than the stricter
    one the generator applies to a new label: a stored row has to match the label
    the running server actually resolved, or an account's own aliases stop
    resolving for it.
    """
    if account is None:
        return None
    try:
        return normalise_account_label(str(account)).lower()
    except ValueError:
        # A label the canonical rule refuses cannot name a configured account, so
        # there is nothing for it to key. Legacy rows written before this rule
        # existed fall here and stay unresolvable rather than matching the wrong
        # login - which is the safe direction for a row that names a recipient.
        return None


_LABEL_RE = re.compile(r"\A[A-Za-z0-9_]+\Z")


def normalise_account_label(raw: str) -> str:
    r"""Turn a typed account label into one that can survive a round trip through `.env`.

    The one rule, in the package rather than in the script that happened to need
    it first: a label is decided by whoever types it, written into an environment
    variable NAME by the generator and by the account manager, and read back by
    the client registry. Three copies of a rule drift, and the drift shows up as
    an account that saves without complaint and never loads.

    python-dotenv refuses to parse a line whose key contains a space: it warns on
    stderr and DROPS the line. "KGB Verifier" written literally produced
    `TELEGRAM_SESSION_STRING_KGB VERIFIER=...` - an account that sat in the file,
    looked correct, and could never load.

    Spaces and hyphens become underscores; anything the result still cannot be is
    refused, because there is no safe mapping to invent for it and refusing
    loudly beats guessing. That promise used to be documented and not kept: `---`
    collapsed to the empty label and `work=other` kept its `=`, which moves the
    split point of the `.env` line and files the account under a key nobody
    configured.

    Note `account_key` is the OTHER half: this is what a label may be, that is
    what a label already in use compares as.
    """
    label = re.sub(r"[\s\-]+", "_", raw.strip()).strip("_")
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"{raw!r} is not a usable account label: a label becomes part of an "
            "environment variable name, so it must be ASCII letters, digits and "
            "underscores, and cannot be empty."
        )
    return label


def sole_account_label() -> Optional[str]:
    """The only configured login's label, or None when there are none or several.

    Imported lazily and defensively: this module depends on nothing else in the
    package by design - an alias lookup must not need a connection - and
    `connection` builds its clients at import time, exiting when nothing is
    configured.
    """
    try:
        from telegram_mcp.connection import clients
    except (ImportError, SystemExit):  # pragma: no cover - unconfigured install
        return None
    return next(iter(clients)) if len(clients) == 1 else None


_ACCOUNT_SEPARATOR = "\n"


AliasKey = Tuple[Optional[str], str]


def _store_key(account: Optional[str], alias: str) -> str:
    """The single string a (account, alias) pair is stored under."""
    return f"{account}{_ACCOUNT_SEPARATOR}{alias}" if account else alias


def _split_store_key(raw: str) -> AliasKey:
    account, separator, alias = raw.partition(_ACCOUNT_SEPARATOR)
    if not separator:
        return None, alias_key(raw)
    return account_key(account), alias_key(alias)


_ALIAS_CACHE: Dict[str, Any] = {}


def _cache_stamp(path: Path):
    """What makes a cached parse still valid: same file, same write, same size."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _reset_alias_cache() -> None:
    """Drop every cached parse. For tests, mirroring effect_catalog._reset_catalog."""
    _ALIAS_CACHE.clear()


def load_aliases(strict: bool = False) -> Dict[AliasKey, Dict[str, Any]]:
    """Return {(account, key): {"id": int, "name": str|None, "account": str|None}}.

    Keyed by the pair, not the alias: chat ids are unique only within a login, so
    two logins must be able to call two different people "мама". The flat map this
    replaced let the second save delete the first.

    Legacy `{alias: id}` files, and rows that carried their account in the value
    rather than the key, upgrade on read. Never raises: this runs inside
    resolve_entity on every call, so a damaged file must not take chat tools down.
    """
    path = aliases_file_path()
    if not path.exists() and not os.getenv(_ALIASES_ENV):
        path = _LEGACY_ALIASES_FILE

    stamp = _cache_stamp(path)
    cached = _ALIAS_CACHE.get(str(path))
    if cached is not None and stamp is not None and cached[0] == stamp:
        # A COPY: update_aliases hands this map to migrate_legacy_rows and then to
        # the caller's mutate(), both of which change it in place. Sharing the
        # cached object would let one call's edit leak into every later read.
        return {key: dict(value) for key, value in cached[1].items()}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("aliases file must be a JSON object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as error:
        log_event(
            logging.WARNING,
            "ignoring an unreadable aliases file",
            error=error,
            path=path,
        )
        if strict:
            # Refuse to write over data we could not read: a degraded read plus a
            # write-back would silently delete every alias in the file.
            raise AliasStoreUnreadable(str(error)) from error
        return {}

    records: Dict[AliasKey, Dict[str, Any]] = {}
    for stored, value in raw.items():
        record = {"id": value} if not isinstance(value, dict) else dict(value)
        try:
            record["id"] = int(record["id"])
        except (KeyError, TypeError, ValueError):
            continue  # skip the bad row, keep every good one
        record["name"] = sanitize_name(str(record["name"])) if record.get("name") else None
        keyed_account, alias = _split_store_key(str(stored))
        # The value wins over the key: that is what upgrades a row written by the
        # version that scoped in the value and keyed flat.
        record["account"] = account_key(record.get("account")) or keyed_account
        records[(record["account"], alias)] = record

    if stamp is not None:
        _ALIAS_CACHE[str(path)] = (stamp, records)
    # A copy for the same reason the cache hit returns one: this map gets mutated.
    return {key: dict(value) for key, value in records.items()}


def _stored_row(key: Union[AliasKey, str], value: Any) -> tuple:
    """One (file key, record) pair, from either shape a caller may hold.

    Accepts a `(account, alias)` key or a bare alias, and a record or a bare id,
    so the store can be written by the tools, by a migration, or by a test that
    only cares about ids.
    """
    account, alias = key if isinstance(key, tuple) else (None, key)
    record = dict(value) if isinstance(value, dict) else {"id": int(value)}
    account = account_key(record.get("account")) or account_key(account)
    if account:
        record["account"] = account
    else:
        record.pop("account", None)
    return _store_key(account, alias_key(str(alias))), record


def save_aliases(aliases: Dict[Any, Any]) -> None:
    """Atomically persist aliases 0600 — the file maps nicknames to real people."""
    path = aliases_file_path()
    if not os.getenv(_ALIASES_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            recoverable = isinstance(existing, dict)
        except (OSError, ValueError):
            recoverable = False
        if not recoverable:
            # Never overwrite a file we could not parse; it may be hand-recoverable.
            path.replace(path.with_suffix(f".corrupt-{int(time.time())}"))

    payload = dict(_stored_row(key, value) for key, value in aliases.items())
    # mkstemp creates a fresh 0600 file with an unpredictable name: a fixed
    # ".tmp" is both a symlink target and a collision point between processes.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # Before the rename, so the file is never briefly readable under its real
        # name: an ACL travels with the file, and mkstemp's 0600 is POSIX-only.
        # False means the DACL could not be written - and on Windows that ACL is
        # the ONLY thing making this file private, because mkstemp's 0600 is a
        # POSIX guarantee. This file maps nicknames to real people. Publishing it
        # under its real name because a permissions call quietly failed is the
        # one outcome worth failing the write for, and it is what the contract at
        # the top of this module already demanded of callers: treat False as a
        # refusal, not a warning. connection.py and tools/events.py both do.
        if not restrict_to_owner(tmp):
            raise AliasStoreUnprotected(
                f"the alias store could not be made readable only by this account, "
                f"so it was not written: {path}"
            )
        os.replace(tmp, path)  # atomic: a crash leaves the previous file intact
        _ALIAS_CACHE.pop(str(path), None)
    except BaseException:
        os.unlink(tmp)
        raise


class AliasStoreUnreadable(Exception):
    """The alias file exists but could not be read, so writing would destroy it."""


class AliasStoreUnprotected(Exception):
    """The alias file could not be restricted to its owner, so it was not written."""


_LOCK_TIMEOUT_SECONDS = 10.0


_LOCK_RETRY_SECONDS = 0.05


def _lock_path(path: Path) -> str:
    return str(path) + ".lock"


def _lock_exclusive(lock_fd: int) -> bool:
    """Take the lock without blocking. False means someone else holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return False


def _unlock(lock_fd: int) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
    elif msvcrt is not None:
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def _try_acquire(path: Path) -> bool:
    """Probe whether the alias lock is free, taking and releasing it if so.

    Exists so the exclusivity of the lock can be proved from a second process,
    which is the only place the bug it guards against could ever show up.
    """
    lock_fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock_exclusive(lock_fd):
            return False
        _unlock(lock_fd)
        return True
    finally:
        os.close(lock_fd)


@contextmanager
def _alias_lock(path: Path):
    """Serialize read-modify-write cycles across processes.

    This used to be a bare ``yield`` wherever ``fcntl`` was missing, i.e. on all of
    Windows - the platform this project targets first. ``update_aliases`` loads the
    whole map, mutates it and writes it back, so two unserialized writers each
    saved their own copy and the second silently discarded the first. A deleted
    alias could reappear that way.

    ``msvcrt.locking`` is the Windows equivalent of ``flock`` and needs the byte it
    locks to exist, hence the one-byte file. The wait is bounded: an alias write
    must not become an unkillable wait if a holder dies badly.
    """
    lock_fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.path.getsize(_lock_path(path)) == 0:
            # msvcrt locks a byte range, so there has to be a byte to lock.
            os.write(lock_fd, b"\0")
            os.lseek(lock_fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while not _lock_exclusive(lock_fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"another process has held the alias lock for more than "
                    f"{_LOCK_TIMEOUT_SECONDS:.0f}s: {_lock_path(path)}"
                )
            time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            _unlock(lock_fd)
    finally:
        os.close(lock_fd)


def migrate_legacy_rows(aliases: Dict[AliasKey, Dict[str, Any]]) -> int:
    """Stamp pre-scoping rows with the login they can only have come from.

    Only ever runs where the answer is not a guess: with one login configured,
    a row saved before scoping existed was saved on that one. With several, the
    row stays as it is and `_resolvable` refuses it - see there.

    An explicit row for the same alias wins, so re-running this can never
    overwrite a scoped mapping with an older unscoped one.
    """
    sole = sole_account_label()
    if not sole:
        return 0
    migrated = 0
    for account, alias in [key for key in aliases if key[0] is None]:
        record = aliases.pop((account, alias))
        record["account"] = sole
        if aliases.setdefault((sole, alias), record) is record:
            migrated += 1
    return migrated


def update_aliases(mutate):
    """Apply `mutate(aliases)` to the alias file under an exclusive lock.

    Two tool calls that each load, change and save the whole map would otherwise
    lose one of the two writes — including a delete silently coming back.
    """
    path = aliases_file_path()
    if not os.getenv(_ALIASES_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
    with _alias_lock(path):
        aliases = load_aliases(strict=True)
        # Under the lock, before the caller sees the map: a mutate() that keys by
        # (account, alias) must not be handed rows it cannot address.
        migrate_legacy_rows(aliases)
        result = mutate(aliases)
        save_aliases(aliases)
        return result
