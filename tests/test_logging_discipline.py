"""One primitive writes the log, and nothing may write around it.

`log_and_format_error` was already bounded when this was audited. The leak was
everywhere else: about sixty `logger.exception(...)` calls sitting directly above
it, plus warnings that interpolated a chat id, a title, a path or a raw exception
with `%s`. `logger.exception` hands the handler a rendered traceback ending in
the exception's own text, and an exception carries whatever the failing call was
given -- a caption, a filename, a contact's name, a search query.

A filter cannot fix that after the fact. `RedactingFilter` scrubs the shapes it
was told about; it has no way to recognise a chat title. So the decision moved to
the writer, and this module holds the two tests that keep it there:

* a **canary** -- a unique string planted in an exception and in a context value
  must not appear in the file the handler wrote;
* a **source rule** -- no module in the package may call a logging method on a
  logger directly. `telegram_mcp/safe_log.py` is the one exception, because it is
  the primitive.

The source rule is the one that survives contact with a future change: the
canary proves today's call sites are clean, and the rule is what fails the build
when a new `logger.exception` is added tomorrow.
"""

import ast
import logging
import pathlib

import pytest

# The logging machinery moved out of `connection` into `log_setup`: writing a
# log file and reaching Telegram are different jobs, and one file was carrying
# both. Patch and call the module that OWNS these names - `connection` still
# re-exports them for callers, but a patch applied there is a second name the
# handler never reads.
from telegram_mcp import log_setup, runtime, safe_log

CANARY = "canary-7Kq2Vz-must-not-be-logged"

# The primitive itself, which is allowed to call the logger, and nothing else.
_PRIMITIVE = "safe_log.py"

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "telegram_mcp"


def _logger_calls(tree: ast.AST):
    """Every call of a logging method on something that looks like a logger."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id.lower().endswith(("logger", "log")):
            yield node, f"{target.id}.{func.attr}"
        elif isinstance(target, ast.Call):
            # `logging.getLogger("...").exception(...)` -- the same leak wearing
            # a longer name, and four of these were live when this was written.
            inner = target.func
            if isinstance(inner, ast.Attribute) and inner.attr == "getLogger":
                yield node, f"getLogger().{func.attr}"


def test_no_module_logs_around_the_primitive():
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == _PRIMITIVE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, name in _logger_calls(tree):
            offenders.append(f"{path.relative_to(_PACKAGE.parent)}:{node.lineno}: {name}(...)")

    assert offenders == [], (
        "these call the logger directly instead of telegram_mcp.safe_log.log_event, "
        "which is the only place that decides what a log line may contain:\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_passes_exc_info_to_a_log_call():
    """`exc_info=True` is the argument that hands a formatter the exception text."""
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "exc_info":
                    offenders.append(f"{path.relative_to(_PACKAGE.parent)}:{node.lineno}")
    assert offenders == [], "exc_info renders the exception's own text: " + ", ".join(offenders)


@pytest.fixture
def logged(tmp_path, monkeypatch):
    """The real handler over a throwaway file, wired in place of the global one."""
    path = tmp_path / "mcp_errors.log"
    handler = log_setup._make_file_handler(str(path))
    test_logger = logging.getLogger("telegram_mcp.test-discipline")
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    test_logger.addHandler(handler)
    monkeypatch.setattr(safe_log, "logger", test_logger)
    try:
        yield path
    finally:
        test_logger.removeHandler(handler)
        handler.close()


def test_a_canary_in_an_exception_never_reaches_the_log(logged):
    try:
        raise RuntimeError(f"could not open {CANARY} for the chat {CANARY}")
    except RuntimeError as error:
        safe_log.log_event(logging.ERROR, "a bounded event", error=error, title=CANARY)

    written = logged.read_text(encoding="utf-8")
    assert CANARY not in written
    # Still useful: the type and a stable digest survive, so two reports of the
    # same failure can be told apart from two different ones.
    assert "RuntimeError" in written
    assert "a bounded event" in written


def test_a_canary_in_a_cause_never_reaches_the_log(logged):
    """`raise X from Y` renders both, so scrubbing the outer one is not enough."""
    try:
        try:
            raise ValueError(CANARY)
        except ValueError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as error:
        safe_log.log_event(logging.ERROR, "a wrapped failure", error=error)

    written = logged.read_text(encoding="utf-8")
    assert CANARY not in written
    assert "caused by ValueError" in written


def test_structural_context_survives_because_it_is_what_makes_a_line_actionable(logged):
    safe_log.log_event(
        logging.ERROR, "an event", chat_id=-1001234567890, limit=50, flag=True, missing=None
    )

    written = logged.read_text(encoding="utf-8")
    assert "-1001234567890" in written
    assert "limit=50" in written
    assert "flag=True" in written


def test_the_same_failure_keeps_the_same_digest_across_calls(logged):
    first = safe_log.safe_exception(ValueError("boom"))
    second = safe_log.safe_exception(ValueError("boom"))
    third = safe_log.safe_exception(ValueError("bang"))

    assert first == second
    assert first != third


def test_the_error_formatter_still_routes_through_the_primitive(logged, monkeypatch):
    """`log_and_format_error` is a caller of the primitive, not a second writer."""
    returned = runtime.log_and_format_error(
        "search_messages", ValueError(CANARY), query=CANARY, chat_id=-1001234567890
    )

    written = logged.read_text(encoding="utf-8")
    assert CANARY not in written
    assert CANARY not in returned
    assert "-1001234567890" in written


# --- where the log lives, and who may read it -------------------------------


def test_the_log_lives_in_the_state_directory_not_beside_the_installation():
    """Beside main.py the log landed inside the git checkout: committed or synced
    by accident, inheriting whatever that directory grants, and unwritable
    wherever the install is read-only."""
    from telegram_mcp import settings

    assert pathlib.Path(log_setup.log_file_path).parent == settings.state_dir()
    assert pathlib.Path(log_setup.log_file_path).name == "mcp_errors.log"


def test_every_log_file_is_restricted_on_creation_and_on_rotation(tmp_path, monkeypatch):
    """`os.chmod(0o600)` is not owner-only on Windows: it toggles the read-only
    attribute and cannot clear the read bit, so the POSIX-only call left the log
    readable by every account on the machine this project targets first -- while
    the POSIX-only test passed. Rotation matters as much as creation: a log
    created after a rollover is exactly as sensitive as the first one."""
    restricted = []
    monkeypatch.setattr(log_setup, "restrict_to_owner", lambda path: restricted.append(str(path)))

    path = tmp_path / "mcp_errors.log"
    handler = log_setup._make_file_handler(str(path))
    try:
        handler.maxBytes = 64
        record = logging.LogRecord(
            "telegram_mcp", logging.ERROR, __file__, 1, "x" * 200, None, None
        )
        handler.emit(record)
        handler.emit(record)
    finally:
        handler.close()

    assert restricted, "the log file was created without being restricted to its owner"
    assert len(restricted) >= 2, "a rotation produced a fresh log that nobody restricted"
    assert all(name == str(path) for name in restricted)
