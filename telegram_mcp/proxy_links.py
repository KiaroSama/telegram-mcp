"""Turning links, pasted text and channel posts into proxies - pure, no network.

Telegram proxies travel as ``tg://proxy`` / ``t.me/proxy`` (MTProto) and ``tg://socks`` /
``t.me/socks`` links, as ``socks5://`` / ``socks4://`` / ``http://`` URLs, and as bare
``host:port:secret`` lines. A source channel hides them: @ProxyDaemi (checked 2026-09-26)
posts one word per proxy with the link behind it, between ads - so the text, the url
entities, the hidden-link entities and the inline buttons are all read.

MTProto secrets arrive hex or base64 (URL-safe or standard, often URL-encoded). They are
stored in one form, lower-case hex, so the same proxy written two ways has one
fingerprint. The first byte names the variant: ``dd`` padded, ``ee`` FakeTLS (the rest
after 16 bytes is the disguise domain), otherwise plain 16 bytes.

A secret or a password never leaves this module in anything meant to be shown:
``describe()`` and ``repr`` carry the fingerprint, and an invalid entry's line has its
secret and credentials cut out.
"""

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

__all__ = [
    "Invalid",
    "Proxy",
    "dedupe",
    "from_message",
    "parse_link",
    "parse_text",
    "redact",
]

KINDS = ("mtproto", "socks5", "socks4", "http")
_MIN_PORT, _MAX_PORT = 1, 65535
_HOST = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")
_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-.]*[A-Za-z0-9])?$")

_TG_LINK = re.compile(
    r"(?:tg://|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/)(?:proxy|socks)\?[^\s<>\"'|]+",
    re.IGNORECASE,
)
# Only host:port with nothing after it: an ordinary web link is never taken for a proxy.
_SCHEME_URL = re.compile(
    r"(?:socks5h?|socks4a?|socks|http)://(?:[^\s<>\"'|/@]+@)?[^\s<>\"'|/:@]+:\d+/?"
    r"(?=[\s<>\"'|]|$)",
    re.IGNORECASE,
)
_BARE = re.compile(r"^\s*([A-Za-z0-9.\-]+):(\d{1,6}):([A-Za-z0-9+/=_%\-]{32,})\s*$")


@dataclass(frozen=True)
class Proxy:
    kind: str
    host: str
    port: int
    secret: Optional[str] = field(default=None, repr=False)
    variant: Optional[str] = None
    username: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)

    @property
    def fingerprint(self) -> str:
        material = "|".join(
            (
                self.kind,
                self.host.lower(),
                str(self.port),
                self.secret or "",
                self.username or "",
                self.password or "",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    @property
    def domain(self) -> Optional[str]:
        if self.variant != "ee" or not self.secret:
            return None
        return bytes.fromhex(self.secret)[17:].decode("ascii")

    def describe(self) -> dict:
        """What may be shown: never the secret or the password."""
        return {
            "id": self.fingerprint,
            "kind": self.kind,
            "variant": self.variant,
            "host": self.host,
            "port": self.port,
        }


@dataclass(frozen=True)
class Invalid:
    line: str
    reason: str


def redact(text: str) -> str:
    """The entry as the owner may see it: secret, user and password cut out, bounded."""
    text = re.sub(r"(?i)(secret|pass|user)=[^&\s]*", r"\1=…", text)
    text = re.sub(r"(//)[^/@\s]*@", r"\1…@", text)
    text = re.sub(r"^(\s*[^:\s]+:\d+:)\S+", r"\1…", text)
    return text if len(text) <= 120 else text[:117] + "..."


def _port(raw: Optional[str]) -> int:
    if raw is None or raw == "":
        raise ValueError("missing port")
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("port is not a number") from exc
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ValueError("port out of range")
    return port


def _host(raw: Optional[str]) -> str:
    host = (raw or "").strip()
    if not host:
        raise ValueError("missing server")
    if any(ch.isspace() for ch in host):
        raise ValueError("server contains spaces")
    if not _HOST.match(host):
        raise ValueError("server is not a host name or address")
    return host


def _secret_bytes(raw: str) -> bytes:
    text = unquote(raw.strip())
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", text):
        return bytes.fromhex(text)
    candidate = text.replace("-", "+").replace("_", "/").rstrip("=")
    candidate += "=" * (-len(candidate) % 4)
    try:
        return base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("secret is neither hex nor base64") from exc


def _mtproto(host: str, port: int, raw_secret: Optional[str]) -> Proxy:
    if not raw_secret:
        raise ValueError("missing secret")
    secret = _secret_bytes(raw_secret)
    if secret[:1] == b"\xee":
        if len(secret) <= 17:
            raise ValueError("FakeTLS secret has no domain")
        try:
            domain = secret[17:].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("FakeTLS secret's domain is not a host name") from exc
        if not _DOMAIN.match(domain):
            raise ValueError("FakeTLS secret's domain is not a host name")
        variant = "ee"
    elif secret[:1] == b"\xdd" and len(secret) == 17:
        variant = "dd"
    elif len(secret) == 16:
        variant = "plain"
    else:
        raise ValueError("secret has the wrong length")
    return Proxy("mtproto", host, port, secret=secret.hex(), variant=variant)


def _query(text: str) -> dict:
    """Split a query WITHOUT turning '+' into a space: base64 secrets contain '+'."""
    pairs = {}
    for part in text.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            pairs[key.lower()] = unquote(value)
    return pairs


def parse_link(link: str) -> Proxy:
    """One proxy from one link, or ``ValueError`` naming what is wrong with it."""
    text = link.strip()
    tg = re.match(
        r"(?i)^(?:tg://|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/)(proxy|socks)\?(.*)$", text
    )
    if tg:
        which, query = tg.group(1).lower(), _query(tg.group(2))
        host, port = _host(query.get("server")), _port(query.get("port"))
        if which == "proxy":
            return _mtproto(host, port, query.get("secret"))
        return Proxy("socks5", host, port, username=query.get("user"), password=query.get("pass"))
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme in ("socks5", "socks5h", "socks", "socks4", "socks4a", "http"):
        if parts.path not in ("", "/") or parts.query:
            raise ValueError("not a proxy address")
        kind = {"socks": "socks5", "socks5h": "socks5", "socks4a": "socks4"}.get(scheme, scheme)
        try:
            raw_port = parts.port
        except ValueError as exc:
            raise ValueError("port out of range") from exc
        if raw_port is None:
            raise ValueError("missing port")
        return Proxy(
            kind,
            _host(parts.hostname),
            _port(str(raw_port)),
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
        )
    bare = _BARE.match(text)
    if bare:
        return _mtproto(_host(bare.group(1)), _port(bare.group(2)), bare.group(3))
    raise ValueError("not a proxy link")


def _candidates(text: str) -> Iterable[str]:
    """Every link-shaped or bare-line entry, in the order it appears."""
    found = [(m.start(), m.group(0)) for m in _TG_LINK.finditer(text)]
    found += [(m.start(), m.group(0)) for m in _SCHEME_URL.finditer(text)]
    offset = 0
    for line in text.splitlines(keepends=True):
        if _BARE.match(line):
            found.append((offset, line.strip()))
        offset += len(line)
    return [value for _, value in sorted(found)]


def _collect(candidates: Iterable[str], unique: bool = True) -> Tuple[List[Proxy], List[Invalid]]:
    found: List[Proxy] = []
    invalid: List[Invalid] = []
    for candidate in candidates:
        try:
            found.append(parse_link(candidate))
        except ValueError as exc:
            reason = str(exc)
            # A plain http:// link that is not a bare host:port is an ordinary web link.
            if reason == "not a proxy address":
                continue
            invalid.append(Invalid(redact(candidate), reason))
    return (dedupe(found) if unique else found), invalid


def parse_text(text: str, unique: bool = True) -> Tuple[List[Proxy], List[Invalid]]:
    """Every proxy in pasted text, once; every broken proxy link with its reason.

    ``unique=False`` keeps repeats, for a caller that reports how many there were.
    """
    return _collect(_candidates(text or ""), unique)


def _message_urls(message: Any) -> Iterable[str]:
    getter = getattr(message, "get_entities_text", None)
    if callable(getter):
        try:
            pairs = getter() or []
        except Exception:
            pairs = []
        for entity, shown in pairs:
            url = getattr(entity, "url", None)
            if url:
                yield url
            elif shown:
                yield from _candidates(shown)
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if url:
                yield url


def from_message(message: Any, unique: bool = True) -> Tuple[List[Proxy], List[Invalid]]:
    """Proxies in one post: its text, its url and hidden-link entities, its buttons."""
    text = getattr(message, "message", None) or ""
    candidates = list(_candidates(text))
    for url in _message_urls(message):
        if _TG_LINK.fullmatch(url) or _SCHEME_URL.fullmatch(url) or _BARE.match(url):
            candidates.append(url)
    return _collect(candidates, unique)


def dedupe(proxies: Iterable[Proxy]) -> List[Proxy]:
    seen, kept = set(), []
    for proxy in proxies:
        if proxy.fingerprint not in seen:
            seen.add(proxy.fingerprint)
            kept.append(proxy)
    return kept
