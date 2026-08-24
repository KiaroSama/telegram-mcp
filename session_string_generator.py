#!/usr/bin/env python3
"""
Telegram Session String Generator

This script generates a session string that can be used for Telegram authentication
with the Telegram MCP server. The session string allows for portable authentication
without storing session files.

Usage:
    python session_string_generator.py
    python session_string_generator.py --qr

Requirements:
    - telethon
    - python-dotenv

Note on ID Formats:
When using the MCP server, please be aware that all `chat_id` and `user_id`
parameters support integer IDs, string representations of IDs (e.g., "123456"),
and usernames (e.g., "@mychannel").
"""

import argparse
import asyncio
import getpass
import io
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import errors
from telethon.sessions import StringSession
from telethon.sync import TelegramClient
from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.console_theme import default_hint, failure, heading, hint, note
from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

# How many times the QR code is regenerated after expiry before giving up.
_QR_MAX_REFRESHES = 10

load_dotenv()


_ENV_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# A label is a SUFFIX of an env key, so it may start with a digit; it may not
# contain anything an env-var name cannot hold.
_LABEL_RE = re.compile(r"\A[A-Za-z0-9_]+\Z")


def normalise_label(raw: str) -> str:
    r"""Turn a typed label into one that can survive a round trip through `.env`.

    A label becomes part of an environment variable NAME, and python-dotenv refuses
    to parse a line whose key contains a space: it warns on stderr and DROPS the
    line. "KGB Verifier" was written literally once and produced
    `TELEGRAM_SESSION_STRING_KGB VERIFIER=...` - an account that sat in the file,
    looked correct, and could never load.

    Spaces and hyphens become underscores; anything the result still cannot be
    is refused, because there is no safe mapping to invent for it and refusing
    loudly beats guessing. That promise used to be documented and not kept:
    `---` collapsed to the empty label and `work=other` kept its `=`, which
    moves the split point of the `.env` line and files the account under a key
    nobody configured. The account manager applies the same rule before it ever
    gets here, so this is the guard for the standalone path.
    """
    label = re.sub(r"[\s\-]+", "_", raw.strip()).strip("_")
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"{raw!r} is not a usable account label: a label becomes part of an "
            "environment variable name, so it must be ASCII letters, digits and "
            "underscores, and cannot be empty."
        )
    return label


def write_env_value(key: str, value: str, env_path: Path = Path(".env")) -> Optional[Path]:
    r"""Set one key in `.env`, replacing its line or appending it, and back the file up.

    Returns the backup's path, or None when there was no file to back up.

    Extracted from main() so it can be exercised without a Telegram login. It sat
    inside the interactive flow, which meant the only way to test the file handling
    was to fake an entire sign-in - so it never was tested, and it rewrote the file
    holding every configured account with no way back.

    Every other line survives byte-for-byte: comments, ordering, and every key this
    knows nothing about, which is most of the file.
    """
    if not key or any(character.isspace() for character in key):
        # python-dotenv drops such a line on read, so the write would look like a
        # success and produce a setting that never loads.
        raise ValueError(f"an env key cannot contain whitespace: {key!r}")
    if not _ENV_KEY_RE.match(key):
        # An '=' or any other punctuation moves where the line splits, so the
        # value is read back under a different key than the one written.
        raise ValueError(f"not a usable env key: {key!r}")

    backup = None
    if env_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
        backup = env_path.with_name(f"{env_path.name}.backup-{stamp}")
        shutil.copyfile(env_path, backup)
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []

    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}" + "\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}" + "\n")

    env_path.write_text("".join(lines), encoding="utf-8")
    return backup


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Telegram session string for telegram-mcp."
    )
    login_group = parser.add_mutually_exclusive_group()
    login_group.add_argument(
        "--qr",
        action="store_true",
        help="Use Telegram QR login without prompting for a login method.",
    )
    login_group.add_argument(
        "--phone",
        action="store_true",
        help="Use phone number + verification code login without prompting for a login method.",
    )
    parser.add_argument(
        "--label",
        default=None,
        help=(
            "Account label to save under, skipping the prompt. Passed by the account "
            "manager, which has already asked for one."
        ),
    )
    return parser.parse_args()


def _check_installation() -> None:
    try:
        assert_safe_distribution()
    except UnsafeInstallationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def _render_qr(qr) -> None:
    import qrcode

    print()
    print(heading("QR code login"))
    print()

    qr_obj = qrcode.QRCode(border=1)
    qr_obj.add_data(qr.url)
    qr_obj.make(fit=True)
    f = io.StringIO()
    qr_obj.print_ascii(out=f, invert=True)
    print(f.getvalue())

    print(note("Scan the QR code above with your Telegram app:"))
    print(hint("  Open Telegram > Settings > Devices > Link Desktop Device"))
    print()
    print(f"Or open this link on a device where you're logged in:\n  {qr.url}\n")
    print(hint(f"Expires at: {qr.expires.strftime('%H:%M:%S')}"))
    print(hint("Waiting for you to scan..."))


def _seconds_until_expiry(qr) -> float:
    """Seconds left before this QR token expires, with a small safety margin."""
    expires = qr.expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    return max(1.0, remaining - 1.0)


def _qr_login(client: TelegramClient) -> None:
    qr = client.qr_login()
    _render_qr(qr)

    for _ in range(_QR_MAX_REFRESHES):
        try:
            client.loop.run_until_complete(qr.wait(timeout=_seconds_until_expiry(qr)))
            return
        except asyncio.TimeoutError:
            client.loop.run_until_complete(qr.recreate())
            print()
            print(note("That QR code expired. Here is a fresh one."))
            _render_qr(qr)
        except errors.SessionPasswordNeededError:
            _sign_in_with_password(client)
            return

    print()
    print(failure("The QR code expired too many times. Run the generator again."))
    client.disconnect()
    sys.exit(1)


def _sign_in_with_password(client: TelegramClient) -> None:
    """Ask for the 2FA password until it is accepted, or the user gives up.

    Shared by BOTH login paths on purpose. This loop used to exist only in the QR
    branch; the phone branch called sign_in once, so a single mistyped password
    raised PasswordHashInvalidError, escaped to the outer handler and killed the
    whole run with "Failed to generate session string" - after the code had
    already been used, which is the expensive part to redo.
    """
    while True:
        pw = getpass.getpass("\nTwo-factor authentication enabled. Please enter your password: ")
        if not pw:
            print(note("No password entered. Press Ctrl+C to give up, or try again."))
            continue
        try:
            client.sign_in(password=pw)
            return
        except errors.PasswordHashInvalidError:
            print(failure("That password was not accepted. Try again."))


def _phone_login(client: TelegramClient) -> None:
    phone = input("Please enter your phone (or bot token): ")

    try:
        client.send_code_request(phone)
    except errors.FloodWaitError as e:
        print()
        print(failure(f"Telegram asked for a wait of {e.seconds} seconds before trying again."))
        client.disconnect()
        sys.exit(1)
    except errors.PhoneNumberInvalidError:
        print()
        print(failure("That phone number is not valid."))
        client.disconnect()
        sys.exit(1)
    except Exception as e:
        print()
        print(failure(f"Telegram would not send the code: {e}"))
        client.disconnect()
        sys.exit(1)

    code = input("\nPlease enter the code you received: ")
    try:
        client.sign_in(phone, code)
    except errors.SessionPasswordNeededError:
        _sign_in_with_password(client)


def main() -> None:
    args = _parse_args()
    _check_installation()

    API_ID = os.getenv("TELEGRAM_API_ID")
    API_HASH = os.getenv("TELEGRAM_API_HASH")

    if not API_ID or not API_HASH:
        print(failure("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env."))
        print(hint("Get them from https://my.telegram.org/apps, then put them in .env."))
        sys.exit(1)

    try:
        API_ID = int(API_ID)
    except ValueError:
        print(failure("TELEGRAM_API_ID must be a number."))
        sys.exit(1)

    print()
    print(heading("Telegram session string generator"))
    print()
    if args.label is None:
        # Skipped when the account manager is the caller: it has just explained the
        # same thing, and saying it twice in two styles is what prompted this.
        print("This script will generate a session string for your Telegram account.")
        print("The generated session string can be added to your .env file.")
    print(
        "\nYour credentials will NOT be stored on any server and are only used for local authentication.\n"
    )

    if args.label is not None:
        # Supplied by the account manager, which asked for it already. Asking a
        # second time is how the same account ended up described twice.
        label = args.label.strip()
    else:
        try:
            label = (
                input(
                    "Account label (optional, e.g. 'work', 'personal'; leave empty for default): "
                )
                .strip()
                .lower()
            )
        except EOFError:
            # Non-interactive stdin (piped/scripted runs): fall back to the default label.
            label = ""

    # Before the login, not after it: a label that cannot become an env key used
    # to be discovered once the session already existed, and the run then either
    # wrote an unreadable line or threw the freshly minted session away.
    if label:
        try:
            safe_label = normalise_label(label)
        except ValueError as exc:
            print(failure(str(exc)))
            sys.exit(1)
        if safe_label != label.strip():
            print(hint(f"Saving under '{safe_label}' - a key cannot contain a space."))
        env_var = f"TELEGRAM_SESSION_STRING_{safe_label.upper()}"
    else:
        env_var = "TELEGRAM_SESSION_STRING"

    if args.qr:
        method = "1"
    elif args.phone:
        method = "2"
    else:
        print()
        print(heading("Choose login method:"))
        print("  1) QR code login (recommended -- scan from your Telegram app)")
        print("  2) Phone number + verification code")
        print()
        method = input(f'Selection {default_hint("[1]")}: ').strip() or "1"

    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH, **client_identity_kwargs())
        client.connect()

        if not client.is_user_authorized():
            if method == "1":
                _qr_login(client)
            else:
                _phone_login(client)

        session_string = StringSession.save(client.session)

        print()
        print(heading("Authentication successful."))
        print(heading("Your session string"))
        print(f"\n{session_string}\n")
        print("Add this to your .env file as:")
        print(f"{env_var}={session_string}")
        print()
        print(note("This string is a full login to that account. Never share it."))

        try:
            print()
            choice = input(f'Save it to .env as {env_var}? {default_hint("[Y/n]")}: ')
        except EOFError:
            # Nothing is reading the prompt, so nothing can confirm it either.
            choice = "n"
        if choice.strip().lower() in {"", "y", "yes"}:
            try:
                backup = write_env_value(env_var, session_string)
                print("")
                print(f".env updated: {env_var} is saved.")
                if backup:
                    print(hint(f"The previous file is kept as {backup.name}."))
            except Exception as e:
                print("")
                print(f"Error updating .env file: {e}")
                print(hint("Add the session string to .env by hand instead."))

        client.disconnect()

    except Exception as e:
        print()
        print(failure(f"{e}"))
        print(failure("No session string was generated."))
        sys.exit(1)


if __name__ == "__main__":
    main()
