"""The one way this project writes a log line.

A log file outlives the call that wrote it, is copied into bug reports, and on a
shared machine is read by whoever can read the operator's home directory. What
lands in it therefore has to be decided by an allowlist, not by whatever a
formatter happened to render.

That is not something a filter can do afterwards. ``RedactingFilter`` in
``connection`` scrubs the shapes it was told about -- a session string, an invite
link, a bot token -- and it is the right net for text this project did not
construct. It cannot recognise a chat title, a contact's name, a search query or
a caption, and an exception carries whatever the failing call was given: a
``logger.exception`` hands the handler a rendered traceback ending in exactly
that text. A canary planted in an exception came straight back out of the log.

So the decision moves to the writer. :func:`log_event` is the only logging
primitive the package uses, and it admits exactly three kinds of thing:

* the *event*, which this project writes as a literal;
* structural context -- numbers, booleans, ``None`` -- because chat ids, message
  ids, limits and flags are what make a line actionable and none of them is
  content;
* everything else reduced by :func:`safe_value` to type, length and an eight-hex
  digest: enough to see that two reports concern the same input, not enough to
  read it.

The digest is what keeps a bounded line useful. The same failure keeps the same
eight characters across restarts, so two reports can still be compared.

``tests/test_logging_discipline.py`` fails the build if a direct
``logger.error``/``logger.exception`` call appears anywhere else in the package.

Deliberately at the bottom of the import graph: ``connection``, ``aliases``,
``file_roots`` and ``runtime`` all write log lines, and none of them may import
each other to do it.
"""

import hashlib
import logging
import traceback
from typing import Any

logger = logging.getLogger("telegram_mcp")

# How many innermost frames of a failure are worth keeping. Enough to name the
# call that failed and who called it; not the whole stack, which is mostly this
# server's own plumbing repeated in every report.
_LOGGED_FRAMES = 6


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def safe_value(value: Any) -> Any:
    """What may be written about one context value.

    Numbers, booleans and ``None`` pass through: they are structure, not content.
    Everything else becomes its type, its size and a short digest.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else repr(value)
    return f"<{type(value).__name__} len={len(text)} #{_digest(text)}>"


def safe_exception(error: BaseException) -> str:
    """Everything about a failure that may be written down.

    Type, size and digest -- the same allowlist :func:`safe_value` applies -- plus
    the innermost frames, which say where it happened without quoting anything
    the caller supplied. ``raise X from Y`` renders both, so the cause is named
    by type as well; one link, no recursion.
    """
    text = str(error)
    parts = [f"{type(error).__name__} len={len(text)} #{_digest(text)}"]

    cause = error.__cause__ or error.__context__
    if cause is not None:
        parts.append(f"caused by {type(cause).__name__}")

    frames = traceback.extract_tb(error.__traceback__)[-_LOGGED_FRAMES:]
    if frames:
        # PurePosixPath/PureWindowsPath would each be wrong on the other host;
        # rpartition on both separators is the same answer everywhere.
        names = [
            f"{frame.filename.rpartition('/')[2].rpartition(chr(92))[2]}"
            f":{frame.lineno}:{frame.name}"
            for frame in reversed(frames)
        ]
        parts.append("at " + " <- ".join(names))
    return " ".join(parts)


def log_event(level: int, event: str, /, **context: Any) -> None:
    """Record one bounded line. The only logging call in the package.

    ``event`` is a literal this project writes; ``context`` values are reduced by
    :func:`safe_value`; a ``BaseException`` under the key ``error`` is reduced by
    :func:`safe_exception`. No ``exc_info``, ever -- that is the argument that
    hands a formatter the exception's own text.

    ``level`` and ``event`` are positional-only, and the failure travels as a
    context key rather than a named parameter, so a caller forwarding arbitrary
    ``**kwargs`` (which ``log_and_format_error`` does, 175 times) cannot collide
    with this signature and turn a log line into a TypeError.
    """
    error = context.pop("error", None)
    if not isinstance(error, BaseException):
        # Some caller's ordinary context that happens to be called `error`.
        if error is not None:
            context["error"] = error
        error = None

    parts = [event]
    if context:
        parts.append(" ".join(f"{key}={safe_value(value)}" for key, value in context.items()))
    if error is not None:
        parts.append(f"failure: {safe_exception(error)}")
    logger.log(level, " | ".join(parts))


# Historic private names: `runtime` exported these and its own tests import them.
_safe_context_value = safe_value
_safe_exception = safe_exception


__all__ = [
    "log_event",
    "logger",
    "safe_exception",
    "safe_value",
    "_safe_context_value",
    "_safe_exception",
]
