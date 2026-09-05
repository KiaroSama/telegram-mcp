"""The account manager's source, as one text.

`Manage-Accounts.ps1` was one 1152-line file until it was split; the entry point
now dot-sources `account-manager/FileSafety.ps1`, `account-manager/EnvFile.ps1` and `account-manager/Console.ps1`.

Every test that asserts something about the launcher's SOURCE goes through here.
Reading only the entry point was correct while it was one file, and after the
split it silently stops covering whatever moved — a source regex that matches
nothing does not fail, it just stops testing. Two Python tests and both
PowerShell suites were caught by exactly that when the split landed.

Discovery, not a hardcoded list: a piece added to `account-manager/` is picked up, and one
removed cannot leave a stale path behind.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY_POINT = REPO / "Manage-Accounts.ps1"
LIB = REPO / "account-manager"


def launcher_files() -> list:
    """The entry point first, then each dot-sourced piece in a stable order."""
    return [ENTRY_POINT] + sorted(LIB.glob("*.ps1"))


def launcher_source() -> str:
    """Every file the account manager is made of, concatenated."""
    return "\n".join(path.read_text(encoding="utf-8") for path in launcher_files())
