from __future__ import annotations

from urllib.parse import unquote, urlparse
from urllib.request import getproxies

from app.config import settings


def get_proxy() -> tuple[str, str, int, bool, str | None, str | None] | None:
    """Return a Telethon-compatible proxy tuple.

    TG_PROXY wins. Otherwise try the operating system / environment proxy,
    which is what a local Telegram client commonly uses in restricted networks.
    Supported examples:
      socks5://127.0.0.1:7892
      http://127.0.0.1:7892
    """
    raw = settings.tg_proxy.strip()
    if not raw:
        proxies = getproxies()
        raw = proxies.get("https") or proxies.get("http") or ""
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    if scheme in {"socks5", "socks5h"}:
        proxy_type = "socks5"
    elif scheme == "socks4":
        proxy_type = "socks4"
    else:
        proxy_type = "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (1080 if proxy_type.startswith("socks") else 8080)
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return proxy_type, host, port, True, username, password
