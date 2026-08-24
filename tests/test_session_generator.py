"""The 2FA retry, which one login path had and the other did not.

Reported from a real run: phone login, wrong password once, and the generator
answered "Failed to generate session string" and exited — after the SMS code had
already been spent, which is the part that costs a second round trip to Telegram.

The QR path had a retry loop; the phone path called `sign_in` once and let
`PasswordHashInvalidError` escape to the outer handler. Both now share one helper,
and these tests drive that helper directly rather than asserting the two branches
look similar, because looking similar is exactly what they did before.
"""

import base64
import importlib.util
import json
import os
import shutil
import subprocess
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

    def disconnect(self):
        self.disconnected = True


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


def test_the_password_prompt_gives_up_instead_of_asking_for_ever(generator, monkeypatch):
    """`while True` with no ceiling: a scripted or piped stdin that keeps
    answering the same wrong password never leaves the loop, and there is nobody
    at the terminal to press Ctrl+C."""
    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: "always-wrong")
    client = _Client(failures=10**6)

    with pytest.raises(SystemExit) as exit_info:
        generator._sign_in_with_password(client)

    assert exit_info.value.code != 0
    assert len(client.attempts) == generator.MAX_PASSWORD_ATTEMPTS


def test_an_empty_prompt_cannot_spin_for_ever_either(generator, monkeypatch):
    """A closed stdin returns "" without blocking, so the branch that does NOT
    reach Telegram is the one that can spin fastest."""
    monkeypatch.setattr(generator.getpass, "getpass", lambda _prompt: "")
    client = _Client(failures=0)

    with pytest.raises(SystemExit):
        generator._sign_in_with_password(client)

    assert client.attempts == [], "an empty password was sent to Telegram"


def test_the_attempt_ceiling_is_a_finite_positive_number(generator):
    ceiling = generator.MAX_PASSWORD_ATTEMPTS

    assert isinstance(ceiling, int)
    assert 1 <= ceiling <= 20


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


def test_writing_a_labelled_key_leaves_every_other_line_alone(generator, tmp_path):
    env = tmp_path / ".env"
    original = [
        "# a comment that must survive",
        "TELEGRAM_API_ID=12345",
        "",
        "TELEGRAM_SESSION_STRING=1AAAexisting",
        "UNRELATED_KEY=keep me",
    ]
    env.write_text("\n".join(original) + "\n", encoding="utf-8")

    backup = generator.write_env_value("TELEGRAM_SESSION_STRING_WORK", "1AAAnew", env)

    written = env.read_text(encoding="utf-8")
    for line in original:
        assert line in written, f"rewriting .env lost: {line!r}"
    assert "TELEGRAM_SESSION_STRING_WORK=1AAAnew" in written
    assert backup is not None and backup.exists(), "the file was rewritten with no backup"
    assert backup.read_text(encoding="utf-8") == "\n".join(original) + "\n"


def test_writing_an_existing_key_replaces_its_line_rather_than_appending(generator, tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_SESSION_STRING=1AAAold\nOTHER=1\n", encoding="utf-8")

    generator.write_env_value("TELEGRAM_SESSION_STRING", "1AAAnew", env)

    lines = [
        line
        for line in env.read_text(encoding="utf-8").splitlines()
        if line.startswith("TELEGRAM_SESSION_STRING=")
    ]
    assert lines == ["TELEGRAM_SESSION_STRING=1AAAnew"], f"duplicated instead of replaced: {lines}"


def test_a_file_without_a_trailing_newline_does_not_glue_two_keys_together(generator, tmp_path):
    """Appending to a file whose last line is unterminated used to join them."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=12345", encoding="utf-8")

    generator.write_env_value("TELEGRAM_SESSION_STRING_WORK", "1AAAnew", env)

    assert env.read_text(encoding="utf-8").splitlines() == [
        "TELEGRAM_API_ID=12345",
        "TELEGRAM_SESSION_STRING_WORK=1AAAnew",
    ]


def test_writing_into_a_missing_file_creates_it_and_reports_no_backup(generator, tmp_path):
    env = tmp_path / ".env"

    backup = generator.write_env_value("TELEGRAM_SESSION_STRING", "1AAAnew", env)

    assert backup is None, "a backup was claimed for a file that did not exist"
    assert env.read_text(encoding="utf-8") == "TELEGRAM_SESSION_STRING=1AAAnew\n"


def test_the_save_prompt_defaults_to_yes(generator):
    """Enter alone must save. Someone who completed a login wants the result kept."""
    source = (REPO / "session_string_generator.py").read_text(encoding="utf-8")
    assert "[Y/n]" in source, "the save prompt no longer advertises yes as the default"
    assert "(y/N)" not in source, "the save prompt still defaults to no"
    assert 'in {"", "y", "yes"}' in source, "an empty answer is not treated as yes"


def test_the_label_argument_skips_the_second_prompt(generator, monkeypatch):
    """The account manager already asked. Asking again is the duplicate that was reported."""
    monkeypatch.setattr(sys, "argv", ["session_string_generator.py", "--label", "kgb_verifier"])
    assert generator._parse_args().label == "kgb_verifier"
    monkeypatch.setattr(sys, "argv", ["session_string_generator.py"])
    assert generator._parse_args().label is None


def test_a_typed_label_becomes_a_key_dotenv_can_read(generator):
    """Found live: the generator wrote `TELEGRAM_SESSION_STRING_KGB VERIFIER=...`.

    python-dotenv drops a line whose key contains a space, warning only on stderr, so
    the account sat in .env looking correct and never loaded. The account manager
    already normalised; the generator's own path did not.
    """
    cases = {
        "KGB Verifier": "KGB_Verifier",
        "  Work  Account  ": "Work_Account",  # a run of spaces collapses to one _
        "my-second-phone": "my_second_phone",
        "Personal": "Personal",
        "_padded_": "padded",
    }
    for typed, expected in cases.items():
        got = generator.normalise_label(typed)
        assert got == expected, f"{typed!r} became {got!r}, expected {expected!r}"
        assert not any(c.isspace() for c in got), f"{typed!r} kept whitespace"


def test_writing_a_key_with_whitespace_is_refused(generator, tmp_path):
    """Defence in depth: such a key is silently unreadable, so a 'successful' write
    is worse than an error - it produces a setting that can never load."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="whitespace"):
        generator.write_env_value("TELEGRAM_SESSION_STRING_KGB VERIFIER", "1AAA", env)

    assert env.read_text(encoding="utf-8") == "TELEGRAM_API_ID=1\n", "the file was touched anyway"


def test_a_normalised_key_round_trips_through_dotenv(generator, tmp_path):
    """The end-to-end property that actually matters: write it, then read it back
    with the same parser the server uses."""
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")
    label = generator.normalise_label("KGB Verifier")

    generator.write_env_value(f"TELEGRAM_SESSION_STRING_{label.upper()}", "1AAAsession", env)

    parsed = dotenv_values(env)
    assert parsed.get("TELEGRAM_SESSION_STRING_KGB_VERIFIER") == "1AAAsession"


def test_the_generator_and_the_package_share_one_normaliser(generator):
    """Not "two functions that agree today": the same object.

    Three places decide what an account label is - this script, the account
    manager, and the client registry that reads the resulting env keys back. Two
    copies of a rule drift, and the drift shows up as an account that saves fine
    and never loads.
    """
    from telegram_mcp import aliases

    assert generator.normalise_label is aliases.normalise_account_label


# Every label a person is plausibly going to type, plus the ones with no safe
# mapping. Shared by the Python check and the PowerShell one so neither can drift
# by testing a friendlier set than the other.
LABEL_CASES = [
    "KGB Verifier",
    "  Work  Account  ",
    "my-second-phone",
    "Personal",
    "a  b",
    "_padded_",
    "WORK-DASH",
    "work_2",
    "---",
    "has.dot",
    "has/slash",
    "کانال",
    "has=equals",
    "has space and-hyphen",
]

# Input and output travel through the environment and stdout as ASCII JSON in
# input order: a typed label may be non-ASCII, and a console code page decoding
# it differently would fail this test for a reason that has nothing to do with
# the rule being checked.
_PROBE = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = Get-Content -LiteralPath $env:TGMCP_MANAGER -Raw
$body = [regex]::Match($source, '(?ms)^function ConvertTo-Label \\{.*?^\\}')
if (-not $body.Success) { throw 'ConvertTo-Label is gone from the account manager.' }
. ([ScriptBlock]::Create($body.Value))
$answers = foreach ($typed in ([Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($env:TGMCP_LABELS)) | ConvertFrom-Json)) {
    $got = ConvertTo-Label -Raw $typed
    if ($null -eq $got) { '' } else { $got }
}
ConvertTo-Json -Compress -InputObject @($answers)
"""


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_the_account_manager_normalises_labels_exactly_as_the_package_does(generator):
    """The third copy of the rule, and the one that cannot import the first two.

    PowerShell has no way to call `normalise_account_label`, so "shared" here can
    only mean "provably identical for the same input". Left unchecked, the menu
    and the generator drift, and the drift shows up as an account that saves
    without complaint and never loads.

    `ConvertTo-Label` lower-cases because the menu prints the runtime form; the
    package keeps the typed case because the env key upper-cases it anyway.
    """
    completed = subprocess.run(
        [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "TGMCP_MANAGER": str(REPO / "Manage-Accounts.ps1"),
            "TGMCP_LABELS": base64.b64encode(json.dumps(LABEL_CASES).encode("utf-8")).decode(
                "ascii"
            ),
        },
    )
    assert completed.returncode == 0, completed.stderr

    powershell = json.loads(completed.stdout)
    assert len(powershell) == len(LABEL_CASES)
    for typed, theirs in zip(LABEL_CASES, powershell):
        try:
            expected = generator.normalise_label(typed).lower()
        except ValueError:
            expected = ""  # ConvertTo-Label reports a refusal as the empty string
        assert (
            theirs == expected
        ), f"{typed!r}: the account manager says {theirs!r}, the package says {expected!r}"


def test_the_runtime_label_form_is_the_normalised_one_lowercased(generator):
    """`get_client` looks a label up lowercased, so what the generator writes has
    to survive that: normalise, upper-case into the env key, lower-case back."""
    from telegram_mcp import aliases

    for typed in ("KGB Verifier", "my-second-phone", "  Work  Account  "):
        label = generator.normalise_label(typed)
        env_suffix = f"TELEGRAM_SESSION_STRING_{label.upper()}"[len("TELEGRAM_SESSION_STRING_") :]

        assert aliases.account_key(env_suffix) == label.lower()


# --- F06: a label that cannot become an env key must be refused, not mangled ---


@pytest.mark.parametrize(
    "typed",
    [
        "---",  # collapses to nothing at all
        "   ",
        "work=other",  # an '=' splits the .env line in the wrong place
        "work account!",
        "café",  # non-ASCII is not a portable env-var name
    ],
)
def test_a_label_that_cannot_be_an_env_key_is_refused(generator, typed):
    """The docstring always promised "refusing loudly beats guessing".

    It did not: `---` became the empty label and `work=other` kept its `=`, so
    the generator wrote `TELEGRAM_SESSION_STRING_WORK=OTHER=1AAA...` and
    python-dotenv read the account back under a key nobody configured.
    """
    with pytest.raises(ValueError, match="label"):
        generator.normalise_label(typed)


def test_write_env_value_refuses_a_key_that_is_not_an_env_name(generator, tmp_path):
    """Defence in depth for the same class of key, whatever produced it."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        generator.write_env_value("TELEGRAM_SESSION_STRING_WORK=OTHER", "1AAA", env)

    assert env.read_text(encoding="utf-8") == "TELEGRAM_API_ID=1\n", "the file was touched anyway"
