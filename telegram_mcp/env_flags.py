"""Reading a setting out of the environment, where several subsystems must agree.

Small on purpose. It exists because the proxy layer, the alias store, file-path
security and the event feed all read boolean switches from the environment, and if
each parsed its own, `TELEGRAM_EVENT_FEED=on` could mean one thing in one place and
another somewhere else. One parser is the point; the module is only the seam that
lets the four reach it without importing each other.
"""

from typing import Optional


def parse_bool_env(value: Optional[str], default: bool) -> bool:
    """A permissive truthy read: unset means `default`, and only the obvious words win.

    Anything unrecognised is False rather than an error, because a malformed switch
    must not stop the server from starting.
    """
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Historic private name: `runtime` exported it under the leading underscore and three
# subsystems still import it that way.
_parse_bool_env = parse_bool_env

__all__ = ["parse_bool_env", "_parse_bool_env"]
