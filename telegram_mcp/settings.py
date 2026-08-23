"""What the server was given: environment configuration, and the error for bad input.

Deliberately the bottom of the import graph. Everything above it - the client layer,
the alias store, file-path security, the tools - needs some of this, and none of it
needs them, so putting it anywhere higher creates a cycle.

`ValidationError` lives here because that is what it mostly reports: seven of its nine
raise sites are configuration the operator got wrong (a proxy type that does not exist,
a host without a port), where the right behaviour is to fail at startup rather than at
the first call. The two remaining sites use it for a caller-supplied ID, which is a
different kind of wrong wearing the same name - worth separating one day, but changing
it now would change what callers catch.
"""

import os
from typing import Optional

from dotenv import load_dotenv

# Loaded HERE, not by whoever imports this. These values are read at import time, and
# this module now sits at the bottom of the import graph - `main.py` reaches it through
# `file_roots` before `runtime` has run a line, so relying on `runtime` to have called
# `load_dotenv()` first meant the server could not start from a `.env` at all. It is
# idempotent, and `runtime` still calls it for its own remaining reads.
load_dotenv()


class ValidationError(Exception):
    """Custom exception for validation errors."""

    pass


def _require_credential(name: str) -> str:
    """Read a mandatory credential, or say which one is missing and where it comes from.

    Missing credentials are the most common way this server fails for a new operator,
    and the bare reads this replaced failed as `int(None)` - a TypeError naming no
    variable, no file and no next step. Same wording as
    `session_string_generator.py`, which already got this right: name the variable,
    name the file, name where the value comes from. It raises rather than prints
    because this is a library module, and on the stdio transport stdout is the MCP
    protocol channel.
    """
    value = os.getenv(name)
    if not value:
        raise ValidationError(
            f"{name} is not set. Put it in the .env file next to the server. "
            "Get both TELEGRAM_API_ID and TELEGRAM_API_HASH from "
            "https://my.telegram.org/apps."
        )
    return value


_RAW_TELEGRAM_API_ID = _require_credential("TELEGRAM_API_ID")
try:
    TELEGRAM_API_ID = int(_RAW_TELEGRAM_API_ID)
except ValueError:
    # Truncated: this is an operator-supplied string, and an error message is a
    # place things get logged.
    raise ValidationError(
        "TELEGRAM_API_ID must be a number, but .env has "
        f"{_RAW_TELEGRAM_API_ID[:40]!r}. Copy the numeric App api_id from "
        "https://my.telegram.org/apps."
    ) from None

TELEGRAM_API_HASH = _require_credential("TELEGRAM_API_HASH")


def parse_bool_env(value: Optional[str], default: bool) -> bool:
    """A permissive truthy read: unset means `default`, and only the obvious words win.

    Anything unrecognised is False rather than an error, because a malformed switch
    must not stop the server from starting. One parser, four subsystems - the proxy
    layer, the alias store, file-path security and the event feed - so that
    `TELEGRAM_EVENT_FEED=on` cannot mean different things in different places.
    """
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Historic private name: `runtime` exported it under the leading underscore and three
# subsystems still import it that way.
_parse_bool_env = parse_bool_env

__all__ = [
    "TELEGRAM_API_HASH",
    "TELEGRAM_API_ID",
    "ValidationError",
    "parse_bool_env",
    "_parse_bool_env",
]
