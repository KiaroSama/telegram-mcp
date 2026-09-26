"""FakeTLS (``ee``) MTProto proxies: the MTProto stream dressed as a TLS 1.3 session.

Telethon 1.45 strips an ``ee`` secret down to its 16 key bytes and speaks plain
obfuscated MTProto, which a FakeTLS proxy drops. This adds the disguise: a 517-byte
ClientHello whose SNI is the secret's domain and whose random field is an HMAC of the
key, a check of the server's answering digest, and ``17 03 03`` record framing both ways.

Adapted from ``mtproto_faketls.py`` in alikhil/mtproto-proxy-checker (commit
3c1be956812c), itself based on MKultra's MK_XRAYchecker
(https://github.com/MKultra6969/MK_XRAYchecker). Changes: the full ``ee`` secret (hex or
base64) is accepted; the handshake is bounded by the connect timeout; the writer offers
``wait_closed`` because Telethon awaits it on disconnect; no blocking DNS lookup.

    MIT License

    Copyright (c) 2026 MTProto Proxy Checker Contributors

    Permission is hereby granted, free of charge, to any person obtaining a copy of this
    software and associated documentation files (the "Software"), to deal in the Software
    without restriction, including without limitation the rights to use, copy, modify,
    merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be included in all copies
    or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
    INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
    PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
    CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
    OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import random
import time
from typing import Optional

from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

__all__ = ["ConnectionTcpMTProxyFakeTLS", "FakeTLSCodec", "FakeTLSReader", "FakeTLSWriter"]

_HELLO_SIZE = 517
_MAX_RECORD = 16384 + 24
_P25519 = 2**255 - 19
_RANDOM = random.SystemRandom()
_CHANGE_CIPHER_THEN_DATA = b"\x14\x03\x03\x00\x01\x01\x17\x03\x03"


def _secret_bytes(secret: str) -> bytes:
    text = secret.strip()
    try:
        return bytes.fromhex(text)
    except ValueError:
        candidate = text.replace("-", "+").replace("_", "/").rstrip("=")
        candidate += "=" * (-len(candidate) % 4)
        try:
            return base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("FakeTLS secret is neither hex nor base64") from exc


def _x25519_public_key() -> bytes:
    n = _RANDOM.randrange(_P25519)
    return ((n * n) % _P25519).to_bytes(32, "little")


class FakeTLSCodec:
    """Builds the ClientHello and checks the ServerHello for one connection."""

    def __init__(self, secret: str) -> None:
        raw = _secret_bytes(secret)
        if raw[:1] != b"\xee" or len(raw) < 18:
            raise ValueError("not a FakeTLS secret: expected ee + 16 key bytes + domain")
        self.key = raw[1:17]
        self.domain = raw[17:]
        self.session_id = b""
        self.random = b""

    def client_hello(self, now: Optional[int] = None) -> bytes:
        self.session_id = os.urandom(32)
        name = self.domain
        server_name = (
            b"\x00\x00"
            + (len(name) + 5).to_bytes(2, "big")
            + (len(name) + 3).to_bytes(2, "big")
            + b"\x00"
            + len(name).to_bytes(2, "big")
            + name
        )
        extensions_before_padding = b"".join(
            (
                b"\x4a\x4a\x00\x00",
                server_name,
                b"\x00\x17\x00\x00",
                b"\xff\x01\x00\x01\x00",
                b"\x00\x0a\x00\x0a\x00\x08\xba\xba\x00\x1d\x00\x17\x00\x18",
                b"\x00\x0b\x00\x02\x01\x00",
                b"\x00\x23\x00\x00",
                b"\x00\x10\x00\x0e\x00\x0c\x02\x68\x32\x08\x68\x74\x74\x70\x2f\x31\x2e\x31",
                b"\x00\x05\x00\x05\x01\x00\x00\x00\x00",
                b"\x00\x0d\x00\x12\x00\x10\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01"
                b"\x08\x06\x06\x01",
                b"\x00\x12\x00\x00",
                b"\x00\x33\x00\x2b\x00\x29\xba\xba\x00\x01\x00\x00\x1d\x00\x20"
                + _x25519_public_key(),
                b"\x00\x2d\x00\x02\x01\x01",
                b"\x00\x2b\x00\x0b\x0a\x9a\x9a\x03\x04\x03\x03\x03\x02\x03\x01",
                b"\x00\x1b\x00\x03\x02\x00\x02",
                b"\x1a\x1a\x00\x01\x00",
            )
        )
        head = (
            b"\x16\x03\x01\x02\x00"  # handshake record, 512 bytes
            + b"\x01\x00\x01\xfc"  # client hello, 508 bytes
            + b"\x03\x03"
        )
        middle = (
            b"\x20"
            + self.session_id
            + b"\x00\x20"
            + b"\xfa\xfa\x13\x01\x13\x02\x13\x03\xc0\x2b\xc0\x2f\xc0\x2c\xc0\x30"
            + b"\xcc\xa9\xcc\xa8\xc0\x13\xc0\x14\x00\x9c\x00\x9d\x00\x2f\x00\x35"
            + b"\x01\x00"
        )
        fixed = len(head) + 32 + len(middle) + 2 + len(extensions_before_padding) + 4
        padding = _HELLO_SIZE - fixed
        if padding < 0:
            raise ValueError("FakeTLS domain is too long for one ClientHello")
        extensions = extensions_before_padding + b"\x00\x15" + padding.to_bytes(2, "big")
        extensions += b"\x00" * padding
        blank = head + b"\x00" * 32 + middle + len(extensions).to_bytes(2, "big") + extensions
        digest = hmac.new(self.key, blank, hashlib.sha256).digest()
        stamp = int(time.time() if now is None else now).to_bytes(4, "little")
        self.random = digest[:28] + bytes(a ^ b for a, b in zip(stamp, digest[28:]))
        return blank[:11] + self.random + blank[43:]

    def verify_server_hello(self, hello: bytes) -> bool:
        if len(hello) < 127 + len(_CHANGE_CIPHER_THEN_DATA) or not hello.startswith(
            b"\x16\x03\x03"
        ):
            return False
        if hello[127 : 127 + len(_CHANGE_CIPHER_THEN_DATA)] != _CHANGE_CIPHER_THEN_DATA:
            return False
        if hello[44:76] != self.session_id:
            return False
        blank = hello[:11] + b"\x00" * 32 + hello[43:]
        expected = hmac.new(self.key, self.random + blank, hashlib.sha256).digest()
        return hmac.compare_digest(hello[11:43], expected)


class FakeTLSReader:
    """Unwraps ``17 03 03`` records; skips change-cipher-spec records."""

    def __init__(self, upstream) -> None:
        self.upstream = upstream
        self.buffer = bytearray()

    async def _record(self) -> bytes:
        while True:
            header = await self.upstream.readexactly(5)
            kind, version = header[:1], header[1:3]
            if kind not in (b"\x14", b"\x17") or version != b"\x03\x03":
                raise ConnectionError("FakeTLS proxy sent a record that is not TLS data")
            data = await self.upstream.readexactly(int.from_bytes(header[3:5], "big"))
            if kind == b"\x17":
                return data

    async def readexactly(self, n: int) -> bytes:
        while len(self.buffer) < n:
            self.buffer += await self._record()
        data, self.buffer = bytes(self.buffer[:n]), self.buffer[n:]
        return data

    async def read_server_hello(self) -> bytes:
        hello = await self.upstream.readexactly(127 + 6 + 3 + 2)
        return hello + await self.upstream.readexactly(int.from_bytes(hello[-2:], "big"))

    def at_eof(self) -> bool:
        return self.upstream.at_eof() and not self.buffer

    async def _wait_for_data(self, *args):
        return await self.upstream._wait_for_data(*args)


class FakeTLSWriter:
    """Wraps every write in ``17 03 03`` records of at most 16408 bytes."""

    def __init__(self, upstream) -> None:
        self.upstream = upstream

    def write(self, data: bytes) -> None:
        for start in range(0, len(data), _MAX_RECORD):
            chunk = data[start : start + _MAX_RECORD]
            self.upstream.write(b"\x17\x03\x03" + len(chunk).to_bytes(2, "big") + chunk)

    async def drain(self):
        return await self.upstream.drain()

    def close(self):
        return self.upstream.close()

    async def wait_closed(self):
        return await self.upstream.wait_closed()

    def get_extra_info(self, name, default=None):
        return self.upstream.get_extra_info(name, default)

    @property
    def transport(self):
        return self.upstream.transport


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """A Telethon connection to an ``ee`` proxy: ``proxy=(host, port, full_ee_secret)``."""

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        self.fake_tls = FakeTLSCodec(proxy[2])
        super().__init__(
            ip,
            port,
            dc_id,
            loggers=loggers,
            proxy=(proxy[0], proxy[1], self.fake_tls.key.hex()),
            local_addr=local_addr,
        )

    async def _connect(self, timeout=None, ssl=None):
        local_addr = self._local_addr
        if isinstance(local_addr, str):
            local_addr = (local_addr, 0)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host=self._ip, port=self._port, local_addr=local_addr),
            timeout=timeout,
        )
        self._writer.write(self.fake_tls.client_hello())
        await self._writer.drain()
        self._reader = FakeTLSReader(self._reader)
        self._writer = FakeTLSWriter(self._writer)
        hello = await asyncio.wait_for(self._reader.read_server_hello(), timeout=timeout)
        if not self.fake_tls.verify_server_hello(hello):
            raise ConnectionError("FakeTLS proxy's server hello did not verify")
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()
