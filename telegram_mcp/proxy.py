"""Reading ``TELEGRAM_PROXY_*`` configuration into Telethon's kwargs.

Separated from :mod:`telegram_mcp.connection` because it is a different job:
everything here turns environment variables into a ``(proxy, connection)`` pair
and never touches a socket, a session or an account. Splitting it out is what
lets the connection module be about staying connected.

The names are re-exported from ``connection`` because ``runtime`` star-imports
that module and ``runner`` imports :func:`parse_port` from it - moving code
should not move anyone's import.
"""

import os
from typing import Any, Optional

from telegram_mcp.settings import ValidationError, _parse_bool_env

__all__ = [
    "_PROXY_TYPES_ALL",
    "_PROXY_TYPES_SOCKS_HTTP",
    "_build_proxy_for_label",
    "_get_proxy_env",
    "parse_port",
]


_PROXY_TYPES_SOCKS_HTTP = {"socks5", "socks4", "http"}
_PROXY_TYPES_ALL = _PROXY_TYPES_SOCKS_HTTP | {"mtproxy"}

# TCP ports a socket can actually be reached on. 0 means "any free port" to
# bind() and nothing at all to connect(), and the rest are simply out of range.
_MIN_PORT = 1
_MAX_PORT = 65535


def parse_port(raw: str, variable: str) -> int:
    """Parse a TCP port from configuration, refusing anything unreachable.

    Shared by the proxy settings and the HTTP transport's ``MCP_PORT``: both
    used to take the value on trust, so ``0``/``-1``/``70000`` were carried all
    the way to the first connection attempt and surfaced there as an unrelated
    socket error.
    """
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{variable} must be an integer between {_MIN_PORT} and {_MAX_PORT}, got {raw!r}."
        ) from exc
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ValidationError(
            f"{variable} must be between {_MIN_PORT} and {_MAX_PORT}, got {port}."
        )
    return port


def _get_proxy_env(name: str, label: str) -> Optional[str]:
    """Resolve a TELEGRAM_PROXY_* env var with optional ``_<LABEL>`` suffix.

    Per-account values override the unsuffixed defaults so a global proxy can
    coexist with per-label overrides.
    """
    suffixed = os.getenv(f"TELEGRAM_PROXY_{name}_{label.upper()}")
    if suffixed:
        return suffixed
    return os.getenv(f"TELEGRAM_PROXY_{name}") or None


def _build_proxy_for_label(label: str) -> tuple[Optional[Any], Optional[Any]]:
    """Return ``(proxy, connection)`` kwargs for ``TelegramClient`` for a label.

    Reads ``TELEGRAM_PROXY_*`` env vars (with optional ``_<LABEL>`` suffix).
    Returns ``(None, None)`` when no proxy is configured. Raises
    :class:`ValidationError` for malformed configuration so the server fails
    fast instead of silently bypassing the proxy.
    """
    proxy_type = _get_proxy_env("TYPE", label)
    if not proxy_type:
        return None, None

    proxy_type = proxy_type.strip().lower()
    if proxy_type not in _PROXY_TYPES_ALL:
        raise ValidationError(
            f"Invalid TELEGRAM_PROXY_TYPE '{proxy_type}'. "
            f"Expected one of: {', '.join(sorted(_PROXY_TYPES_ALL))}."
        )

    host = _get_proxy_env("HOST", label)
    port_raw = _get_proxy_env("PORT", label)
    if not host or not port_raw:
        raise ValidationError(
            "TELEGRAM_PROXY_HOST and TELEGRAM_PROXY_PORT are required when "
            "TELEGRAM_PROXY_TYPE is set."
        )
    port = parse_port(port_raw, "TELEGRAM_PROXY_PORT")

    if proxy_type == "mtproxy":
        secret = _get_proxy_env("SECRET", label)
        if not secret:
            raise ValidationError("TELEGRAM_PROXY_SECRET is required for mtproxy.")
        try:
            from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        except ImportError as exc:  # pragma: no cover - defensive guard
            raise ValidationError(
                "Telethon MTProxy connection class is unavailable; upgrade telethon."
            ) from exc
        return (host, port, secret), ConnectionTcpMTProxyRandomizedIntermediate

    # SOCKS4/SOCKS5/HTTP via python-socks (Telethon's optional dependency).
    try:
        import python_socks  # noqa: F401
    except ImportError as exc:
        raise ValidationError(
            f"Proxy type '{proxy_type}' requires the 'python-socks' package. "
            "Install it with `pip install python-socks` or `uv sync --extra proxy`."
        ) from exc

    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": host,
        "port": port,
        "rdns": _parse_bool_env(_get_proxy_env("RDNS", label), default=True),
    }
    username = _get_proxy_env("USERNAME", label)
    password = _get_proxy_env("PASSWORD", label)
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy, None
