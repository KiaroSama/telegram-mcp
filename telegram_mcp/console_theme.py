"""The terminal palette the launchers share, for the Python side.

`Manage-Accounts.ps1` carries the same codes for PowerShell. Two runtimes cannot
import one module, so the values live twice — and
`tests/test_console_theme.py` compares them, because a palette that drifts between
two halves of the same flow is worse than no palette at all.

Ported from FFmWiz (`ffmwiz/core/colors.py`, `ffmwiz/appio.py`). Only the tokens
these launchers actually use are here; the source palette has a hundred more and
carrying them across would be importing names to spend six.

256-colour SGR rather than the terminal's sixteen named colours, because the two
prompt keys have to be distinguishable at a glance: `back` is orange 166 and
`exit` is blue 32, and neither has a named equivalent.
"""

from __future__ import annotations

import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
WHITE = "\033[97m"
LIGHT_BLUE = "\033[38;5;117m"
NOTE_YELLOW = "\033[38;5;227m"
HINT_YELLOW = "\033[38;5;221m"
DIM = "\033[38;5;250m"
BACK_PROMPT = "\033[38;5;166m"
EXIT_PROMPT = "\033[38;5;32m"


def use_color() -> bool:
    """Whether to emit SGR at all.

    NO_COLOR is honoured the way FFmWiz honours it. A redirected stdout gets plain
    text too: escapes in a piped log are noise, and this program's output is
    routinely captured.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text: str, color: str) -> str:
    """`text` wrapped in `color`, or unchanged when colour is off."""
    if not use_color():
        return text
    return f"{color}{text}{RESET}"


def heading(text: str) -> str:
    """A section title, in the same blue the launcher menu uses."""
    return paint(text, LIGHT_BLUE)


def note(text: str) -> str:
    """Something the reader should notice but that is not a failure."""
    return paint(text, NOTE_YELLOW)


def failure(text: str) -> str:
    return paint(text, RED)


def hint(text: str) -> str:
    """Detail that helps but should not compete with the prompt."""
    return paint(text, DIM)


def back_text(text: str = "back=0, quit=exit") -> str:
    """`{back=0, quit=exit}` with each key coloured by what it means.

    Split on the comma and coloured by content rather than position, so a caller
    can pass only the half that applies without the colours moving.
    """
    parts = []
    for part in text.split(", "):
        lowered = part.lower()
        if "back" in lowered:
            parts.append(paint(part, BACK_PROMPT))
        elif "exit" in lowered:
            parts.append(paint(part, EXIT_PROMPT))
        else:
            parts.append(paint(part, WHITE))
    return "{" + ", ".join(parts) + "}"


def default_hint(value: str) -> str:
    """The bracketed default in a prompt, e.g. `[1]` or `[Y/n]`."""
    return paint(value, GREEN)
