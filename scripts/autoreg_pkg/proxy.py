"""Proxy parsing, per-account sticky selection and exit-geo probing."""
from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

from .util import AutoregError, redact

PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


class ProxyError(AutoregError):
    pass


@dataclass(frozen=True)
class Proxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def authenticated_url(self) -> str:
        if self.username:
            cred = quote(self.username, safe="")
            if self.password:
                cred += ":" + quote(self.password, safe="")
            return f"{self.scheme}://{cred}@{self.host}:{self.port}"
        return self.server

    def playwright(self) -> dict[str, str]:
        out = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    def requests(self) -> dict[str, str]:
        url = self.authenticated_url
        return {"http": url, "https": url}

    def safe(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}" + (" (auth)" if self.username else "")

    def __str__(self) -> str:  # never leak credentials through accidental f-strings
        return self.safe()


def parse_proxy(value: str | None) -> Proxy | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"direct", "none", "off", "0"}:
        return None
    # Common provider format: host:port:user:pass
    if "://" not in raw and raw.count(":") == 3:
        host, port, user, password = raw.split(":", 3)
        raw = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ProxyError(f"Unsupported proxy scheme {scheme!r} (use http/https/socks5)")
    if not parsed.hostname or not parsed.port:
        raise ProxyError(f"Proxy must include host and port: {value!r}")
    return Proxy(
        raw=raw, scheme=scheme, host=parsed.hostname, port=int(parsed.port),
        username=parsed.username or None, password=parsed.password or None,
    )


def parse_pool(values: Iterable[str] | None) -> list[Proxy]:
    out: list[Proxy] = []
    seen: set[tuple[str, int, str | None]] = set()
    for value in values or []:
        proxy = parse_proxy(value)
        if not proxy:
            continue
        key = (proxy.host, proxy.port, proxy.username)
        if key not in seen:
            seen.add(key)
            out.append(proxy)
    return out


def requests_proxies(proxy: Proxy | None) -> dict[str, str] | None:
    return proxy.requests() if proxy else None


def probe(proxy: Proxy | None, *, timeout: float = 12.0) -> dict[str, Any]:
    """Return exit IP geo.  Raises ProxyError with a redacted message."""
    fields = "status,message,country,countryCode,regionName,city,timezone,isp,org,as,query"
    url = f"http://ip-api.com/json/?fields={fields}"
    try:
        response = requests.get(url, proxies=requests_proxies(proxy), timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise ProxyError(f"Proxy probe failed via {proxy or 'direct'}: {redact(exc)}") from exc
    if data.get("status") != "success":
        raise ProxyError(f"Proxy probe failed via {proxy or 'direct'}: {data.get('message', data)}")
    return {
        "ip": data.get("query"),
        "country": data.get("country"),
        "country_code": data.get("countryCode"),
        "region": data.get("regionName"),
        "city": data.get("city"),
        "timezone": data.get("timezone"),
        "isp": data.get("isp"),
        "org": data.get("org"),
        "as": data.get("as"),
        "proxy": proxy.safe() if proxy else "direct",
    }


class ProxyPool:
    """Sticky-by-key selection with health tracking."""

    def __init__(self, proxies: Iterable[Proxy], strategy: str = "per_account",
                 *, probe_on_pick: bool = False):
        self.proxies = list(proxies)
        self.strategy = strategy if strategy in {"per_account", "per_attempt", "round_robin"} else "per_account"
        self.probe_on_pick = probe_on_pick
        self._lock = threading.Lock()
        self._counter = 0
        self._health: dict[str, dict[str, Any]] = {}
        self._geo: dict[str, dict[str, Any]] = {}

    def __bool__(self) -> bool:
        return bool(self.proxies)

    def __len__(self) -> int:
        return len(self.proxies)

    def _healthy(self, proxy: Proxy) -> bool:
        state = self._health.get(proxy.safe())
        return not state or state.get("ok", True)

    def pick(self, key: str) -> Proxy | None:
        if not self.proxies:
            return None
        with self._lock:
            if self.strategy == "per_attempt":
                candidates = [p for p in self.proxies if self._healthy(p)] or self.proxies
                return random.choice(candidates)
            if self.strategy == "round_robin":
                self._counter += 1
                start = self._counter % len(self.proxies)
            else:
                start = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(self.proxies)
            for offset in range(len(self.proxies)):
                proxy = self.proxies[(start + offset) % len(self.proxies)]
                if self._healthy(proxy):
                    return proxy
            return self.proxies[start]

    def probe(self, proxy: Proxy | None, *, force: bool = False) -> dict[str, Any]:
        if proxy is None:
            try:
                return probe(None)
            except ProxyError:
                return {"proxy": "direct"}
        key = proxy.safe()
        with self._lock:
            cached = self._geo.get(key)
        if cached and not force:
            return cached
        try:
            geo = probe(proxy)
            with self._lock:
                self._geo[key] = geo
                self._health[key] = {"ok": True, "checked_at": int(time.time())}
            return geo
        except ProxyError:
            with self._lock:
                self._health[key] = {"ok": False, "checked_at": int(time.time())}
            raise

    def mark_bad(self, proxy: Proxy, reason: str = "") -> None:
        with self._lock:
            self._health[proxy.safe()] = {"ok": False, "reason": redact(reason),
                                          "checked_at": int(time.time())}

    def mark_good(self, proxy: Proxy) -> None:
        with self._lock:
            self._health[proxy.safe()] = {"ok": True, "checked_at": int(time.time())}

    def health(self) -> dict[str, Any]:
        return dict(self._health)


def pool_from_settings(settings: Any, override: str | None = None) -> ProxyPool:
    strategy = str(settings.get("proxy.strategy", "per_account"))
    if override:
        values = [override]
    else:
        default = settings.get("proxy.default", "direct")
        pool = settings.get("proxy.pool", []) or []
        values = list(pool)
        if default and default != "direct":
            values.insert(0, default)
    return ProxyPool(parse_pool(values), strategy)
