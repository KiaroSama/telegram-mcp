"""A decoder handling hostile bytes does not hold the Telegram login.

`run_bounded` gives every helper a separate PID, a kill-on-close job object, and
byte and time ceilings — so a wedged or crashing decoder cannot take the server
with it. That containment stops at the process boundary and said nothing about
the process ENVIRONMENT, which was inherited whole.

What it inherited included the login. `TELEGRAM_API_HASH` and
`TELEGRAM_SESSION_STRING*` are loaded by `load_dotenv()` in `runtime.py` and stay
in `os.environ` for the life of the server, and `pillow_worker`, `lottie_worker`
and `capture_worker` read no environment variable at all — they take their whole
input as argv. So the inheritance bought nothing and cost the account: a
memory-safety failure in an image or animation decoder would land in a process
holding a full session string, readable with no further escalation.
"""

import sys

from telegram_mcp.visual.bounded_process import child_environment, run_bounded

CEILING = 64 * 1024

# Never a real value. The point is that a child cannot see it whatever it is.
PLANTED = "planted-not-a-real-session-string"

_REPORT = (
    "import os,sys;"
    "sys.stdout.write(','.join(sorted(k for k in os.environ if k.startswith('TELEGRAM_'))))"
)


def _run_reporter():
    completed = run_bounded(
        [sys.executable, "-c", _REPORT],
        label="environment reporter",
        timeout=60,
        max_output_bytes=CEILING,
        max_stderr_bytes=CEILING,
    )
    return (completed.stdout or b"").decode("utf-8", "replace").strip()


def test_a_decoder_cannot_see_the_session_string(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", PLANTED)
    monkeypatch.setenv("TELEGRAM_API_HASH", PLANTED)

    seen = _run_reporter()

    assert seen == "", f"the child could still see {seen}"


def test_the_allowlist_is_not_simply_empty():
    """Guard the guard: an environment scrubbed to nothing would pass the test
    above and break every helper that has to find ffmpeg."""
    passed = child_environment()

    assert "PATH" in passed
    if sys.platform == "win32":
        # A child started without SYSTEMROOT fails in ways that look like a
        # corrupted Python install rather than a missing variable.
        assert "SYSTEMROOT" in passed


def test_the_allowlist_drops_anything_telegram_shaped(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_SECOND", PLANTED)
    monkeypatch.setenv("MCP_TRUSTED_PROXY_AUTH", PLANTED)

    passed = child_environment()

    assert not [key for key in passed if key.startswith(("TELEGRAM_", "MCP_"))]
    assert PLANTED not in passed.values()
