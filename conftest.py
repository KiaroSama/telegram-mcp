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

import os

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
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")
