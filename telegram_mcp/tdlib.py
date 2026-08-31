"""Secret chats, which Telethon cannot do at all.

Telethon never implemented MTProto 2.0 end-to-end encryption. The raw TL
requests are in the schema -- ``messages.requestEncryption``,
``messages.sendEncrypted`` and the rest -- but nothing drives them: there is no
Diffie-Hellman exchange, no key store, no secret-chat layer negotiation, and no
``create_secret_chat`` on the client. The project was archived in February 2026
without one, so this is not a gap that will close. Writing the exchange and the
message encryption by hand is not something a tool server should own.

So secret chats come from Telegram's own code instead: TDLib, the library the
official clients are built on, driven through its JSON interface. It performs
the cryptography itself, and it is current -- 1.8.67 speaks layer 229, where the
installed Telethon speaks 227.

Two costs, stated here because neither can be hidden from whoever runs this:

* TDLib cannot read a Telethon session file. There is no import path for an
  existing authorisation. An account used for secret chats logs in once more
  through ``scripts/secret_chat_login.py``, and that login is a new device in
  the account's session list.
* The binary is an optional dependency. Without it every other tool in this
  server keeps working and only the secret-chat tools report why they cannot.

Everything else in the server stays on Telethon. This module exists to run
secret chats and nothing more.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import TELEGRAM_API_HASH, TELEGRAM_API_ID, state_dir

__all__ = [
    "NotSignedIn",
    "account_label",
    "TDLibClient",
    "TDLibError",
    "TDLibUnavailable",
    "close_all",
    "database_dir_for",
    "secret_client",
    "tdjson_status",
]


class TDLibUnavailable(RuntimeError):
    """The `tdjson` binary is not installed.

    Raised instead of `ImportError` so a tool can answer with the install
    command rather than a traceback about a module nobody mentioned.
    """


class NotSignedIn(RuntimeError):
    """The account has a Telethon session but no TDLib login.

    Its own type because the fix is specific and nothing else in the server
    needs it: this is not a permission problem, a network problem, or a wrong
    account name, and answering with any of those would send the caller looking
    in the wrong place.
    """

    def __init__(self, account: str, state: Optional[str]):
        super().__init__(
            f"Account {account!r} is signed in to Telethon but not to Telegram's "
            f"secret-chat library (state: {state}). The two cannot share a login: "
            f"TDLib has no way to import a Telethon session, so this account needs "
            f"one extra sign-in, once. Run:"
            + chr(10)
            + f"    python scripts/secret_chat_login.py {account}"
        )
        self.account = account
        self.state = state


class TDLibError(RuntimeError):
    """An `error` object from TDLib, carrying Telegram's own code and text."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{message} (TDLib code {code})")
        self.code = code
        self.message = message


def _tdjson():
    """The binary, imported on first use.

    Deferred so that importing this module -- which `tools/secret_chats.py` does
    unconditionally -- cannot break a server whose operator never wanted secret
    chats and never installed the dependency.
    """
    try:
        import tdjson
    except ImportError as exc:  # pragma: no cover - exercised by tdjson_status
        raise TDLibUnavailable(
            "Secret chats need Telegram's own library, which is not installed. "
            "Install it with: pip install tdjson"
        ) from exc
    return tdjson


def tdjson_status() -> dict:
    """Whether secret chats are available, and what to do if not.

    A tool that simply failed would leave the caller unable to tell an absent
    dependency from a broken login, which are fixed in completely different
    places.
    """
    try:
        td = _tdjson()
    except TDLibUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    try:
        version = json.loads(
            td.td_execute(json.dumps({"@type": "getOption", "name": "version"}).encode()).decode()
        ).get("value")
    except Exception as exc:  # pragma: no cover - a broken build, not a missing one
        return {"available": False, "reason": f"tdjson is installed but unusable: {exc}"}
    return {"available": True, "tdlib_version": version}


def account_label(account: Optional[str]) -> str:
    """The account label a TDLib database is stored under.

    `get_client` resolves `None` to the sole client but hands back the client,
    not its name, and TDLib is addressed by name. Lives here rather than in a
    tool module because this is the module that turns a label into a database
    directory, and two copies of the rule would be two chances to disagree.

    The import is deferred: `connection` builds real clients at import time, and
    this module must stay importable without them.
    """
    from telegram_mcp.connection import clients

    if account is None:
        if len(clients) == 1:
            return next(iter(clients))
        raise ValueError(f"Account is required. Available accounts: {', '.join(clients)}")
    label = account.lower()
    if label not in clients:
        raise ValueError(f"Unknown account '{account}'. Available accounts: {', '.join(clients)}")
    return label


def database_dir_for(account: str) -> Path:
    """Where one account's TDLib database lives.

    Under `state_dir()` with the Telethon sessions rather than beside the code:
    the install directory may be read-only and is often a git checkout, and one
    place to lock down beats two.
    """
    return state_dir() / "tdlib" / account


# --------------------------------------------------------------------------
# The receive loop.
#
# `td_receive` is process-global, not per-client: one call returns the next
# event for ANY client, tagged with its `@client_id`. So there is exactly one
# reader thread for the process, and it routes. A thread rather than a task
# because the call blocks.
# --------------------------------------------------------------------------

_clients: dict[int, "TDLibClient"] = {}
_clients_lock = threading.Lock()
_reader: Optional[threading.Thread] = None
_extra_ids = itertools.count(1)


def _quieten(td) -> None:
    """TDLib logs every request and response at its default verbosity.

    Left alone it writes the full text of each message to stderr, which for a
    secret chat means printing the plaintext this module exists to protect.
    """
    td.td_execute(json.dumps({"@type": "setLogVerbosityLevel", "new_verbosity_level": 1}).encode())


def _dispatch(event: dict) -> None:
    client_id = event.get("@client_id")
    with _clients_lock:
        client = _clients.get(client_id)
    if client is None:
        return
    client._handle(event)


def _reader_loop() -> None:  # pragma: no cover - a thread, driven by live TDLib
    td = _tdjson()
    while True:
        try:
            raw = td.td_receive(1.0)
        except Exception as exc:
            # Through log_event, not the logger: a raw handler would be free to
            # format the event that failed, and a secret chat's event carries
            # the plaintext this module exists to keep out of logs.
            log_event(logging.ERROR, "tdlib_receive_failed", error=exc)
            return
        if not raw:
            continue
        try:
            _dispatch(json.loads(raw.decode()))
        except Exception as exc:
            log_event(logging.ERROR, "tdlib_dispatch_failed", error=exc)


def _ensure_reader() -> None:
    global _reader
    with _clients_lock:
        if _reader is not None and _reader.is_alive():
            return
        _reader = threading.Thread(target=_reader_loop, name="tdlib-receive", daemon=True)
        _reader.start()


class TDLibClient:
    """One account's TDLib client.

    Requests are correlated by the `@extra` field TDLib echoes back, so several
    can be in flight at once. Updates -- which arrive unsolicited and carry no
    `@extra` -- go to a queue instead of a future.
    """

    def __init__(self, account: str, database_dir: Optional[Path] = None):
        self.account = account
        self.database_dir = Path(database_dir) if database_dir else database_dir_for(account)
        self.authorization_state: Optional[str] = None
        self.updates: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._td = _tdjson()
        self._client_id: Optional[int] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._state_changed: Optional[asyncio.Event] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> str:
        """Bring the client up and return the authorisation state it settles in.

        `authorizationStateReady` means a previous login is still good.
        Anything else is a step `scripts/secret_chat_login.py` has to complete;
        this returns it rather than prompting, because a tool server has nowhere
        to prompt.
        """
        _quieten(self._td)
        self._loop = asyncio.get_running_loop()
        self._state_changed = asyncio.Event()
        self._client_id = self._td.td_create_client_id()
        with _clients_lock:
            _clients[self._client_id] = self
        _ensure_reader()

        # Nothing happens until the client is poked; TDLib answers the first
        # request with its authorisation state.
        self._send({"@type": "getOption", "name": "version"})
        return await self._settle()

    async def _settle(self, timeout: float = 30.0) -> str:
        """Wait for an authorisation state that needs someone else to act.

        The intermediate states pass by on their own -- `WaitTdlibParameters` is
        answered here, `Ready` is the end -- so waiting for "not changing any
        more" would either hang or return a state that is about to be replaced.
        """
        settled = {
            "authorizationStateReady",
            "authorizationStateWaitPhoneNumber",
            "authorizationStateWaitCode",
            "authorizationStateWaitPassword",
            "authorizationStateWaitEmailAddress",
            "authorizationStateWaitEmailCode",
            "authorizationStateWaitRegistration",
            "authorizationStateClosed",
        }
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.authorization_state in settled:
                return self.authorization_state
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"TDLib did not reach a usable authorisation state within {timeout:.0f}s "
                    f"(last state: {self.authorization_state})"
                )
            self._state_changed.clear()
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        """Shut the client down so TDLib flushes its database.

        Skipping this risks losing the secret-chat keys written since the last
        flush, which cannot be re-derived -- the messages they decrypt are gone
        with them.
        """
        if self._client_id is None:
            return
        try:
            await self.request({"@type": "close"}, timeout=10)
        except (TDLibError, TimeoutError):
            pass
        with _clients_lock:
            _clients.pop(self._client_id, None)
        self._client_id = None

    # -- request / response -----------------------------------------------

    def _send(self, obj: dict) -> None:
        self._td.td_send(self._client_id, json.dumps(obj).encode())

    async def request(self, obj: dict, timeout: float = 30.0) -> dict:
        """Send one request and wait for its answer.

        Raises `TDLibError` when TDLib answers with an `error`, so a caller
        never has to check whether a dict is a result or a failure.
        """
        if self._client_id is None:
            raise RuntimeError("TDLib client is not started")
        extra = str(next(_extra_ids))
        future = self._loop.create_future()
        self._pending[extra] = future
        self._send({**obj, "@extra": extra})
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(extra, None)
            raise TimeoutError(f"TDLib did not answer {obj['@type']} within {timeout:.0f}s")
        if result.get("@type") == "error":
            raise TDLibError(result.get("code", 0), result.get("message", "unknown error"))
        return result

    # -- inbound -----------------------------------------------------------

    def _handle(self, event: dict) -> None:
        """Called on the reader thread. Everything it touches is hopped onto the
        client's own event loop, because futures and queues are not thread-safe.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_on_loop, event)

    def _handle_on_loop(self, event: dict) -> None:
        extra = event.get("@extra")
        if extra is not None:
            future = self._pending.pop(extra, None)
            if future is not None and not future.done():
                future.set_result(event)
            return

        if event.get("@type") == "updateAuthorizationState":
            self._on_authorization(event["authorization_state"])
            return

        try:
            self.updates.put_nowait(event)
        except asyncio.QueueFull:
            # Dropping the oldest keeps the newest, which is what a caller
            # polling for "did my message arrive" actually wants.
            try:
                self.updates.get_nowait()
                self.updates.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass

    def _on_authorization(self, state: dict) -> None:
        self.authorization_state = state["@type"]
        if self.authorization_state == "authorizationStateWaitTdlibParameters":
            self._send(self._parameters())
        if self._state_changed is not None:
            self._state_changed.set()

    def _parameters(self) -> dict:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        return {
            "@type": "setTdlibParameters",
            "use_test_dc": False,
            "database_directory": str(self.database_dir),
            "files_directory": str(self.database_dir / "files"),
            "database_encryption_key": "",
            "use_file_database": True,
            "use_chat_info_database": True,
            "use_message_database": True,
            # The whole point. TDLib will not accept or create an encrypted chat
            # with this off, and the failure is a silent absence of updates
            # rather than an error.
            "use_secret_chats": True,
            "api_id": TELEGRAM_API_ID,
            "api_hash": TELEGRAM_API_HASH,
            "system_language_code": "en",
            "device_model": "telegram-mcp",
            "system_version": "",
            "application_version": "1.0",
        }

    # -- convenience -------------------------------------------------------

    async def drain_updates(self, of_type: Optional[set] = None) -> list[dict[str, Any]]:
        """Everything queued since the last drain, oldest first.

        Non-blocking on purpose: a tool call has to return, and "nothing new"
        is a real answer.
        """
        out = []
        while True:
            try:
                event = self.updates.get_nowait()
            except asyncio.QueueEmpty:
                return out
            if of_type is None or event.get("@type") in of_type:
                out.append(event)


# --------------------------------------------------------------------------
# One started client per account, because starting one is expensive: it opens a
# database, reconnects, and re-fetches state. A tool call must not pay that.
# --------------------------------------------------------------------------

_by_account: dict[str, TDLibClient] = {}
_by_account_lock = asyncio.Lock()


async def secret_client(account: str) -> TDLibClient:
    """The account's started TDLib client.

    Raises `NotSignedIn` rather than returning a half-usable client: every
    secret-chat operation needs a real authorisation, and a client that is
    merely running would fail later with a message about whatever call happened
    to come first.
    """
    async with _by_account_lock:
        existing = _by_account.get(account)
        if existing is not None and existing._client_id is not None:
            return existing

        client = TDLibClient(account)
        state = await client.start()
        if state != "authorizationStateReady":
            await client.close()
            raise NotSignedIn(account, state)
        _by_account[account] = client
        return client


async def close_all() -> None:
    """Shut every started client down, flushing its database.

    Called on server shutdown. TDLib writes secret-chat keys lazily, and a key
    lost on exit takes its chat's history with it -- there is no way to
    re-derive one.
    """
    async with _by_account_lock:
        for client in list(_by_account.values()):
            await client.close()
        _by_account.clear()
