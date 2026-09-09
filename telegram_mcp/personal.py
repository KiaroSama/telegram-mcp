"""Where an account's own data and one-off scripts belong.

Work done for a real account leaves things behind: a scan of somebody's channel,
the ids their packs use, a half-finished job's progress file, a script written
for one task. Those are not project files. They are one person's, and this
server talks to several accounts at once.

So they go in `.personal/<telegram user id>/`, which is git-ignored, and the
path comes from here rather than from whoever is writing at the time.

**Keyed by the NUMERIC id, never by the account label.** A label is a name in
`.env`: it can be renamed, removed and added back, or reused for a different
account entirely. The numeric id is Telegram's and does not move. Key the folder
by the label and an account that comes back as `refx_2` stops finding the work
done when it was called `refx`; key it by the id and the label can change every
week without anything noticing.

This is deliberately NOT `.ignoreme`, which the project's rules make read-only:
nothing may modify, copy or transmit what is in there. This directory is written
to constantly, so it is its own thing with its own guarantee - ignored, local,
and per person.
"""

import weakref
from pathlib import Path

DIRECTORY_NAME = ".personal"

# `get_me()` costs a round trip, and a live client's id cannot change, so it is
# asked once per CLIENT.
#
# Keyed by the client object, never by the account label. A label-keyed cache
# hands the first account's directory to the second one that borrows the name,
# which is the exact leak this module exists to prevent - and it does it
# silently. Weak, so a disconnected client does not pin its entry.
_ids: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

_NOTE = """This directory holds data and scripts belonging to ONE Telegram account,
named by that account's numeric user id.

It is git-ignored. Nothing in here is part of the project, and nothing in here
should be copied into it: it is one person's material, kept apart from every
other account this server can reach.

The id is numeric on purpose. Account LABELS live in .env and can be renamed,
removed, or reused; the id cannot, so work done under an old label is still
found under a new one.

Created by telegram_mcp/personal.py - see docs/personal-data.md.
"""


def project_root() -> Path:
    """The checkout this module lives in."""
    return Path(__file__).resolve().parent.parent


def personal_root() -> Path:
    """The parent of every account's directory. Not created until one is needed."""
    return project_root() / DIRECTORY_NAME


async def account_id(client) -> int:
    """The numeric Telegram id behind a connected client."""
    me = await client.get_me()
    return int(me.id)


async def personal_dir(client, create: bool = True) -> Path:
    """`.personal/<numeric id>/` for this client, created on first use.

    Takes the client and nothing else. There is deliberately no `label`
    argument: a caller cannot pass the wrong one, and the account label never
    reaches the path or the cache.
    """
    numeric = _ids.get(client)
    if numeric is None:
        numeric = await account_id(client)
        _ids[client] = numeric

    directory = personal_root() / str(numeric)
    if create and not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        # Written once, so a folder found later on disk explains itself without
        # this file having to be read.
        (personal_root() / "README.md").write_text(_NOTE, encoding="utf-8")
    return directory


def forget_cached_ids() -> None:
    """Drop the label->id cache. For tests, and for a re-login under a new account."""
    _ids.clear()


__all__ = [
    "DIRECTORY_NAME",
    "account_id",
    "forget_cached_ids",
    "personal_dir",
    "personal_root",
    "project_root",
]
