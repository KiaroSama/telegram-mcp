"""FakeTLS (``ee``) proxies: the MTProto stream dressed as a TLS 1.3 session.

Telethon strips the ``ee`` prefix and cannot speak the disguise, so the handshake and
the record framing are ours (adapted from alikhil/mtproto-proxy-checker, MIT). These
tests hold the parts a proxy checks: the ClientHello's size, its SNI, the HMAC in its
random field, the server hello's digest, and the 0x17 record framing both ways.
"""

import asyncio
import base64
import hashlib
import hmac
import struct
import time

import pytest

from telegram_mcp import proxy_faketls as ft

KEY = bytes(range(16))
DOMAIN = b"www.example.com"
FULL = (b"\xee" + KEY + DOMAIN).hex()


def test_the_codec_accepts_the_full_secret_hex_or_base64():
    for secret in (FULL, base64.urlsafe_b64encode(bytes.fromhex(FULL)).decode().rstrip("=")):
        codec = ft.FakeTLSCodec(secret)
        assert (codec.key, codec.domain) == (KEY, DOMAIN)


@pytest.mark.parametrize("secret", [(b"\xee" + KEY).hex(), KEY.hex(), "zz"])
def test_a_secret_that_is_not_faketls_is_refused(secret):
    with pytest.raises(ValueError):
        ft.FakeTLSCodec(secret)


def test_the_client_hello_is_517_bytes_and_names_the_domain():
    hello = ft.FakeTLSCodec(FULL).client_hello()
    assert len(hello) == 517
    assert hello[:3] == b"\x16\x03\x01"
    assert DOMAIN in hello


def test_the_random_field_is_the_secret_hmac_with_the_time_in_its_last_bytes():
    codec = ft.FakeTLSCodec(FULL)
    now = int(time.time())
    hello = codec.client_hello(now=now)
    random = hello[11:43]
    blank = hello[:11] + b"\x00" * 32 + hello[43:]
    digest = hmac.new(KEY, blank, hashlib.sha256).digest()
    assert random[:28] == digest[:28]
    stamped = bytes(a ^ b for a, b in zip(random[28:], digest[28:]))
    assert struct.unpack("<I", stamped)[0] == now


def _server_hello(codec, *, tamper=False):
    head = b"\x16\x03\x03" + b"\x00" * 8  # 11 bytes before the digest
    body = head + b"\x00" * 32 + b"\x20" + codec.session_id
    body += b"\x00" * (127 - len(body))
    body += b"\x14\x03\x03\x00\x01\x01\x17\x03\x03" + b"\x00\x04" + b"data"
    digest = hmac.new(codec.key, codec.random + body, hashlib.sha256).digest()
    if tamper:
        digest = bytes([digest[0] ^ 1]) + digest[1:]
    return body[:11] + digest + body[43:]


def test_a_server_hello_with_the_right_digest_verifies_and_a_tampered_one_does_not():
    codec = ft.FakeTLSCodec(FULL)
    codec.client_hello()
    assert codec.verify_server_hello(_server_hello(codec)) is True
    assert codec.verify_server_hello(_server_hello(codec, tamper=True)) is False
    assert codec.verify_server_hello(b"\x16\x03\x03short") is False


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data


def test_writes_are_framed_as_application_data_records():
    sink = _Sink()
    payload = bytes(40000)
    ft.FakeTLSWriter(sink).write(payload)
    records, rest = [], sink.data
    while rest:
        assert rest[:3] == b"\x17\x03\x03"
        length = int.from_bytes(rest[3:5], "big")
        assert length <= 16408
        records.append(rest[5 : 5 + length])
        rest = rest[5 + length :]
    assert b"".join(records) == payload and len(records) == 3


def test_reads_cross_record_boundaries_and_skip_change_cipher_spec():
    async def scenario():
        stream = asyncio.StreamReader()
        stream.feed_data(b"\x14\x03\x03\x00\x01\x01")  # change cipher spec: skipped
        stream.feed_data(b"\x17\x03\x03\x00\x03abc")
        stream.feed_data(b"\x17\x03\x03\x00\x04defg")
        stream.feed_eof()
        reader = ft.FakeTLSReader(stream)
        return await reader.readexactly(5), await reader.readexactly(2)

    assert asyncio.run(scenario()) == (b"abcde", b"fg")
