"""One palette, carried in two languages, kept identical by this file.

The account manager is PowerShell and the session generator is Python, and they run
back to back in the same flow. They cannot import one module, so the colour codes
exist twice — which is fine only while something compares them. A launcher whose
two halves are differently coloured looks broken in a way no runtime error reports.
"""

import re
from pathlib import Path

import pytest

from telegram_mcp import console_theme

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "Manage-Accounts.ps1"

# Python name -> the key used in the PowerShell $script:Color table.
SHARED = {
    "RESET": "Reset",
    "BOLD": "Bold",
    "RED": "Red",
    "GREEN": "Green",
    "WHITE": "White",
    "LIGHT_BLUE": "LightBlue",
    "NOTE_YELLOW": "NoteYellow",
    "HINT_YELLOW": "HintYellow",
    "DIM": "Dim",
    "BACK_PROMPT": "BackPrompt",
    "EXIT_PROMPT": "ExitPrompt",
}


@pytest.fixture
def colour_on(monkeypatch):
    """pytest captures stdout, so isatty() is False and colour is correctly off.

    A test about what the colours ARE has to turn them on first; without this the
    assertions passed on a function that emitted nothing.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(console_theme.sys.stdout, "isatty", lambda: True, raising=False)


def _powershell_palette():
    source = MANAGER.read_text(encoding="utf-8")
    start = source.index("$script:Color = @{")
    end = source.index("function Test-ColorSupport")
    return dict(re.findall(r'(\w+)\s*=\s*"\$script:Esc(\[[0-9;]+m)"', source[start:end]))


def test_every_shared_colour_is_the_same_in_both_languages():
    powershell = _powershell_palette()
    assert powershell, "the PowerShell colour table could not be read"

    drifted = []
    for python_name, powershell_name in SHARED.items():
        expected = getattr(console_theme, python_name).replace("\033", "")
        actual = powershell.get(powershell_name)
        if actual != expected:
            drifted.append(f"{python_name}: python={expected!r} powershell={actual!r}")

    assert not drifted, "the two halves of one launcher have different colours:\n" + "\n".join(
        drifted
    )


def test_back_text_marks_the_two_keys_differently(colour_on):
    """`back` and `exit` must not share a colour - telling them apart is the point."""
    rendered = console_theme.back_text()

    assert rendered.startswith("{") and rendered.endswith("}")
    assert console_theme.BACK_PROMPT in rendered
    assert console_theme.EXIT_PROMPT in rendered
    assert console_theme.BACK_PROMPT != console_theme.EXIT_PROMPT


def test_a_hint_with_only_one_key_does_not_invent_the_other(colour_on):
    rendered = console_theme.back_text("quit=exit")

    assert "back" not in rendered
    assert console_theme.EXIT_PROMPT in rendered


def test_colour_is_dropped_when_the_output_is_not_a_terminal(monkeypatch):
    """These programs' output is routinely piped into a log; escapes there are noise."""
    monkeypatch.setattr(console_theme.sys.stdout, "isatty", lambda: False, raising=False)

    assert console_theme.paint("plain", console_theme.RED) == "plain"
    assert console_theme.back_text() == "{back=0, quit=exit}"


def test_no_color_is_honoured_even_on_a_terminal(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(console_theme.sys.stdout, "isatty", lambda: True, raising=False)

    assert console_theme.heading("plain") == "plain"


def test_colour_is_emitted_on_a_terminal_without_no_color(monkeypatch):
    """The negative cases above would all pass on a function that never paints."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(console_theme.sys.stdout, "isatty", lambda: True, raising=False)

    assert console_theme.heading("x") == f"{console_theme.LIGHT_BLUE}x{console_theme.RESET}"
