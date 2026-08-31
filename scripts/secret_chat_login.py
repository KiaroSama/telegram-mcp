#!/usr/bin/env python3
"""Sign an account in to TDLib using the Telethon login it already has.

Secret chats do not run on Telethon -- it never implemented MTProto 2.0 -- so
they run on TDLib (see ``telegram_mcp/tdlib.py``). TDLib keeps its own
authorisation and cannot read a Telethon session or import one, which for a long
time read as "one more login code per account".

It is not, and that was the wrong conclusion. Telegram's own device-linking flow
lets a NEW client publish a login token and an ALREADY AUTHORISED client accept
it. Both halves are reachable from here: TDLib produces the token, and this
account's existing Telethon client accepts it. **Nothing is asked of you, and no
QR code is displayed or scanned** -- the protocol calls it QR login because that
is how the phone app surfaces it, but here the token never leaves the process.

    python scripts/secret_chat_login.py kgb_verifier

What this cannot remove is the second DEVICE: TDLib is a separate client, so the
account's session list gains an entry. That is the protocol, not a shortcut not
taken.

Two-step verification is the one case that still needs a person. Telegram asks
for the password even when a linked device presents a valid token, so this
prompts for it -- not echoed, and typed by the account's owner into their own
terminal rather than passed as an argument.

Run it again at any time: an account that is already signed in says so and exits
without touching anything.
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
    account_label,
    authorise_from_telethon,
    database_dir_for,
    tdjson_status,
)


async def _telethon_client(label: str):
    """The account's Telethon client, connected and confirmed authorised.

    Confirmed rather than assumed: an unauthorised client would fail at
    `acceptLoginToken` with an error about the token, which sends the reader
    looking at the wrong half of this.
    """
    from telegram_mcp.connection import clients

    client = clients[label]
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            f"Account {label!r} is not signed in to Telethon either, so there is no "
            "authorisation here to extend. Add it with Manage-Accounts.ps1 first."
        )
    return client


async def _run(account: str) -> int:
    status = tdjson_status()
    if not status["available"]:
        print(status["reason"])
        return 1
    print(f"TDLib {status['tdlib_version']}")

    try:
        label = account_label(account)
    except ValueError as exc:
        print(exc)
        return 1

    print(f"Account:  {label}")
    print(f"Database: {database_dir_for(label)}")

    client = TDLibClient(label)
    state = await client.start()

    if state == "authorizationStateReady":
        me = await client.request({"@type": "getMe"})
        name = " ".join(filter(None, (me.get("first_name"), me.get("last_name"))))
        print(f"\nAlready signed in as {name} (id {me.get('id')}). Nothing to do.")
        await client.close()
        return 0

    # A client can come up ALREADY past the token step: an earlier run's token
    # was accepted and Telegram is now asking for the two-step password. Trying
    # to authorise from there publishes a second token for nothing, which is
    # what the first live run did.
    if state == "authorizationStateWaitPhoneNumber":
        telethon_client = await _telethon_client(label)
        print()
        print("Authorising from this account's existing Telethon login...")
        state = await authorise_from_telethon(client, telethon_client)

    if state == "authorizationStateWaitPassword":
        # Telegram asks for this even when the token is valid. It is the one
        # thing here a person still has to supply.
        print("\nThis account has two-step verification.")
        password = getpass.getpass("Two-step verification password (not shown): ")
        if not password:
            print("Nothing entered; cancelled.")
            await client.close()
            return 1
        await client.request({"@type": "checkAuthenticationPassword", "password": password})
        state = await client._settle()

    if state != "authorizationStateReady":
        print(f"\nTDLib stopped at {state}, so secret chats are not available yet.")
        await client.close()
        return 1

    me = await client.request({"@type": "getMe"})
    name = " ".join(filter(None, (me.get("first_name"), me.get("last_name"))))
    print(f"\nSigned in as {name} (id {me.get('id')}) - no code needed.")
    print("Secret chats are now available for this account.")
    await client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "account",
        help="The account label, matching the one the server uses (see list_accounts).",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args.account))
    except TDLibUnavailable as exc:
        print(exc)
        return 1
    except TDLibError as exc:
        print(f"Telegram refused this: {exc}")
        return 1
    except (KeyError, RuntimeError) as exc:
        print(exc if str(exc) else f"Unknown account {args.account!r}.")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
