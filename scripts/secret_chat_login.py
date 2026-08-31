#!/usr/bin/env python3
"""Log one account in to TDLib, once, so its secret chats work.

Secret chats do not run on Telethon -- Telethon never implemented MTProto 2.0
end-to-end encryption. They run on TDLib, Telegram's own client library (see
``telegram_mcp/tdlib.py``). TDLib cannot read a Telethon session file and offers
no way to import an existing authorisation, so an account that wants secret
chats has to sign in here as well. That is a one-time step per account, and it
adds one device to the account's session list.

This is a terminal script rather than an MCP tool on purpose. The login code and
a two-step password are typed by the account's owner into their own terminal;
neither is ever an argument to a tool, a value in a log, or anything an agent
sees. The password prompt is not echoed.

    python scripts/secret_chat_login.py kgb_verifier

Run it again at any time to check an account: an account that is already signed
in reports so and exits without asking for anything.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram_mcp.tdlib import (  # noqa: E402
    TDLibClient,
    TDLibError,
    TDLibUnavailable,
    database_dir_for,
    tdjson_status,
)


def _ask(prompt: str) -> str:
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        raise SystemExit(1)
    if not value:
        print("Nothing entered; cancelled.")
        raise SystemExit(1)
    return value


async def _login(account: str) -> int:
    client = TDLibClient(account)
    print(f"Account:  {account}")
    print(f"Database: {client.database_dir}")
    state = await client.start()

    # Each pass answers exactly one state, then re-reads: TDLib decides what
    # comes next, and the order is not fixed (an account with a two-step
    # password asks for it after the code, one without never does).
    while state != "authorizationStateReady":
        if state == "authorizationStateWaitPhoneNumber":
            phone = _ask("Phone number, with country code (e.g. +98...): ")
            await client.request(
                {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": phone,
                    "settings": {"@type": "phoneNumberAuthenticationSettings"},
                }
            )
        elif state == "authorizationStateWaitCode":
            code = _ask("Login code (Telegram sent it to your other devices): ")
            await client.request({"@type": "checkAuthenticationCode", "code": code})
        elif state == "authorizationStateWaitPassword":
            # getpass, so the two-step password is not echoed to the terminal
            # and cannot end up in a screen recording or a scrollback buffer.
            password = getpass.getpass("Two-step verification password (not shown): ")
            if not password:
                print("Nothing entered; cancelled.")
                return 1
            await client.request({"@type": "checkAuthenticationPassword", "password": password})
        elif state == "authorizationStateClosed":
            print("TDLib closed the session before login finished.")
            return 1
        else:
            print(
                f"This account needs a step this script does not handle: {state}.\n"
                "Finish it in an official Telegram client, then run this again."
            )
            return 1

        state = await client._settle()

    me = await client.request({"@type": "getMe"})
    name = " ".join(filter(None, (me.get("first_name"), me.get("last_name"))))
    print(f"\nSigned in as {name} (id {me.get('id')}).")
    print("Secret chats are now available for this account.")
    await client.close()
    return 0


async def _run(account: str) -> int:
    status = tdjson_status()
    if not status["available"]:
        print(status["reason"])
        print("\nThen run this script again.")
        return 1
    print(f"TDLib {status['tdlib_version']}")

    # An already-signed-in account must not be asked for a code it does not
    # need: a needless re-login would add a second device for no reason.
    probe = TDLibClient(account)
    state = await probe.start()
    if state == "authorizationStateReady":
        me = await probe.request({"@type": "getMe"})
        name = " ".join(filter(None, (me.get("first_name"), me.get("last_name"))))
        print(f"{account} is already signed in as {name} (id {me.get('id')}). Nothing to do.")
        await probe.close()
        return 0
    await probe.close()

    return await _login(account)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "account",
        help="The account label, matching the one the server uses (see list_accounts).",
    )
    args = parser.parse_args()

    print(f"TDLib database directory: {database_dir_for(args.account)}\n")
    try:
        return asyncio.run(_run(args.account))
    except TDLibUnavailable as exc:
        print(exc)
        return 1
    except TDLibError as exc:
        print(f"Telegram refused this: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
