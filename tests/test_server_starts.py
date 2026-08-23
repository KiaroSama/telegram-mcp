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


def _import_settings_with(tmp_path, dotenv):
    """Import `settings` in a clean interpreter whose only configuration is `dotenv`."""
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env["PYTHONPATH"] = str(REPO)

    return subprocess.run(
        [sys.executable, "-c", "import telegram_mcp.settings"],
        cwd=tmp_path,  # the .env is found relative to the working directory
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# The three ways credentials go wrong. These assert on stderr *content*, not just on
# the exit code: the old bare reads also exited non-zero, they just said `int()
# argument must be a string ... not 'NoneType'`, which names no variable to set and
# no file to set it in.
def test_a_missing_api_id_names_the_variable_and_the_file(tmp_path):
    result = _import_settings_with(tmp_path, "TELEGRAM_API_HASH=0123456789abcdef\n")

    assert result.returncode != 0, result.stdout
    assert "TELEGRAM_API_ID is not set" in result.stderr, result.stderr[-1500:]
    assert ".env" in result.stderr, result.stderr[-1500:]


def test_a_non_numeric_api_id_says_it_must_be_a_number(tmp_path):
    result = _import_settings_with(
        tmp_path,
        "TELEGRAM_API_ID=not-a-number\nTELEGRAM_API_HASH=0123456789abcdef\n",
    )

    assert result.returncode != 0, result.stdout
    assert "TELEGRAM_API_ID must be a number" in result.stderr, result.stderr[-1500:]


def test_a_missing_api_hash_names_the_variable(tmp_path):
    result = _import_settings_with(tmp_path, "TELEGRAM_API_ID=12345\n")

    assert result.returncode != 0, result.stdout
    assert "TELEGRAM_API_HASH is not set" in result.stderr, result.stderr[-1500:]
    assert ".env" in result.stderr, result.stderr[-1500:]
