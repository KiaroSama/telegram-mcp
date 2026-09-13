"""What the account configuration SAYS right now, and what changed since.

Pure functions over the `.env` file and the process environment: which file is
read, a content digest of it, which variables configure an account, and which of
those the environment supplied rather than the file. Nothing here builds a
client, opens a socket or mutates the live registry - `connection` does that,
and split this out when the provenance tracking pushed it past the size ceiling.

Two facts the rest of the package depends on:

* **Provenance is decidable only at import.** `load_dotenv` adds to `os.environ`
  and never removes, so after the first edit a variable deleted from the file is
  indistinguishable from one the environment always supplied.
* **The fingerprint hashes CONTENT, not metadata.** A re-login rewrites `.env`
  with a session string of the same length, which moves neither size nor - on a
  coarse-resolution filesystem - mtime.
"""

import os
from typing import Optional


def _env_file() -> Optional[str]:
    """The `.env` this process reads, or None when it runs on real env vars."""
    try:
        from dotenv import find_dotenv

        return find_dotenv(usecwd=True) or None
    except Exception:
        return None


def _env_fingerprint(path: Optional[str]) -> tuple:
    """A digest of the file's CONTENT, not its metadata.

    This was `(st_mtime_ns, st_size)` and that was wrong. Replacing one session
    string with another of the same length changes neither: the account manager
    rewrites `.env` wholesale, so a re-login is exactly a same-size rewrite, and
    on a filesystem whose timestamp resolution is coarser than the gap between
    the two writes the mtime does not move either. CI caught it on a Windows
    runner where the local machine never had.

    Hashing a file of a few kilobytes costs microseconds and is checked once per
    `get_client`. The stat was the cheaper answer to the wrong question.
    """
    if not path:
        return ()
    try:
        with open(path, "rb") as handle:
            return (_account_digest_bytes(handle.read()),)
    except OSError:
        return ()


def _account_digest_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _account_digest(value: str) -> str:
    """A session string is a full login; only ever its digest is kept."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_ACCOUNT_PREFIXES = ("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")


def _external_account_vars() -> dict:
    """Account variables the ENVIRONMENT supplies, not the `.env` file.

    Provenance is decidable exactly once, and only at import: a variable that is
    in `os.environ` but not in the file was set outside it. Afterwards the
    question is unanswerable, because `load_dotenv` adds to `os.environ` and
    never removes, so a variable later deleted from the file still sits there
    looking externally configured.
    """
    from dotenv import dotenv_values

    path = _env_file()
    on_disk = dotenv_values(path) if path else {}
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_ACCOUNT_PREFIXES) and value and key not in on_disk
    }


# Decided once, at import, for the reason the docstring above gives.
_EXTERNAL_ACCOUNT_VARS: dict = _external_account_vars()


def _accounts_from_disk() -> dict:
    """The environment as it would be if this process had started right now.

    File-managed account variables come from the FILE alone, everything else
    from the live process. That asymmetry is the point: `load_dotenv` can add a
    variable to `os.environ` but never removes one, so an account deleted from
    `.env` would otherwise stay configured forever.

    What the asymmetry must NOT do is delete an account the file never owned.
    Dropping every account-prefixed variable and re-adding only the file's meant
    an unrelated edit to `.env` silently unconfigured an account supplied as a
    real environment variable - so `_EXTERNAL_ACCOUNT_VARS`, decided at import,
    is re-added first and the file still wins wherever both name the same key.
    """
    from dotenv import dotenv_values

    path = _env_file()
    on_disk = dotenv_values(path) if path else {}
    env = {k: v for k, v in os.environ.items() if not k.startswith(_ACCOUNT_PREFIXES)}
    env.update(_EXTERNAL_ACCOUNT_VARS)
    env.update({k: v for k, v in on_disk.items() if v})
    return env


def _current_digests(env: dict) -> dict:
    return {
        key: _account_digest(value)
        for key, value in env.items()
        if key.startswith(_ACCOUNT_PREFIXES) and value
    }


__all__ = [
    "_ACCOUNT_PREFIXES",
    "_account_digest",
    "_account_digest_bytes",
    "_accounts_from_disk",
    "_current_digests",
    "_env_file",
    "_env_fingerprint",
    "_EXTERNAL_ACCOUNT_VARS",
    "_external_account_vars",
]
