"""The one place this server reaches the secret-chat implementation.

Nineteen call sites across five tool modules used to reach TDLib through
`tdlib_registry.secret_client`. They reach `secret_manager` here instead, and this
module is the ONLY one that imports `telethon_secret_chat` — a test enforces that,
because the package is maintained in its own repository and updated independently, so
an upstream signature change has to be a one-file edit rather than a search.

**Why the manager rides the caller's client.** TDLib keeps its own authorization and
cannot import a Telethon one, so every account that used secret chats showed the
operator two devices. The manager is constructed over the Telethon client this server
already holds for that account, whose lease `claim_session` already owns. It opens no
socket and authorises nothing, which is Principle I discharged by construction rather
than by a check.

The caching rules below are not decoration. They are what the registry this replaces
learned: a backend cached against a label rather than against the CLIENT hands a caller
a manager wired to a generation nobody uses any more, and a backend started while
shutdown is flushing races the flush for key material that cannot be recovered.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

from telethon_secret_chat import FileStorage, SecretChatManager
from telethon_secret_chat.errors import (
    ChatClosed,
    ChatNotReady,
    LayerUnsupported,
    MessageRejected,
    ParameterRejected,
    ResendUnsatisfiable,
    SecretChatError,
    StorageRequired,
)
from telethon_secret_chat.schema import secret_tl

from telegram_mcp import secret_history
from telegram_mcp.alias_store import restrict_to_owner
from telegram_mcp.safe_log import log_event
from telegram_mcp.settings import state_dir

# Re-exported, because this module is the ONLY one allowed to import the package
# and a test enforces that. Two callers need pieces of it - `secret_common` to tell
# a refusal from a defect, and the typing tool to name one protocol action - and
# letting either import it directly would turn "an upstream change is a one-file
# edit" into "grep and hope".
__all__ = [
    "ChatClosed",
    "ChatNotReady",
    "LayerUnsupported",
    "MessageRejected",
    "ParameterRejected",
    "ResendUnsatisfiable",
    "SecretChatError",
    "SecretChatUnavailable",
    "StorageRequired",
    "close_all",
    "secret_manager",
    "secret_tl",
]


class SecretChatUnavailable(RuntimeError):
    """A secret-chat backend cannot be served right now, and why.

    Its own type because the fixes are specific and nothing else here needs them:
    this is not a permission problem, a network problem or a wrong account name, and
    answering with any of those sends the caller looking in the wrong place.
    """

    def __init__(self, account: str, reason: str):
        super().__init__(f"Secret chats are unavailable for {account!r}: {reason}.")
        self.account = account
        self.reason = reason


#: account -> its started manager.
_by_account: Dict[str, SecretChatManager] = {}

#: account -> the client its manager was built over. A manager is reused only while
#: this is still the account's current client; see `secret_manager`.
_verified_against: Dict[str, object] = {}

#: Set for the life of the process once shutdown starts. Never cleared: a server that
#: has begun flushing key material does not go back to serving.
_closing = False

_lock = asyncio.Lock()


def _telethon_client(account: str):
    """The account's current client generation.

    Indirection on purpose: `connection` reaches this module through the tool layer,
    so importing it at module scope closes a cycle, and a test needs one place to
    point somewhere else.
    """
    from telegram_mcp.connection import get_client

    return get_client(account)


def _storage_for(account: str) -> FileStorage:
    """Where this account's key material lives.

    Under the server's state directory, never the install directory, and one
    directory per account so two accounts in one process cannot open each other's.
    The package requires storage explicitly and has no default, which is the right
    call — a library that quietly writes key material writes it somewhere the
    operator did not protect.
    """
    path = state_dir() / "secret-chats" / account
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileStorage(path)


#: account -> the OS lock this process holds on that account's key store.
_store_locks: Dict[str, object] = {}


def _store_lock_dir() -> Path:
    return state_dir() / "secret-chats"


def _store_identity(account: str) -> str:
    return f"secret-chat-store:{account}"


async def _claim_store(account: str) -> None:
    """Hold this account's key store for the life of the process, or refuse.

    The session lock stops two processes sharing one SESSION; it does not stop two
    logins of one account, under one label, sharing this folder. Two managers over
    one store overwrite each other's keys, so the second process is refused.
    Released by `close_all`, and by the OS if the process dies.
    """
    if account in _store_locks:
        return
    from telegram_mcp.singleton import SessionLock, SessionLockError

    lock = SessionLock(_store_identity(account), lock_dir=_store_lock_dir())
    try:
        await asyncio.to_thread(lock.acquire, grace_seconds=2.0, poll_interval=0.2)
    except SessionLockError:
        raise SecretChatUnavailable(
            account,
            "another telegram-mcp process holds this account's secret-chat keys; stop "
            "it first - two processes over one key store overwrite each other's keys",
        ) from None
    _store_locks[account] = lock


def _owner_path(account: str) -> Path:
    """Which Telegram account a label's key store was written for."""
    return state_dir() / "secret-chats" / f"{account}.owner.json"


async def _bind_store(account: str, client) -> None:
    """Refuse a key store written for a different Telegram account.

    The store is found by LABEL, and a label can be re-pointed at another account in
    `.env`. Without this the new account would open - and try to decrypt with - the
    old account's keys. A store with no record yet (every store written before this
    check existed) is adopted by the account using it now.
    """
    me = await client.get_me(input_peer=True)
    user_id = getattr(me, "user_id", None) or getattr(me, "id", None)
    path = _owner_path(account)
    if path.exists():
        try:
            recorded = json.loads(path.read_text(encoding="utf-8")).get("user_id")
        except (OSError, ValueError, AttributeError):
            recorded = None
        if recorded is not None and user_id is not None and int(recorded) != int(user_id):
            raise SecretChatUnavailable(
                account,
                "this label's secret-chat keys belong to a different Telegram account "
                f"(user {recorded}, not {user_id}); move state/secret-chats/{account}* "
                "aside or give this account its own label",
            )
        if recorded is not None:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"user_id": user_id}), encoding="utf-8")
    restrict_to_owner(path)


async def secret_manager(account: str) -> SecretChatManager:
    """The account's started secret-chat manager.

    Raises rather than returning a half-usable object: every secret-chat operation
    needs a real authorization, and a manager that is merely constructed would fail
    later with a message about whichever call happened to come first.

    Lazy. An account that never opens a secret chat never builds one.
    """
    if _closing:
        raise SecretChatUnavailable(account, "the server is shutting down")

    client = _telethon_client(account)

    async with _lock:
        # RE-READ both inside the lock. The checks above only describe the moment
        # this caller arrived; a caller that queued on the lock passed them and then
        # would act on state that changed while it waited.
        if _closing:
            raise SecretChatUnavailable(account, "the server is shutting down")
        if _telethon_client(account) is not client:
            raise SecretChatUnavailable(
                account,
                "the account was reconfigured while its secret-chat backend was being "
                "started, so this generation is no longer current; retry the call",
            )

        existing = _by_account.get(account)
        if existing is not None and _verified_against.get(account) is client:
            return existing

        if existing is not None:
            # A manager from a generation that has since been replaced. Stop it
            # before building its successor, so the old subscription is gone and its
            # chats are flushed; two managers over one account would mean two key
            # stores for one conversation.
            await _stop(existing)

        await _claim_store(account)
        await _bind_store(account, client)
        manager = SecretChatManager(client, _storage_for(account))
        manager.on("ChatRequested", _accept_incoming(manager, account))
        manager.on("MessageReceived", _remember(account))
        await manager.start()
        _by_account[account] = manager
        _verified_against[account] = client
        return manager


def _accept_incoming(manager: SecretChatManager, account: str):
    """Answer an incoming secret-chat request, because no tool can.

    The previous backend completed the handshake inside itself: an invitation arrived
    and became a usable chat with nothing asked of this server. The package hands the
    decision back instead, which is the better design for a library and a CAPABILITY
    LOSS here - the published tool surface is frozen, so there is no
    `accept_secret_chat` for a caller to reach for, and an unanswered request would sit
    at `pending` until it expired with every tool correctly refusing to send into it.

    Accepting restores exactly what the operator had. It is also what the account's
    other clients do, and it commits nothing: a chat that is accepted and never used
    costs one key, and `close_secret_chat` ends it.

    A failure here is logged and dropped rather than raised. This runs inside the
    package's own update dispatch, where an exception would take down the subscription
    that every OTHER chat on this account also depends on, to punish one bad
    invitation.
    """

    async def _handler(event):
        try:
            await manager.accept(event.chat_id)
        except Exception as error:
            log_event(
                logging.WARNING,
                "could not accept an incoming secret chat; it stays pending",
                account=account,
                chat_id=getattr(event, "chat_id", None),
                error=error,
            )

    return _handler


def _remember(account: str):
    """Write every arrived message into this server's own durable history.

    The package keeps arrivals in memory, which dies with the process, and keeps no
    record of what this side SENT because nothing arrives for it. `read_secret_messages`
    published both directions across restarts for the whole life of the previous
    backend, so :mod:`telegram_mcp.secret_history` holds them and this is where the
    incoming half is caught - once, at the seam, rather than at each reading tool.

    Failures are logged, never raised: this runs inside the package's update dispatch,
    and a full disk must not tear down the subscription that decrypts every other chat.
    """

    async def _handler(event):
        try:
            secret_history.record_received(account, event)
        except Exception as error:
            log_event(
                logging.WARNING,
                "could not record a received secret message",
                account=account,
                error=error,
            )

    return _handler


async def _stop(manager: SecretChatManager) -> None:
    """Stop one manager, letting a failure surface rather than be swallowed.

    `stop()` flushes each chat. A swallowed failure here is lost key material, which
    no restart brings back, so it is reported rather than absorbed.
    """
    await manager.stop()


async def close_all() -> List[Tuple[str, BaseException]]:
    """Stop every manager, flushing key material. Idempotent.

    Returns one ``(account, error)`` per manager that did NOT close cleanly, so the
    caller can name each one. Collected rather than raised: the first account's
    failure must not skip the flush of every account after it, and a key that is
    never written is a chat's history gone for good.

    Shutdown can be reached twice - once from a signal handler and once from the
    runner's own path - so the second call must be a no-op rather than a failure.
    """
    global _closing
    _closing = True

    failures: List[Tuple[str, BaseException]] = []
    async with _lock:
        for account in list(_by_account):
            manager = _by_account.pop(account, None)
            _verified_against.pop(account, None)
            if manager is None:
                continue
            try:
                await _stop(manager)
            except Exception as error:
                # Keep the store's lock: its keys may still be flushing, and no
                # other process may open them until this one is gone.
                failures.append((account, error))
                continue
            lock = _store_locks.pop(account, None)
            if lock is not None:
                lock.release()
    return failures
