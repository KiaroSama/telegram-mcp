"""Where this server's log records go, and what they are allowed to contain.

Split out of ``connection.py``, which had grown to a thousand lines covering two
unrelated jobs: reaching Telegram, and writing a log file. Nothing here knows
about a TelegramClient, and nothing in the connection path calls into it - the
only coupling was that both lived in one file.

The log used to be a plain append-only FileHandler: whatever mode the umask gave
it, no size ceiling, and every byte a caller passed written out verbatim. A
planted canary came straight back out of it. What replaced that is here.

`logger` itself belongs to ``safe_log``, which is the only module allowed to call
a method on it. This module decides where its records land.
"""

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

from pythonjsonlogger.json import JsonFormatter

from telegram_mcp.aliases import restrict_to_owner
from telegram_mcp.safe_log import log_event, logger
from telegram_mcp.settings import state_dir

# --- Logging: bounded, owner-only, and redacted ------------------------------
#
# The log used to be a plain append-only FileHandler: whatever mode the umask
# gave it (0644 on a normal host), no size ceiling, and every byte a caller
# passed written out verbatim. A planted canary came straight back out of it.

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
_REDACTED = "[REDACTED]"

# Values that are secrets by shape, wherever in a line they appear. This is the
# net for text we do not construct ourselves -- a Telethon exception quoting the
# session, an invite link echoed back by Telegram. Context this project builds
# is redacted by key in `runtime.log_and_format_error`, which does not have to
# guess.
_SECRET_SHAPES = (
    # Telethon StringSession: '1' + a long base64 run.
    re.compile(r"\b1[A-Za-z0-9+/=_-]{40,}"),
    # Invite links and bare joinchat/+ hashes: bearer credentials for a chat.
    re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[A-Za-z0-9_-]+", re.IGNORECASE),
    # Bot tokens.
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
)

# Environment variables whose VALUE is a secret. Scrubbing the literal value
# catches it however it got into the text.
_SECRET_ENV_MARKERS = ("SESSION_STRING", "API_HASH", "PASSWORD", "SECRET", "TOKEN")


def _secret_env_values() -> list:
    """The literal secrets this process was configured with, longest first."""
    values = [
        value
        for key, value in os.environ.items()
        if value and len(value) >= 8 and any(m in key.upper() for m in _SECRET_ENV_MARKERS)
    ]
    return sorted(set(values), key=len, reverse=True)


def redact(text: str) -> str:
    """Replace anything that is a secret by shape or by configured value."""
    for value in _secret_env_values():
        text = text.replace(value, _REDACTED)
    for pattern in _SECRET_SHAPES:
        text = pattern.sub(_REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub every record -- message, arguments and traceback -- before it lands.

    Attached to the HANDLERS rather than the logger so it also covers records
    that propagate up from a child logger, and so no formatter can re-render an
    exception this filter has already cleaned: the rendered traceback is folded
    into the message and ``exc_info`` is dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken %-format is not a leak
            message = str(record.msg)
        if record.exc_info:
            message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
        record.msg = redact(message)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that leaves the file readable by its owner alone.

    Applied on every rollover too: a log created after a rotation is as
    sensitive as the first one, and the umask would have given it 0644.

    `restrict_to_owner`, not `os.chmod(0o600)`. On Windows chmod toggles the
    read-only attribute and cannot clear the read bit, so the POSIX-only call
    left the log readable by every account on the machine this project
    targets first -- while the POSIX-only test passed.
    """

    def _open(self):
        stream = super()._open()
        restrict_to_owner(self.baseFilename)
        return stream


def _make_file_handler(path: str) -> logging.Handler:
    """A bounded, owner-only, redacting JSON handler for ``path``."""
    handler = _OwnerOnlyRotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(logging.ERROR)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RedactingFilter())
    return handler


# Setup robust logging with both file and console output. `logger` itself is
# owned by `safe_log`, which is also the only module allowed to call a method on
# it; this module wires up where its records go.
logger.setLevel(logging.ERROR)  # Set to ERROR for production, INFO for debugging

# Create console handler. Explicitly stderr: on the stdio transport stdout is the
# MCP protocol channel and carries complete tool results, so nothing diagnostic
# may share it. StreamHandler's default happens to be stderr; saying so keeps it
# from being changed by accident.
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setLevel(logging.ERROR)  # Set to ERROR for production, INFO for debugging
console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
)
# stderr is captured by the MCP client and often by a shell log, so it needs the
# same treatment as the file.
console_handler.addFilter(RedactingFilter())

# The log lives in the state directory, not beside the installation. Next to
# main.py it landed inside the git checkout: committed or synced by accident,
# inheriting whatever that directory grants, and unwritable wherever the
# install is read-only. Same location as the alias store and file sessions, so
# there is one directory to lock down.
_log_directory = state_dir()
log_file_path = str(_log_directory / "mcp_errors.log")

logger.addHandler(console_handler)
try:
    _log_directory.mkdir(parents=True, exist_ok=True)
    restrict_to_owner(_log_directory)
    file_handler = _make_file_handler(log_file_path)
    logger.addHandler(file_handler)
except Exception as log_error:
    # stderr, never stdout: on the stdio transport stdout is the MCP protocol
    # channel and a stray line there corrupts the session. Type only -- the
    # message can name a path.
    print(
        f"WARNING: could not set up the log file: {type(log_error).__name__}",
        file=sys.stderr,
    )
    # Console-only logging; the console handler is already attached.
    log_event(logging.ERROR, "could not set up the log file handler", error=log_error)
