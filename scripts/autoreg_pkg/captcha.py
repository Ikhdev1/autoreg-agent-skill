"""CAPTCHA solving and token injection.

Supported:
- 2captcha (Turnstile / hCaptcha / reCAPTCHA v2)
- YesCaptcha (same task types)
- local solver with a Theyka-compatible createTask/getTask API
- static token (tests) and manual (headed browser)

Token injection is deliberately conservative: fill the hidden response field,
dispatch input/change, expose `window.__autoreg_captcha_token`, and call the
widget's `data-callback` if it names a global function.  A solver returning an
HTTP-only token cannot pass a *managed challenge* (Cloudflare's interactive
page), so that case falls back to a real browser profile.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Settings
from .util import CaptchaError, ConfigError, redact


@dataclass
class CaptchaSpec:
    type: str = "turnstile"
    sitekey: str | None = None
    provider: str = "none"
    token: str | None = None
    callback: str | None = None
    action: str | None = None
    cdata: str | None = None
    timeout: float = 180.0
    interval: float = 5.0
    page_url: str | None = None
    base: str | None = None
    mode: str | None = None
    proxy: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def spec_from(settings: Settings, spec: Any, *, page_url: str | None = None) -> CaptchaSpec:
    if spec is None:
        spec = {}
    if isinstance(spec, str):
        spec = {"provider": spec}
    if not isinstance(spec, dict):
        raise ConfigError(f"captcha must be a mapping or name, got {type(spec).__name__}")
    merged = dict(spec)
    provider_name = str(merged.get("provider") or settings.get("captcha.default") or "none")
    provider_cfg = (settings.provider("captcha", provider_name, merged)
                    if provider_name not in {"none", "static", "manual"} else {})
    base = merged.get("base") or provider_cfg.get("base")
    return CaptchaSpec(
        type=str(merged.get("captcha_type") or merged.get("kind") or "turnstile"),
        sitekey=merged.get("sitekey") or merged.get("websiteKey"),
        provider=provider_name,
        token=merged.get("token"),
        callback=merged.get("callback"),
        action=merged.get("action"),
        cdata=merged.get("cdata") or merged.get("data"),
        timeout=float(merged.get("timeout") or settings.timeout("captcha", 180)),
        interval=float(merged.get("interval") or 5.0),
        page_url=merged.get("page_url") or page_url,
        base=base,
        mode=merged.get("mode"),
        extra=provider_cfg,
    )


def detect(page: Any) -> dict[str, Any]:
    return page.evaluate("""(() => {
      const pick = (sel) => document.querySelector(sel);
      const el = pick('.cf-turnstile,[data-sitekey]') || pick('.h-captcha') || pick('.g-recaptcha');
      let type = null, sitekey = null;
      if (el) {
        const cls = (el.className || '').toString();
        type = cls.includes('h-captcha') ? 'hcaptcha'
             : cls.includes('g-recaptcha') ? 'recaptcha' : 'turnstile';
        sitekey = el.getAttribute('data-sitekey') || el.getAttribute('data-hcaptcha-sitekey');
      }
      const frames = [...document.querySelectorAll('iframe')].map(f => f.src || '');
      for (const src of frames) {
        try {
          const u = new URL(src);
          if (!type) {
            if (u.hostname.includes('hcaptcha')) type = 'hcaptcha';
            else if (u.hostname.includes('recaptcha')) type = 'recaptcha';
            else if (u.hostname.includes('challenges.cloudflare.com')) type = 'turnstile';
          }
          sitekey = sitekey || u.searchParams.get('sitekey') || u.searchParams.get('k');
        } catch (e) {}
      }
      return {type, sitekey, iframes: frames};
    })()""")


class _Solver:
    def solve(self, spec: CaptchaSpec) -> str:
        raise NotImplementedError


class TwoCaptcha(_Solver):
    def __init__(self, spec: CaptchaSpec):
        base = (spec.base or spec.extra.get("base") or "https://2captcha.com").rstrip("/")
        self.base = base
        self.key = spec.extra.get("key") or spec.extra.get("api_key")
        if not self.key:
            raise ConfigError("2captcha provider requires key")
        self.timeout = spec.timeout
        self.interval = spec.interval

    def _method(self, kind: str) -> str:
        return {"turnstile": "turnstile", "hcaptcha": "hcaptcha",
                "recaptcha": "userrecaptcha"}.get(kind, kind)

    def solve(self, spec: CaptchaSpec) -> str:
        payload = {
            "key": self.key, "method": self._method(spec.type),
            "pageurl": spec.page_url, "json": 1,
        }
        if spec.type == "recaptcha":
            payload["googlekey"] = spec.sitekey
        else:
            payload["sitekey"] = spec.sitekey
        if spec.action:
            payload["action"] = spec.action
        if spec.cdata:
            payload["data"] = spec.cdata
        if spec.proxy:
            payload.update(_proxy_fields(spec.proxy, two_captcha=True))
        response = requests.post(f"{self.base}/in.php", data=payload, timeout=30)
        data = _json(response, "2captcha in.php")
        if int(data.get("status", 0)) != 1:
            raise CaptchaError(f"2captcha rejected task: {redact(data.get('request') or data)}")
        task_id = str(data["request"])
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            response = requests.get(
                f"{self.base}/res.php",
                params={"key": self.key, "action": "get", "id": task_id, "json": 1},
                timeout=30,
            )
            data = _json(response, "2captcha res.php")
            if int(data.get("status", 0)) == 1:
                return str(data["request"])
            if str(data.get("request", "")).upper() != "CAPCHA_NOT_READY":
                raise CaptchaError(f"2captcha solve failed: {redact(data.get('request'))}")
        raise CaptchaError(f"2captcha timed out after {self.timeout:.0f}s")


class YesCaptcha(_Solver):
    def __init__(self, spec: CaptchaSpec):
        self.base = (spec.base or spec.extra.get("base") or "https://api.yescaptcha.com").rstrip("/")
        self.key = spec.extra.get("key") or spec.extra.get("clientKey") or spec.extra.get("api_key")
        if not self.key:
            raise ConfigError("yescaptcha provider requires key")
        self.timeout = spec.timeout
        self.interval = spec.interval

    def _type(self, kind: str) -> str:
        return {"turnstile": "TurnstileTaskProxyless",
                "hcaptcha": "HCaptchaTaskProxyless",
                "recaptcha": "NoCaptchaTaskProxyless"}.get(kind, kind)

    def solve(self, spec: CaptchaSpec) -> str:
        task = {"type": self._type(spec.type), "websiteURL": spec.page_url,
                "websiteKey": spec.sitekey}
        if spec.action:
            task["action"] = spec.action
        if spec.cdata:
            task["cdata"] = spec.cdata
        if spec.proxy:
            task.update(_proxy_fields(spec.proxy, yescaptcha=True))
        response = requests.post(f"{self.base}/createTask",
                                 json={"clientKey": self.key, "task": task}, timeout=30)
        data = _json(response, "yescaptcha createTask")
        if int(data.get("errorId", 0)) != 0:
            raise CaptchaError(f"yescaptcha rejected task: {redact(data)}")
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaError(f"yescaptcha returned no taskId: {redact(data)}")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            response = requests.post(f"{self.base}/getTaskResult",
                                     json={"clientKey": self.key, "taskId": task_id}, timeout=30)
            data = _json(response, "yescaptcha getTaskResult")
            if int(data.get("errorId", 0)) != 0:
                raise CaptchaError(f"yescaptcha solve failed: {redact(data)}")
            if data.get("status") == "ready":
                token = (data.get("solution") or {}).get("token")
                if not token:
                    raise CaptchaError(f"yescaptcha returned no token: {redact(data)}")
                return str(token)
            if data.get("status") != "processing":
                raise CaptchaError(f"yescaptcha unexpected status: {redact(data)}")
        raise CaptchaError(f"yescaptcha timed out after {self.timeout:.0f}s")


class LocalSolver(_Solver):
    """Theyka-compatible solver, or a 2captcha-compatible one with mode=2captcha."""

    def __init__(self, spec: CaptchaSpec):
        self.base = (spec.base or spec.extra.get("base") or "http://127.0.0.1:8889").rstrip("/")
        self.create_path = spec.extra.get("create_path", "/createTask")
        self.get_path = spec.extra.get("get_path", "/getTask")
        self.timeout = spec.timeout
        self.interval = spec.interval
        self.mode = spec.mode or spec.extra.get("mode") or "theyka"

    def solve(self, spec: CaptchaSpec) -> str:
        if self.mode == "2captcha":
            return TwoCaptcha(spec).solve(spec)
        task_type = {"turnstile": "TurnstileTaskProxyless",
                     "hcaptcha": "HCaptchaTaskProxyless",
                     "recaptcha": "NoCaptchaTaskProxyless"}.get(spec.type, spec.type)
        body = {"type": task_type, "websiteURL": spec.page_url,
                "websiteKey": spec.sitekey}
        if spec.proxy:
            body["proxy"] = _proxy_url(spec.proxy)
        response = requests.post(self.base + self.create_path, json=body, timeout=30)
        data = _json(response, "local solver createTask")
        task_id = data.get("taskId") or data.get("request")
        if not task_id:
            raise CaptchaError(f"local solver returned no taskId: {redact(data)}")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.interval)
            response = requests.get(self.base + self.get_path,
                                    params={"taskId": task_id}, timeout=30)
            data = _json(response, "local solver getTask")
            token = (data.get("solution") or {}).get("token") if isinstance(data.get("solution"), dict) else data.get("token")
            if token:
                return str(token)
            status = str(data.get("status") or "").lower()
            if status in {"ready", "success", "ok"} and token:
                return str(token)
            if status not in {"processing", "pending", "running", "queued", ""}:
                raise CaptchaError(f"local solver failed: {redact(data)}")
        raise CaptchaError(f"local solver timed out after {self.timeout:.0f}s")


def _proxy_url(proxy: Any) -> str:
    return getattr(proxy, "authenticated_url", str(proxy))


def _proxy_fields(proxy: Any, *, two_captcha: bool = False,
                  yescaptcha: bool = False) -> dict[str, Any]:
    scheme = str(getattr(proxy, "scheme", "http")).lower()
    socks = scheme.startswith("socks")
    host = str(getattr(proxy, "host", ""))
    port = int(getattr(proxy, "port", 0))
    username = getattr(proxy, "username", None)
    password = getattr(proxy, "password", None)
    if two_captcha:
        fields: dict[str, Any] = {
            "proxy": f"{host}:{port}",
            "proxytype": "SOCKS5" if socks else "HTTP",
        }
        if username:
            fields["proxy_user"] = username
        if password:
            fields["proxy_password"] = password
        return fields
    if yescaptcha:
        fields = {
            "proxyType": "socks5" if socks else "http",
            "proxyAddress": host,
            "proxyPort": port,
        }
        if username:
            fields["proxyLogin"] = username
        if password:
            fields["proxyPassword"] = password
        return fields
    return {}


def _json(response: requests.Response, where: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise CaptchaError(f"{where}: HTTP {response.status_code} {redact(response.text[:300])}")
    try:
        return response.json()
    except ValueError as exc:
        raise CaptchaError(f"{where}: non-JSON response {redact(response.text[:200])}") from exc


def solve(settings: Settings, spec: CaptchaSpec, page: Any = None,
          *, proxy: Any = None) -> str | None:
    if spec.provider in {"none", "off", "false"}:
        return None
    if spec.provider == "empty":
        # Some New-API forks accept turnstile="" — try this before paying a solver.
        return ""
    if spec.provider == "static":
        if not spec.token:
            raise CaptchaError("captcha provider static requires a token")
        return spec.token
    if spec.provider == "manual":
        return None
    if spec.provider not in {"2captcha", "yescaptcha", "local"}:
        raise CaptchaError(f"Unknown captcha provider {spec.provider!r}")

    if page is not None and (not spec.sitekey or not spec.type):
        found = detect(page)
        spec.type = spec.type or found.get("type") or "turnstile"
        spec.sitekey = spec.sitekey or found.get("sitekey")
    if not spec.sitekey:
        raise CaptchaError("No captcha sitekey found; set captcha.sitekey explicitly")
    if not spec.page_url:
        raise CaptchaError("captcha.page_url is required")
    if proxy is not None:
        spec.proxy = proxy

    if spec.provider == "2captcha":
        return TwoCaptcha(spec).solve(spec)
    if spec.provider == "yescaptcha":
        return YesCaptcha(spec).solve(spec)
    return LocalSolver(spec).solve(spec)


INJECT_JS = """(args) => {
  const {kind, token, callback} = args;
  const names = kind === 'hcaptcha'
    ? ['h-captcha-response', 'g-recaptcha-response']
    : kind === 'recaptcha'
      ? ['g-recaptcha-response', 'h-captcha-response']
      : ['cf-turnstile-response', 'g-recaptcha-response'];
  const set = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    el.value = token;
    el.innerHTML = token;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };
  let ok = false;
  for (const name of names) {
    ok = set(`input[name="${name}"]`) || set(`textarea[name="${name}"]`) || ok;
  }
  if (!ok && names.length) {
    const input = document.createElement('input');
    input.type = 'hidden'; input.name = names[0]; input.value = token;
    document.body.appendChild(input);
    ok = true;
  }
  window.__autoreg_captcha_token = token;
  const widget = document.querySelector('.cf-turnstile,.h-captcha,.g-recaptcha,[data-sitekey]');
  const cb = callback || (widget && widget.getAttribute('data-callback'));
  if (cb) {
    try {
      const fn = cb.includes('.') ? cb.split('.').reduce((o, k) => o && o[k], window) : window[cb];
      if (typeof fn === 'function') fn(token);
    } catch (e) {}
  }
  return ok;
}"""


def inject(page: Any, token: str, kind: str = "turnstile",
           callback: str | None = None) -> bool:
    return bool(page.evaluate(INJECT_JS, {"kind": kind, "token": token, "callback": callback}))


def wait_for_token(page: Any, kind: str = "turnstile", timeout: float = 60.0) -> str:
    names = {"hcaptcha": ["h-captcha-response"],
             "recaptcha": ["g-recaptcha-response"],
             "turnstile": ["cf-turnstile-response"]}.get(kind, ["cf-turnstile-response"])
    selector = ",".join(f'input[name="{n}"],textarea[name="{n}"]' for n in names)
    try:
        page.wait_for_function(
            """(selector) => {
              if (window.__autoreg_captcha_token) return true;
              const nodes = [...document.querySelectorAll(selector)];
              return nodes.some(n => n.value && n.value.length > 10);
            }""",
            arg=selector, timeout=int(timeout * 1000),
        )
    except Exception as exc:
        raise CaptchaError(f"captcha token did not appear in {timeout:.0f}s") from exc
    token = page.evaluate("""(selector) => {
      if (window.__autoreg_captcha_token) return window.__autoreg_captcha_token;
      const n = [...document.querySelectorAll(selector)].find(x => x.value);
      return n ? n.value : null;
    }""", selector)
    if not token:
        raise CaptchaError("captcha token element is empty")
    return str(token)
