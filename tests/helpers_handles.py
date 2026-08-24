"""Standing in for the handle gate in a tool test.

``_open_verified_source`` hands a tool an OPEN file rather than a pathname, so a
test that used to substitute a resolved string has to substitute a handle. These
build a real :class:`VerifiedFile` over a real file: the point of the change is
that what reaches Telethon is a descriptor nothing can re-point, and a fake that
yielded a string would assert the opposite of the thing under test.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from telegram_mcp.handles import VerifiedFile


@asynccontextmanager
async def open_source(path):
    """Yield ``(source, None)`` for a real file on disk."""
    path = Path(path)
    handle = open(path, "rb")
    try:
        handle.raw.name = path.name
    except (AttributeError, TypeError):  # pragma: no cover - a wrapper without a raw
        pass
    source = VerifiedFile(handle, path.name, path.stat().st_size, path)
    try:
        yield source, None
    finally:
        source.close()


@asynccontextmanager
async def refuse_source(error):
    """Yield ``(None, error)``: the gate said no and nothing was opened."""
    yield None, error


def source_gate(resolve):
    """An ``_open_verified_source`` stand-in driven by ``resolve(raw_path)``.

    ``resolve`` returns ``(path, error)`` exactly as the old pathname gate did,
    so a fixture keeps its shape and only what it yields changes.
    """

    def gate(*, raw_path, ctx=None, tool_name=None):
        path, error = resolve(raw_path)
        if error:
            return refuse_source(error)
        return open_source(path)

    return gate


def uploaded_names(items):
    """The basenames of whatever was handed to Telethon, handles or strings."""
    return [getattr(item, "name", None) or Path(str(item)).name for item in items]
