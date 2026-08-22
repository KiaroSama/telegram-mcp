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


class ValidationError(Exception):
    """Custom exception for validation errors."""

    pass


TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")


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
