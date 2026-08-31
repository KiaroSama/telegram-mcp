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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import errors
from telethon.sessions import StringSession
from telethon.sync import TelegramClient
from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.aliases import normalise_account_label, restrict_to_owner
from telegram_mcp.console_theme import default_hint, failure, heading, hint, note
from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

# How many times the QR code is regenerated after expiry before giving up.
_QR_MAX_REFRESHES = 10

load_dotenv()


_ENV_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# One normaliser, not a second copy of the rule: the account manager applies the
# same one before it ever gets here, and the client registry reads back the env
# keys this produces. See telegram_mcp.aliases.normalise_account_label.
normalise_label = normalise_account_label


# How many times the 2FA password is asked for before the run gives up. `while
# True` had no ceiling at all, and "the user gives up" is not a condition that
# exists when stdin is a script.
MAX_PASSWORD_ATTEMPTS = 5


ENV_BACKUP_RETENTION = 5
_MAX_BACKUP_COLLISIONS = 100


def _write_owner_only(path: Path, text: str) -> None:
    """Create ``path`` owner-only from the first byte, refusing to clobber.

    The 0600 in the open is the POSIX half and is what makes the file private
    before a single byte is in it. `restrict_to_owner` is the Windows half:
    there the mode argument is ignored and the file inherits the directory's
    ACL, so a backup of every configured login lands readable by every account
    on the machine.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    restrict_to_owner(path)


def _backup_env(env_path: Path) -> Path:
    """Copy `.env` aside owner-only, under a name nothing else has taken."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
    text = env_path.read_text(encoding="utf-8")
    for attempt in range(_MAX_BACKUP_COLLISIONS):
        suffix = "" if attempt == 0 else f"-{attempt}"
        backup = env_path.with_name(f"{env_path.name}.backup-{stamp}{suffix}")
        try:
            _write_owner_only(backup, text)
            return backup
        except FileExistsError:
            continue
    raise OSError(f"could not find a free backup name for {env_path} within one second")


def _prune_env_backups(env_path: Path) -> None:
    """Keep the newest ``ENV_BACKUP_RETENTION`` backups and delete the rest.

    Each one holds a complete login to every account configured at the time, so
    an unbounded pile of them turns one readable directory into a leak of every
    session ever generated on the machine.
    """
    backups = sorted(env_path.parent.glob(f"{env_path.name}.backup-*"))
    for stale in backups[: max(0, len(backups) - ENV_BACKUP_RETENTION)]:
        try:
            stale.unlink()
        except OSError:
            pass


def write_env_value(key: str, value: str, env_path: Path = Path(".env")) -> Optional[Path]:
    r"""Set one key in `.env`, replacing its line or appending it, and back the file up.

    Returns the backup's path, or None when there was no file to back up.

    Extracted from main() so it can be exercised without a Telegram login. It sat
    inside the interactive flow, which meant the only way to test the file handling
    was to fake an entire sign-in - so it never was tested, and it rewrote the file
    holding every configured account with no way back.

    Every other line survives byte-for-byte: comments, ordering, and every key this
    knows nothing about, which is most of the file.

    The file and its backups are session strings -- full logins -- so both are
    created 0600 rather than inheriting the umask's 0644, the replacement is
    atomic so a crash cannot leave a half-written `.env`, and old backups are
    pruned rather than kept for ever.
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
        backup = _backup_env(env_path)
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

    # mkstemp makes a 0600 file with an unpredictable name in the same
    # directory, so the rename is atomic and no reader ever sees a partial file.
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent), prefix=env_path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())  # the rename must not outrun the bytes
        # Before the rename: an ACL travels with the file, so `.env` is never
        # briefly readable under its real name.
        restrict_to_owner(tmp)
        os.replace(tmp, env_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    _prune_env_backups(env_path)
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
    parser.add_argument(
        "--no-echo",
        action="store_true",
        help=(
            "Save the session to .env without printing it. A terminal is "
            "scrollback, often a screen share, and sometimes a shell log."
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
    print(hint(f"Expires at: {_expiry_clock(qr)}"))
    print(hint("Waiting for you to scan..."))


def _aware_expiry(qr):
    """`qr.expires` as an aware datetime.

    Telethon returns it in UTC. One place, because the countdown normalised it
    and the printed clock did not, and the two disagreeing by the local offset
    is exactly the bug below.
    """
    expires = qr.expires
    return expires if expires.tzinfo is not None else expires.replace(tzinfo=timezone.utc)


def _expiry_clock(qr) -> str:
    """The expiry on the clock the person is actually looking at.

    Printed straight, `qr.expires` shows UTC with nothing saying so: someone in
    UTC+3:30 reads "Expires at: 14:32:47" while their own clock says 18:02, and
    a perfectly good sixty-second countdown looks broken. The seconds were never
    wrong - only this line was.
    """
    return _aware_expiry(qr).astimezone().strftime("%H:%M:%S")


def _seconds_until_expiry(qr) -> float:
    """Seconds left before this QR token expires, with a small safety margin."""
    remaining = (_aware_expiry(qr) - datetime.now(timezone.utc)).total_seconds()
    return max(1.0, remaining - 1.0)


def _qr_login(client: TelegramClient) -> Optional[str]:
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
            return _sign_in_with_password(client)
            return

    print()
    print(failure("The QR code expired too many times. Run the generator again."))
    client.disconnect()
    sys.exit(1)


def _sign_in_with_password(client: TelegramClient) -> Optional[str]:
    """Ask for the 2FA password until it is accepted or the attempts run out.

    Shared by BOTH login paths on purpose. This loop used to exist only in the QR
    branch; the phone branch called sign_in once, so a single mistyped password
    raised PasswordHashInvalidError, escaped to the outer handler and killed the
    whole run with "Failed to generate session string" - after the code had
    already been used, which is the expensive part to redo.

    Bounded, because it used to be `while True` and "the user gives up" is not a
    condition that exists in automation: a scripted or piped stdin answering the
    same wrong password - or answering nothing, which does not even reach
    Telegram - never leaves the loop, and there is no one at the terminal to
    interrupt it. Every remaining attempt is counted out loud so a person can see
    the run ending before it does.
    """
    for remaining in range(MAX_PASSWORD_ATTEMPTS, 0, -1):
        pw = getpass.getpass("\nTwo-factor authentication enabled. Please enter your password: ")
        if not pw:
            print(note(f"No password entered. {remaining - 1} attempt(s) left."))
            continue
        try:
            client.sign_in(password=pw)
            # Returned, not discarded: the TDLib half needs this same password
            # seconds from now, and asking again for one Telegram just accepted
            # spends another attempt against the account's own limits.
            return pw
        except errors.PasswordHashInvalidError:
            print(failure(f"That password was not accepted. {remaining - 1} attempt(s) left."))

    print()
    print(
        failure(
            f"The password was not accepted in {MAX_PASSWORD_ATTEMPTS} attempts, so nothing "
            "was saved. Run the generator again when you have it to hand - the cost is one "
            "more QR scan or login code."
        )
    )
    client.disconnect()
    sys.exit(1)


def _phone_login(client: TelegramClient) -> Optional[str]:
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
        return _sign_in_with_password(client)
    return None


def _finish_secret_chats(client: TelegramClient, label: str, password: Optional[str]) -> None:
    """Sign this same account in to TDLib, without asking for anything again.

    The account now has a Telethon login, and TDLib keeps a separate one that
    secret chats and the newer admin rights run on. Telegram's device-linking
    flow lets this fresh authorisation authorise that one, so no code is needed
    -- and if two-step verification is on, the password the owner typed moments
    ago is reused rather than demanded a second time.

    That reuse is the point. Asking again for a password Telegram had just
    accepted was the behaviour this replaced: it read as the tool not having
    been paying attention, and every extra attempt counts against the account's
    own limits.

    Never fatal. The session string is already saved by this point, so a failure
    here costs the secret-chat half and nothing else, and says how to finish it.
    """
    try:
        from telegram_mcp.tdlib import complete_login, tdjson_status
    except Exception:
        return

    if not tdjson_status()["available"]:
        return

    print()
    print("Finishing the second half (secret chats) - no code, nothing to scan...")
    try:
        state = client.loop.run_until_complete(complete_login(label, client, password=password))
    except Exception as exc:
        print(failure(f"The secret-chat half did not finish: {exc}"))
        print(hint("Everything else is saved; this account works for every other tool."))
        return

    if state == "authorizationStateReady":
        print(f"Done - secret chats are ready for '{label}' too.")
    else:
        print(failure(f"The secret-chat half stopped at {state}."))
        print(hint("Everything else is saved; this account works for every other tool."))


def _report_session(env_var: str, session_string: str, *, echo: bool) -> None:
    """Say the login succeeded, and show the session only if asked to.

    Printing a StringSession puts a full account login into scrollback, into a
    screen share, and into whatever records the terminal. `--no-echo` names the
    key it will be saved under and nothing else.
    """
    print()
    print(heading("Authentication successful."))
    if echo:
        print(heading("Your session string"))
        print(f"\n{session_string}\n")
        print("Add this to your .env file as:")
        print(f"{env_var}={session_string}")
        print()
        print(note("This string is a full login to that account. Never share it."))
    else:
        print(note(f"The session will be saved to .env as {env_var} and not printed."))


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
        safe_label = "default"
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

        password = None
        if not client.is_user_authorized():
            if method == "1":
                password = _qr_login(client)
            else:
                password = _phone_login(client)

        session_string = StringSession.save(client.session)

        _report_session(env_var, session_string, echo=not args.no_echo)

        if args.no_echo:
            # --no-echo means "save it, do not show it": there is nothing on
            # screen to copy, so asking whether to save would only be a way to
            # lose the session.
            save = True
        else:
            try:
                print()
                choice = input(f'Save it to .env as {env_var}? {default_hint("[Y/n]")}: ')
            except EOFError:
                # Nothing is reading the prompt, so nothing can confirm it either.
                choice = "n"
            save = choice.strip().lower() in {"", "y", "yes"}
        if save:
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
            else:
                _finish_secret_chats(client, safe_label, password)

        client.disconnect()

    except Exception as e:
        print()
        print(failure(f"{e}"))
        print(failure("No session string was generated."))
        sys.exit(1)


if __name__ == "__main__":
    main()
