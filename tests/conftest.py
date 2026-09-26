"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

from telegram_mcp import secret_backend, secret_history

from secret_fakes import SECRET_ID, FakeChat, FakeManager


# The suite describes the CODE, never the machine it runs on.
#
# Startup discovery now resolves the `.env` the way the reload path always did -
# `find_dotenv(usecwd=True)` - so on a developer's own checkout it finds THEIR
# configuration. Before, the two disagreed about which file they meant, and the
# tests were quietly relying on startup finding nothing.
#
# What that dependence costs is not hypothetical: with a real `.env` present the
# registry holds that person's accounts, every non-readonly tool becomes
# multi-account and refuses a call with no `account`, and a hundred tests fail on
# one machine and pass on another. It also points the suite at live logins.
#
# Stubbed at module scope because `telegram_mcp.connection` builds its registry
# at IMPORT time, which happens while the first test module is collected - a
# fixture would be far too late.
def _no_dotenv_for_tests(*_args, **_kwargs):
    return ""


import dotenv  # noqa: E402

dotenv.find_dotenv = _no_dotenv_for_tests
dotenv.main.find_dotenv = _no_dotenv_for_tests

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")
for _name in list(os.environ):
    # Any account the developer's environment supplies, out of the way too: a
    # test that means to configure two accounts says so itself.
    if _name.startswith(("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME_")):
        del os.environ[_name]


@pytest.fixture(autouse=True)
def _secret_backend_not_shutting_down():
    """Clear the shutdown latch between tests.

    `secret_backend.close_all()` sets `_closing` and deliberately never clears
    it: once a shutdown has begun, starting a new manager against a key store
    being flushed is never right. That is correct for a process and poisonous
    for a test session, where one suite calling `close_all` left every later
    `secret_manager` in any file answering "the server is shutting down" -
    which CI found and a local run, in a different order, did not.

    Autouse and here rather than in each suite, because the leak is a property
    of the module rather than of any one test file.
    """
    from telegram_mcp import admission, secret_backend

    secret_backend._closing = False
    # The same shape one module along: `admission.release_all()` closes the door
    # so a slow acquire cannot publish after shutdown, and every suite's cleanup
    # calls it. Without this, the first cleanup left every later admission in the
    # session silently refusing to publish.
    admission.begin_serving()
    admission.unreleased_leases.clear()
    yield
    secret_backend._closing = False
    admission.begin_serving()
    admission.unreleased_leases.clear()


@pytest.fixture
def wire_client(monkeypatch):
    """Patch a tool module's client seams in one call.

    There were 33 hand-rolled copies of this across 28 test files, and that is
    the direct reason ~38 registered tools have no behavioural test: writing one
    started with 25 lines of boilerplate before it could assert anything.

    Patch the module that OWNS each name. The tool modules star-import from
    `runtime`, so patching through `runtime` binds a second name and changes
    nothing the tool actually calls - the trap `tests/test_tool_registry.py`
    exists to catch.
    """

    def _wire(module, client, *, resolve=None, entity=None, marked_id=None):
        # `with_account` refreshes before it decides single- or multi-mode, which
        # it must: the registry only moves when something refreshes it, so a
        # second account added while the server ran was invisible to the routing
        # decision. In a test that means the REAL `.env` would be read and the
        # machine's own accounts published - so the wired client is the whole
        # registry here, and the reload is a no-op.
        from telegram_mcp import connection as conn

        monkeypatch.setattr(conn, "refresh_accounts", lambda: [])
        monkeypatch.setattr(conn, "clients", {"default": client})

        async def _resolve_entity(chat_id, cl=None, account=None):
            if resolve is not None:
                return await resolve(chat_id, cl, account)
            return entity if entity is not None else object()

        async def _ensure_connected(_client=None):
            return None

        monkeypatch.setattr(module, "get_client", lambda account=None: client)
        for name, value in (
            ("ensure_connected", _ensure_connected),
            ("resolve_entity", _resolve_entity),
            ("resolve_input_entity", _resolve_entity),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
        if marked_id is not None and hasattr(module, "get_marked_id"):
            monkeypatch.setattr(module, "get_marked_id", marked_id)
        return client

    return _wire


# The allow-list a test sets must survive the test.
#
# `refresh_server_roots()` runs on the path of EVERY file tool and rebuilds
# `SERVER_ALLOWED_ROOTS` from the machine's configuration - but only when the
# configuration string differs from `_last_named_roots`, a module-level cache.
# So exactly one call per process does the rebuild and every later call returns
# early, which is why this surfaced as an ordering bug rather than a failure:
# whichever test happened to be FIRST through a file tool had its roots replaced
# by the real ones, and every test after it was untouched.
#
# Measured on 2026-09-20: `pytest tests/test_file_roots.py tests/test_media_album.py`
# failed with "send_file is disabled until allowed roots are configured" while
# each file passed alone. On a machine with a configured root the test's root was
# swapped for that one; in CI, where there is no configuration, the list was
# emptied outright.
#
# The fix is to make the cache ALREADY CURRENT before each test, which is the
# state a long-running server is in after its first call - so the rebuild does
# not fire, and a test that genuinely exercises refreshing still works because it
# changes the configuration itself. The contents are then restored afterwards,
# because `runtime` and `main` star-import this list and hold the same object;
# `file_roots.py`'s own module docstring says to patch the CONTENTS, never the
# name, and 44 call sites in this suite do it the other way round.
@pytest.fixture(autouse=True)
def _allowed_roots_survive_the_test():
    from telegram_mcp import file_roots

    original_roots = list(file_roots.SERVER_ALLOWED_ROOTS)
    original_cache = file_roots._last_named_roots
    try:
        file_roots._last_named_roots = file_roots._roots_from_file()
    except Exception:
        # A configuration this test cannot read is not a reason to fail it; the
        # rebuild would have returned early for the same reason.
        pass

    yield

    file_roots.SERVER_ALLOWED_ROOTS[:] = original_roots
    file_roots._last_named_roots = original_cache


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """Install a `FakeManager` behind `secret_manager`, with history in `tmp_path`.

    Here rather than beside the fake it builds, because a fixture that is IMPORTED
    shadows itself at every use site - sixty-seven `F811 redefinition of unused
    backend` in one lint run. pytest finds it here without an import.

    The history store is redirected too, because these tools now WRITE to it on
    every send - a suite pointing at the operator's real state directory would be
    a test that edits the machine it runs on.
    """
    monkeypatch.setattr(secret_history, "_cache", {})
    monkeypatch.setattr(secret_history, "state_dir", lambda: tmp_path)

    manager = FakeManager([FakeChat(SECRET_ID)])

    async def _manager(account):
        return manager

    monkeypatch.setattr(secret_backend, "secret_manager", _manager)
    for module in (
        "telegram_mcp.tools.secret_chats",
        "telegram_mcp.tools.secret_messaging",
        "telegram_mcp.tools.secret_actions",
        "telegram_mcp.tools.secret_timed",
    ):
        monkeypatch.setattr(f"{module}.secret_manager", _manager, raising=False)
        # A function looks a name up in ITS OWN module globals, so a seam shared
        # by four modules has to be patched in all four or the patch succeeds
        # while missing the caller under test.
        monkeypatch.setattr(f"{module}._account_label", lambda account=None: "acct", raising=False)
    return manager


# The safeguard's folder rule (FR-030..FR-034) must not reach the real machine from a
# test: `files/downloads` would be created inside the checkout, and the owner's real
# "always allow" folders would silently widen what a test's file tool may open. Each
# test gets its own installation folder, grants file and approval-message registry.
@pytest.fixture(autouse=True)
def _folders_are_the_tests_own(tmp_path_factory, monkeypatch):
    from telegram_mcp.safeguard import folders, grants, sealed

    install = tmp_path_factory.mktemp("install")
    monkeypatch.setattr(folders, "install_dir", lambda: install)
    monkeypatch.setattr(
        grants, "grants_path", lambda: install.parent / f"{install.name}-grants.json"
    )
    monkeypatch.setattr(
        sealed, "sealed_path", lambda: install.parent / f"{install.name}-sealed.json"
    )
    grants.reset_cache()
    sealed.reset_cache()
    yield install
    grants.reset_cache()
    sealed.reset_cache()
