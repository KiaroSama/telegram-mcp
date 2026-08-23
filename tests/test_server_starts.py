"""The server must start from a `.env` alone, in a process with no TELEGRAM_* set.

This is the one thing the rest of the suite structurally cannot check. The root
`conftest.py` neutralises the environment *and* `load_dotenv` before anything imports
the package, which is right for every other test and is exactly why a real startup
regression is invisible here: when `TELEGRAM_API_ID` moved into `settings.py` and that
module ended up imported before `runtime` ran `load_dotenv()`, every test still passed
and the server could not start at all.

So this test does not import the package. It launches a clean interpreter with the
TELEGRAM_* variables stripped, writes a throwaway `.env` beside it, and asks whether
`main` imports.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_package_imports_from_a_dotenv_in_a_bare_environment(tmp_path):
    (tmp_path / ".env").write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n"
        "TELEGRAM_SESSION_NAME=probe\n",
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env["PYTHONPATH"] = str(REPO)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import telegram_mcp.settings as s
                assert s.TELEGRAM_API_ID == 12345, s.TELEGRAM_API_ID
                import telegram_mcp.runtime  # noqa: F401
                print("started")
                """),
        ],
        cwd=tmp_path,  # the .env is found relative to the working directory
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "the package could not be imported from a .env alone:\n" f"{result.stderr[-1500:]}"
    )
    assert "started" in result.stdout
