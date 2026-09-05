"""Isolate the test session from the operator's real environment.

``telegram_mcp/runtime.py`` calls ``load_dotenv()`` at import time, so importing
it during a test run reads whatever ``.env`` happens to sit in the repository
root. That made the suite's result depend on the machine it ran on: on a host
whose ``.env`` sets ``TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK``, the two deny-all
assertions in ``tests/test_file_path_security.py`` fail, because the production
code correctly takes the documented opt-in branch. Forcing that one variable off
proved it — the same file goes from ``2 failed, 9 passed`` to ``11 passed``.

Pinning that single variable would be a symptom fix: every other ``TELEGRAM_*``
key in an operator's ``.env`` leaks the same way. This neutralises the source
instead, so the suite sees a known environment whatever the host holds.

This file lives at the repository root on purpose. pytest loads the ancestor
``conftest.py`` chain before it imports any test module, which is the only point
early enough to matter; ``tests/conftest.py`` is upstream's file, and the fork
keeps its own work in files of its own so ``git merge upstream/main`` stays
clean.

Production behaviour is untouched: the fallback is opt-in by design and works as
documented. A test that wants either branch sets it explicitly with monkeypatch,
which is the point — the setting becomes part of the test rather than part of
the machine.
"""

import atexit
import os
import shutil
import tempfile

import dotenv

# Anything the shell exported goes too: ambient configuration is exactly what
# must not decide a test result.
for _key in [key for key in os.environ if key.startswith("TELEGRAM_")]:
    del os.environ[_key]

# Do this BEFORE telegram_mcp is imported anywhere. runtime.py does
# `from dotenv import load_dotenv`, which binds whatever this attribute holds at
# import time, so replacing it here makes that call a no-op for the session.
# Note that python-dotenv does not override existing variables anyway — the
# values set below would survive regardless — but a key nothing sets here would
# still be inherited from the file, and that is the leak being closed.
dotenv.load_dotenv = lambda *args, **kwargs: False

# The three the import path genuinely requires, matching tests/conftest.py.
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")

# A file-based session under a BARE name resolves into the real private state
# directory - the same one a configured account uses. Any test that reaches a
# real Telethon constructor then writes a GENUINE auth key there, under a name
# the next run reopens. That is not hypothetical: a live login was found in
# `test_session.session` sitting beside the operator's own accounts, and it had
# to be terminated from Telegram rather than merely deleted from disk.
#
# An absolute name is honoured where it points (`session_files.session_file_path`),
# so pointing the suite at a per-run temporary directory makes the same mistake
# write somewhere disposable. `ignore_errors` on the way out because Windows
# will not unlink a session SQLite file a leaked handle still holds - and a
# failure to clean up must not fail the run.
_TEST_SESSION_DIR = tempfile.mkdtemp(prefix="telegram-mcp-test-session-")
atexit.register(shutil.rmtree, _TEST_SESSION_DIR, True)

# The server REFUSES to open a session under a directory it cannot make
# owner-only, and on Windows a fresh temp directory inherits the ACL of %TEMP%.
# So harden it with the project's own helper rather than weaken the check: the
# temp directory then carries the same guarantee the private state directory
# does. `owner_only` imports nothing but the standard library, so this cannot
# disturb the scrubbing above.
from telegram_mcp.owner_only import restrict_to_owner_strict  # noqa: E402

if not restrict_to_owner_strict(_TEST_SESSION_DIR):  # pragma: no cover - platform
    raise RuntimeError(
        f"could not make {_TEST_SESSION_DIR} owner-only, so the suite would be "
        "writing session files somewhere world-readable - refusing to run"
    )

os.environ.setdefault("TELEGRAM_SESSION_NAME", os.path.join(_TEST_SESSION_DIR, "test_session"))
