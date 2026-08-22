"""The 2FA retry, which one login path had and the other did not.

Reported from a real run: phone login, wrong password once, and the generator
answered "Failed to generate session string" and exited — after the SMS code had
already been spent, which is the part that costs a second round trip to Telegram.

The QR path had a retry loop; the phone path called `sign_in` once and let
`PasswordHashInvalidError` escape to the outer handler. Both now share one helper,
and these tests drive that helper directly rather than asserting the two branches
look similar, because looking similar is exactly what they did before.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from telethon import errors

REPO = Path(__file__).resolve().parents[1]


def _generator():
    """Load the generator as a module without running its main()."""
    spec = importlib.util.spec_from_file_location(
        "session_string_generator", REPO / "session_string_generator.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Client:
    """Rejects the first `failures` passwords, then accepts."""

    def __init__(self, failures):
        self.failures = failures
        self.attempts = []

    def sign_in(self, password=None):
        self.attempts.append(password)
        if len(self.attempts) <= self.failures:
            raise errors.PasswordHashInvalidError(request=None)
        return "signed-in"


@pytest.fixture
def generator():
    return _generator()


def test_a_wrong_password_is_asked_for_again_instead_of_ending_the_run(generator, monkeypatch):
    typed = iter(["wrong-one", "wrong-two", "right"])
    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: next(typed))
    client = _Client(failures=2)

    generator._sign_in_with_password(client)

    assert client.attempts == ["wrong-one", "wrong-two", "right"]


def test_a_correct_password_signs_in_once(generator, monkeypatch):
    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: "right")
    client = _Client(failures=0)

    generator._sign_in_with_password(client)

    assert client.attempts == ["right"], "a working password was asked for more than once"


def test_an_empty_password_is_not_sent_to_telegram(generator, monkeypatch):
    """Enter on an empty prompt is a slip, not an attempt worth spending."""
    typed = iter(["", "", "right"])
    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: next(typed))
    client = _Client(failures=0)

    generator._sign_in_with_password(client)

    assert client.attempts == ["right"]


def test_an_unrelated_error_is_not_swallowed_by_the_retry_loop(generator, monkeypatch):
    """Only a rejected password may loop. Anything else must surface."""

    class _Broken(_Client):
        def sign_in(self, password=None):
            self.attempts.append(password)
            raise errors.FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: "whatever")
    client = _Broken(failures=0)

    with pytest.raises(errors.FloodWaitError):
        generator._sign_in_with_password(client)
    assert len(client.attempts) == 1, "a flood wait was retried as if it were a bad password"


def test_both_login_paths_go_through_the_one_helper(generator):
    """The defect was two copies of the same idea, one of which was missing.

    Asserted on the source because the phone path cannot be driven without a real
    Telegram login: what matters is that neither branch calls `sign_in(password=)`
    on its own again.
    """
    source = (REPO / "session_string_generator.py").read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    helper_start = body.index("def _sign_in_with_password")
    helper_end = body.index("def _phone_login")
    outside = body[:helper_start] + body[helper_end:]

    assert "sign_in(password=" not in outside, (
        "a login path signs in with a password outside the shared helper, "
        "which is how the retry went missing from one of them"
    )
    assert (
        outside.count("_sign_in_with_password(client)") == 2
    ), "both the QR and the phone path must reach the shared helper"
