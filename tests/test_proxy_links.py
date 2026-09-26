"""Finding proxies where people actually put them, and refusing what is not one.

Proxies arrive as `tg://proxy` / `t.me/proxy` / `t.me/socks` links, as pasted lists,
and in channels that hide them behind one word each (@ProxyDaemi, checked 2026-09-26:
"proxy | proxy | proxy", three hidden links between ads). Secrets come hex or base64,
URL-safe or standard, often URL-encoded. The same proxy written two ways is one proxy.
"""

import base64
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from telegram_mcp import proxy_links as pl

PLAIN = "0123456789abcdef0123456789abcdef"
DD = "dd" + PLAIN
DOMAIN = "www.example.com"
EE = "ee" + PLAIN + DOMAIN.encode().hex()


def _b64(hex_secret, urlsafe=True, pad=False):
    raw = bytes.fromhex(hex_secret)
    text = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(raw).decode()
    return text if pad else text.rstrip("=")


# --- one link ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        f"tg://proxy?server=1.2.3.4&port=443&secret={PLAIN}",
        f"https://t.me/proxy?server=1.2.3.4&port=443&secret={PLAIN}",
        f"http://telegram.me/proxy?server=1.2.3.4&port=443&secret={PLAIN}",
        f"t.me/proxy?server=1.2.3.4&port=443&secret={PLAIN}",
    ],
)
def test_every_mtproto_link_shape_is_recognised(link):
    proxy = pl.parse_link(link)
    assert (proxy.kind, proxy.variant, proxy.host, proxy.port) == (
        "mtproto",
        "plain",
        "1.2.3.4",
        443,
    )
    assert proxy.secret == PLAIN


def test_the_variant_comes_from_the_first_byte():
    assert pl.parse_link(f"tg://proxy?server=h&port=1&secret={DD}").variant == "dd"
    ee = pl.parse_link(f"tg://proxy?server=h&port=1&secret={EE}")
    assert (ee.variant, ee.domain) == ("ee", DOMAIN)


@pytest.mark.parametrize(
    "secret",
    [
        _b64(PLAIN),
        _b64(PLAIN, urlsafe=False, pad=True),
        quote(_b64(PLAIN, urlsafe=False, pad=True), safe=""),  # %2B, %2F, %3D
        PLAIN.upper(),
    ],
)
def test_secrets_in_any_encoding_normalise_to_one_proxy(secret):
    proxy = pl.parse_link(f"tg://proxy?server=h.example&port=80&secret={secret}")
    assert proxy.secret == PLAIN
    reference = pl.parse_link(f"tg://proxy?server=h.example&port=80&secret={PLAIN}")
    assert proxy.fingerprint == reference.fingerprint


def test_a_base64_ee_secret_keeps_its_domain():
    proxy = pl.parse_link(f"tg://proxy?server=h&port=443&secret={_b64(EE)}")
    assert (proxy.variant, proxy.domain, proxy.secret) == ("ee", DOMAIN, EE)


def test_socks_links_with_and_without_credentials():
    bare = pl.parse_link("https://t.me/socks?server=5.6.7.8&port=1080")
    assert (bare.kind, bare.host, bare.port, bare.username) == ("socks5", "5.6.7.8", 1080, None)
    creds = pl.parse_link("tg://socks?server=5.6.7.8&port=1080&user=u&pass=p%40ss")
    assert (creds.username, creds.password) == ("u", "p@ss")
    assert bare.fingerprint != creds.fingerprint


@pytest.mark.parametrize(
    "url, kind",
    [
        ("socks5://u:p@9.9.9.9:1080", "socks5"),
        ("socks4://9.9.9.9:1080", "socks4"),
        ("http://9.9.9.9:3128", "http"),
    ],
)
def test_scheme_urls_for_socks_and_http(url, kind):
    proxy = pl.parse_link(url)
    assert (proxy.kind, proxy.host, proxy.port) == (kind, "9.9.9.9", int(url.rsplit(":", 1)[1]))


@pytest.mark.parametrize(
    "link, reason",
    [
        (f"tg://proxy?server=h&port=0&secret={PLAIN}", "port out of range"),
        (f"tg://proxy?server=h&port=70000&secret={PLAIN}", "port out of range"),
        (f"tg://proxy?server=h&port=x&secret={PLAIN}", "port is not a number"),
        (f"tg://proxy?port=443&secret={PLAIN}", "missing server"),
        (f"tg://proxy?server=a b&port=443&secret={PLAIN}", "server contains spaces"),
        ("tg://proxy?server=h&port=443", "missing secret"),
        ("tg://proxy?server=h&port=443&secret=%%%%", "secret is neither hex nor base64"),
        ("tg://proxy?server=h&port=443&secret=abcd", "secret has the wrong length"),
        (f"tg://proxy?server=h&port=443&secret=ee{PLAIN}", "FakeTLS secret has no domain"),
    ],
)
def test_invalid_links_say_why(link, reason):
    with pytest.raises(ValueError, match=reason):
        pl.parse_link(link)


# --- text of any length ----------------------------------------------------------------


def test_pasted_text_yields_every_valid_proxy_once_and_every_invalid_with_its_reason():
    text = f"""
    Fresh proxies!!! tg://proxy?server=1.1.1.1&port=443&secret={PLAIN}
    same one again: https://t.me/proxy?server=1.1.1.1&port=443&secret={_b64(PLAIN)}
    broken: tg://proxy?server=2.2.2.2&port=0&secret={PLAIN}
    2.2.2.3:8443:{DD}
    socks5://3.3.3.3:1080
    and a normal link https://example.com/page that is not a proxy
    """
    found, invalid = pl.parse_text(text)
    assert [(p.host, p.variant or p.kind) for p in found] == [
        ("1.1.1.1", "plain"),
        ("2.2.2.3", "dd"),
        ("3.3.3.3", "socks5"),
    ]
    assert [item.reason for item in invalid] == ["port out of range"]


def test_an_invalid_entry_never_shows_its_secret():
    _, invalid = pl.parse_text(f"tg://proxy?server=2.2.2.2&port=0&secret={PLAIN}")
    assert PLAIN not in invalid[0].line
    assert "2.2.2.2" in invalid[0].line


def test_a_proxy_never_shows_its_secret_or_password_when_described():
    proxy = pl.parse_link("tg://socks?server=5.6.7.8&port=1080&user=u&pass=hunter2")
    mt = pl.parse_link(f"tg://proxy?server=h&port=1&secret={EE}")
    for item in (proxy, mt):
        text = repr(item.describe()) + repr(item)
        assert "hunter2" not in text and PLAIN not in text


# --- a channel post: hidden links, url entities, buttons ------------------------------


class _TextUrl:
    def __init__(self, url):
        self.url = url


class _Url:
    pass


def test_hidden_links_url_entities_and_buttons_are_all_read():
    visible = f"see tg://proxy?server=4.4.4.4&port=443&secret={PLAIN}"
    message = SimpleNamespace(
        message="پروکسی | پروکسی | " + visible,
        get_entities_text=lambda: [
            (
                _TextUrl(f"https://t.me/proxy?server=5.5.5.5&port=443&secret={_b64(PLAIN)}"),
                "پروکسی",
            ),
            (_TextUrl("https://example.com/ad"), "پروکسی"),
            (_Url(), f"tg://proxy?server=4.4.4.4&port=443&secret={PLAIN}"),
        ],
        reply_markup=SimpleNamespace(
            rows=[
                SimpleNamespace(
                    buttons=[
                        SimpleNamespace(url=f"tg://proxy?server=6.6.6.6&port=443&secret={DD}"),
                        SimpleNamespace(text="no url here"),
                    ]
                )
            ]
        ),
    )
    found, invalid = pl.from_message(message)
    assert sorted(p.host for p in found) == ["4.4.4.4", "5.5.5.5", "6.6.6.6"]
    assert invalid == []


def test_a_message_without_text_or_markup_yields_nothing():
    assert pl.from_message(SimpleNamespace(message=None)) == ([], [])
