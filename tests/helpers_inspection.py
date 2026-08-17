"""Fakes shared by the two inspection test modules.

``test_inspection_transfer.py`` exercises the bounded-transfer helpers in
``telegram_mcp/media_transfer.py``; ``test_inspection.py`` exercises the MCP
tools in ``telegram_mcp/tools/inspection.py``. Both need the same download
stubs and the same photo-size vocabulary, so they live here rather than being
copied into each file.
"""

import datetime

from telethon.tl import types as t


class _Iter:
    def __init__(self, chunks, log):
        self._chunks, self._log = chunks, log

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()

    async def close(self):
        self._log.append("closed")


class _CountingClient:
    """Streams a payload, recording every location and every byte delivered."""

    def __init__(self, total=512, chunk=1024, inline=b"inline-bytes"):
        self.total, self.chunk, self.inline = total, chunk, inline
        self.delivered = 0
        self.locations = []
        self.thumbs_asked = []

    def iter_download(self, target):
        self.locations.append(target)
        client = self

        class Chunks:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if client.delivered >= client.total:
                    raise StopAsyncIteration
                size = min(client.chunk, client.total - client.delivered)
                client.delivered += size
                return b"x" * size

            async def close(self):
                pass

        return Chunks()

    async def download_media(self, owner, file=None, thumb=None):
        self.thumbs_asked.append(thumb)
        return self.inline


_SMALL = t.PhotoSize(type="m", w=320, h=320, size=10_000)
_MEDIUM = t.PhotoSize(type="x", w=800, h=800, size=90_000)
_ORIGINAL = t.PhotoSizeProgressive(type="y", w=2560, h=2560, sizes=[2_000_000])


def _photo_with(sizes, video_sizes=None):
    return t.Photo(
        id=1,
        access_hash=2,
        file_reference=b"\x00",
        date=datetime.datetime.now(),
        sizes=list(sizes),
        dc_id=2,
        has_stickers=False,
        video_sizes=video_sizes,
    )
