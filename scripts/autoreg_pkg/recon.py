"""Signup-flow recon: capture network, forms and bot protection, then emit a
runnable adapter skeleton.

The goal is to remove the blank-page problem for an agent: one command yields
the selectors, candidate endpoints, captcha type and a YAML starting point.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .browser import BrowserSession, resolve_engine
from .config import Settings
from .identity import host_os, make_profile
from .proxy import Proxy
from .util import (dump_yaml, now_iso, redact, short_id, write_json)

STATIC_EXT = re.compile(
    r"\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|webp|avif|mp4|webm)(?:$|\?)",
    re.I,
)

PROTECTION_SIGNATURES: dict[str, list[str]] = {
    "cloudflare_turnstile": ["challenges.cloudflare.com/turnstile", "cf-turnstile", "turnstile"],
    "cloudflare_challenge": ["cdn-cgi/challenge-platform", "__cf_chl", "cf_chl_opt"],
    "hcaptcha": ["hcaptcha.com", "h-captcha"],
    "recaptcha": ["recaptcha.net", "google.com/recaptcha", "g-recaptcha"],
    "arkose": ["arkoselabs", "funcaptcha", "arkose"],
    "datadome": ["datadome", "dd_cookie"],
    "perimeterx": ["perimeterx", "px-cdn", "_px"],
    "kasada": ["kasada", "kpsdk"],
    "akamai": ["akamai", "bm-verify", "_abck"],
    "imperva": ["incapsula", "imperva", "_incap"],
}

ENDPOINT_HINTS = re.compile(
    r"(signup|sign-up|register|registration|create.?account|auth|login|"
    r"verify|verification|otp|code|email|user|account|onboard|invite)",
    re.I,
)

FORM_JS = """(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return !!(r.width && r.height) && getComputedStyle(el).visibility !== 'hidden';
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.type || null,
    name: el.name || null,
    id: el.id || null,
    placeholder: el.placeholder || null,
    ariaLabel: el.getAttribute('aria-label'),
    autocomplete: el.getAttribute('autocomplete'),
    required: !!el.required,
    valueLen: (el.value || '').length,
    visible: visible(el),
    text: (el.innerText || '').trim().slice(0, 80),
    href: el.href || null,
  });
  const forms = [...document.forms].map(f => ({
    action: f.action, method: (f.method || 'get').toUpperCase(), id: f.id || null,
    inputs: [...f.elements].map(describe)
  }));
  const controls = [...document.querySelectorAll('input,textarea,select,button,[role=button],a')]
    .slice(0, 250).map(describe);
  const iframes = [...document.querySelectorAll('iframe')].map(f => f.src || '');
  const scripts = [...document.scripts].map(s => s.src).filter(Boolean);
  return {
    title: document.title, url: location.href, forms, controls, iframes, scripts,
    bodyText: (document.body ? document.body.innerText : '').slice(0, 6000),
    cookies: document.cookie,
  };
})()"""


@dataclass
class ReconResult:
    url: str
    out_dir: Path
    report: Path
    skeleton: Path
    summary: dict[str, Any]


def recon(settings: Settings, url: str, *, engine: str = "auto",
          headed: bool = False, settle: float = 3.0,
          out_dir: Path | None = None, proxy: Proxy | None = None) -> ReconResult:
    parsed = urlparse(url)
    host = parsed.hostname or "site"
    if parsed.port:
        host = f"{host}_{parsed.port}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir) if out_dir else settings.paths.recon / host / stamp
    out.mkdir(parents=True, exist_ok=True)

    chosen = resolve_engine(engine, stealth=False)
    profile = make_profile(f"recon:{host}:{short_id()}", os_name=host_os(), engine=chosen)
    profile_dir = out / "profile"
    har_path = out / "network.har" if chosen != "camoufox" else None

    requests_log: list[dict[str, Any]] = []
    responses: dict[str, dict[str, Any]] = {}
    console: list[dict[str, Any]] = []

    def on_request(req: Any) -> None:
        if len(requests_log) >= 800:
            return
        try:
            post = req.post_data
        except Exception:
            post = None
        headers = {}
        try:
            for key, value in req.headers.items():
                headers[key] = "***" if key.lower() in {"authorization", "cookie", "set-cookie"} else value
        except Exception:
            headers = {}
        requests_log.append({
            "at": now_iso(), "method": req.method, "url": req.url,
            "resource_type": getattr(req, "resource_type", None),
            "post_data": redact(post)[:2000] if post else None,
            "headers": headers,
        })

    def on_response(resp: Any) -> None:
        try:
            req = resp.request
            key = f"{req.method} {resp.url}"
            responses[key] = {
                "status": resp.status, "url": resp.url, "method": req.method,
                "content_type": (resp.headers or {}).get("content-type"),
            }
        except Exception:
            pass

    def on_console(msg: Any) -> None:
        try:
            if len(console) < 100:
                console.append({"type": msg.type, "text": msg.text[:500]})
        except Exception:
            pass

    with BrowserSession(profile=profile, profile_dir=profile_dir, proxy=proxy,
                        engine=chosen, headed=headed, har_path=har_path,
                        humanize=False) as session:
        page = session.page
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("console", on_console)
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(int(settle * 1000))
        try:
            dom = page.evaluate(FORM_JS)
        except Exception as exc:
            dom = {"error": redact(exc), "forms": [], "controls": [], "iframes": [],
                   "scripts": [], "bodyText": "", "cookies": "", "url": page.url,
                   "title": ""}
        final_url = page.url
        page.screenshot(path=str(out / "page.png"), full_page=True)
        html = session.content()
        (out / "page.html").write_text(html, encoding="utf-8")
        dom["html"] = html

    # Persist raw captures.
    with open(out / "requests.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for item in requests_log:
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    with open(out / "responses.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for item in responses.values():
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    write_json(out / "forms.json", dom)
    write_json(out / "console.json", console)

    protections = _detect_protection(requests_log, dom)
    candidates = _candidate_endpoints(requests_log, responses)
    summary = {
        "url": url, "final_url": final_url, "title": dom.get("title"),
        "engine": chosen, "protections": protections,
        "forms": dom.get("forms") or [], "controls": dom.get("controls") or [],
        "endpoints": candidates, "cookies": _cookie_names(dom.get("cookies") or ""),
        "console": console, "har": str(har_path) if har_path and har_path.is_file() else None,
        "captured_at": now_iso(),
    }
    write_json(out / "summary.json", summary)

    skeleton = _build_skeleton(url, final_url, summary)
    skeleton_path = out / "skeleton.yaml"
    skeleton_path.write_text(
        "# Generated by `autoreg recon`. TODO markers are guesses — verify every\n"
        "# selector and endpoint against summary.json / report.md before a real run.\n"
        + dump_yaml(skeleton), encoding="utf-8")
    report_path = out / "report.md"
    report_path.write_text(_report(summary, out), encoding="utf-8")
    return ReconResult(url=url, out_dir=out, report=report_path,
                       skeleton=skeleton_path, summary=summary)


def _detect_protection(requests_log: list[dict[str, Any]], dom: dict[str, Any]) -> list[str]:
    haystack = " ".join(
        [item.get("url") or "" for item in requests_log]
        + list(dom.get("scripts") or [])
        + list(dom.get("iframes") or [])
        + [dom.get("bodyText") or "", dom.get("html") or ""]
    ).lower()
    found = []
    for name, needles in PROTECTION_SIGNATURES.items():
        if any(needle.lower() in haystack for needle in needles):
            found.append(name)
    return found


def _candidate_endpoints(requests_log: list[dict[str, Any]],
                         responses: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in requests_log:
        url = item.get("url") or ""
        if not url or STATIC_EXT.search(url):
            continue
        rtype = (item.get("resource_type") or "").lower()
        if rtype and rtype not in {"xhr", "fetch", "document"}:
            continue
        parsed = urlparse(url)
        key = f"{item.get('method')} {parsed.path}"
        if key in seen:
            continue
        score = 3 if ENDPOINT_HINTS.search(parsed.path + " " + (parsed.query or "")) else 0
        if item.get("post_data"):
            score += 2
        if rtype in {"xhr", "fetch"}:
            score += 1
        resp = responses.get(f"{item.get('method')} {url}") or {}
        seen[key] = {
            "method": item.get("method"), "path": parsed.path,
            "query": parsed.query, "url": url,
            "resource_type": rtype, "post_data": item.get("post_data"),
            "status": resp.get("status"), "score": score,
        }
    return sorted(seen.values(), key=lambda x: (-x["score"], x["path"]))[:40]


def _cookie_names(raw: str) -> list[str]:
    names = []
    for part in (raw or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return sorted(set(names))


def _selector_for(control: dict[str, Any]) -> str | None:
    if control.get("id"):
        return f'#{control["id"]}'
    if control.get("name"):
        return f'{control["tag"]}[name="{control["name"]}"]'
    if control.get("placeholder"):
        return f'{control["tag"]}[placeholder="{control["placeholder"]}"]'
    return None


def _find_controls(summary: dict[str, Any]) -> dict[str, Any]:
    controls = summary.get("controls") or []
    result: dict[str, Any] = {}
    send_keys = ("send code", "get code", "send otp", "request code",
                 "send verification", "get otp", "send email", "send link",
                 "получить код", "отправить код", "выслать код")
    submit_keys = ("sign up", "signup", "create", "register", "continue", "next",
                   "get started", "join", "start", "go", "apply")
    for control in controls:
        tag = (control.get("tag") or "").lower()
        typ = (control.get("type") or "").lower()
        name = (control.get("name") or "").lower()
        ident = (control.get("id") or "").lower()
        label = (control.get("ariaLabel") or "") + " " + (control.get("text") or "")
        label = label.lower()
        hay = " ".join([name, ident, label, control.get("placeholder") or ""])
        if tag in {"input", "textarea"}:
            if not result.get("email") and (typ == "email" or "email" in hay or "e-mail" in hay):
                result["email"] = control
            if not result.get("password") and typ == "password":
                result["password"] = control
            if not result.get("code") and any(k in hay for k in ("otp", "code", "verification", "pin")):
                result["code"] = control
            if not result.get("name") and any(k in hay for k in ("name", "fullname", "first")):
                result["name"] = control
            if not result.get("phone") and (typ == "tel" or "phone" in hay):
                result["phone"] = control
        if tag in {"button", "a"} or control.get("type") == "submit":
            text = (control.get("text") or "").lower()
            if not result.get("send_code") and any(k in text for k in send_keys):
                result["send_code"] = control
                continue
            if not result.get("submit") and any(k in text for k in submit_keys):
                result["submit"] = control
    return result


def _build_skeleton(url: str, final_url: str, summary: dict[str, Any]) -> dict[str, Any]:
    found = _find_controls(summary)
    steps: list[dict[str, Any]] = [{"goto": final_url or url}]
    if found.get("email"):
        sel = _selector_for(found["email"])
        if sel:
            steps.append({"wait_for": sel})
            steps.append({"fill": {"target": sel, "value": "{{email}}"}})
    if found.get("name"):
        sel = _selector_for(found["name"])
        if sel:
            steps.append({"fill": {"target": sel, "value": "{{full_name}}"}})
    if found.get("password"):
        sel = _selector_for(found["password"])
        if sel:
            steps.append({"fill": {"target": sel, "value": "{{password}}"}})
    send_code = found.get("send_code")
    if send_code:
        sel = _selector_for(send_code) or (f'text={send_code.get("text")}' if send_code.get("text") else None)
        if sel:
            steps.append({"click": sel})
    if found.get("code"):
        sel = _selector_for(found["code"])
        steps.append({"otp": {"target": sel} if sel else {}})
    protections = summary.get("protections") or []
    if any("turnstile" in p for p in protections):
        kind = "turnstile"
    elif any("hcaptcha" in p for p in protections):
        kind = "hcaptcha"
    elif any("recaptcha" in p for p in protections):
        kind = "recaptcha"
    else:
        kind = None
    if kind:
        steps.append({"captcha": {"provider": "none", "type": kind,
                                  "sitekey": "TODO", "page_url": final_url}})
    submit = found.get("submit")
    if submit:
        sel = _selector_for(submit) or (f'text={submit.get("text")}' if submit.get("text") else None)
        if sel:
            steps.append({"click": sel})
    steps.append({"wait_for": {"target": "text=Welcome", "state": "visible",
                               "timeout": 30}})
    steps.append({"screenshot": "success.png"})
    adapter: dict[str, Any] = {
        "name": "TODO-service-name",
        "description": "Generated by recon; verify selectors and add mail settings.",
        "mode": "browser",
        "stealth": bool(protections),
        "engine": "auto",
        "vars": {"signup_url": final_url or url},
        "mail": {
            "provider": "cloudflare_temp_email",
            "code_regex": "(?<!\\d)(\\d{6})(?!\\d)",
            "sender_contains": "",
            "subject_contains": "",
        },
        "captcha": {"provider": "none"},
        "steps": steps,
        "verify": {"url": "{{signup_url}}", "selector": "text=Sign out"},
    }
    if kind:
        adapter["captcha"] = {"provider": "none", "type": kind, "sitekey": "TODO"}
    return adapter


def _report(summary: dict[str, Any], out: Path) -> str:
    lines: list[str] = []
    lines.append(f"# Recon: {summary.get('title') or summary['url']}")
    lines.append("")
    lines.append(f"- URL: {summary['url']}")
    lines.append(f"- Final URL: {summary.get('final_url')}")
    lines.append(f"- Engine: {summary.get('engine')}")
    lines.append(f"- Captured: {summary.get('captured_at')}")
    lines.append(f"- Output: `{out}`")
    lines.append("")
    lines.append("## Bot protection")
    if summary.get("protections"):
        for name in summary["protections"]:
            lines.append(f"- **{name}**")
    else:
        lines.append("- none detected (absence of a script signature is not proof)")
    lines.append("")
    lines.append("## Forms and controls")
    forms = summary.get("forms") or []
    if not forms:
        lines.append("- no `<form>` elements; inspect `controls` in `summary.json`")
    for i, form in enumerate(forms, 1):
        lines.append(f"### Form {i}: `{form.get('method')} {form.get('action')}`")
        lines.append("")
        lines.append("| tag | type | name | id | placeholder | visible |")
        lines.append("|---|---|---|---|---|---|")
        for c in form.get("inputs") or []:
            lines.append(f"| {c.get('tag')} | {c.get('type') or ''} | {c.get('name') or ''} | "
                         f"{c.get('id') or ''} | {c.get('placeholder') or ''} | {c.get('visible')} |")
        lines.append("")
    steps = _find_controls(summary)
    if steps:
        lines.append("### Guessed controls")
        for key, control in steps.items():
            lines.append(f"- {key}: `{_selector_for(control)}` "
                         f"({control.get('tag')} {control.get('type') or ''})")
        lines.append("")
    lines.append("## Candidate endpoints")
    lines.append("")
    lines.append("| method | path | status | score | body |")
    lines.append("|---|---|---|---|---|")
    for e in (summary.get("endpoints") or [])[:20]:
        body = (e.get("post_data") or "").replace("|", "\\|")[:80]
        lines.append(f"| {e['method']} | {e['path']} | {e.get('status') or ''} | {e['score']} | `{body}` |")
    lines.append("")
    lines.append("## Cookies")
    names = summary.get("cookies") or []
    lines.append(", ".join(f"`{n}`" for n in names) if names else "- none visible to JS")
    lines.append("")
    lines.append("## Next steps")
    lines.append("")
    lines.append("1. Read `summary.json` for the full control list and post bodies.")
    lines.append("2. Open `skeleton.yaml`; replace TODO name, selectors, mail provider, "
                 "captcha provider and success condition.")
    lines.append("3. Run once headed: `autoreg run <adapter> --count 1 --headed`.")
    lines.append("4. Inspect `artifacts/<run>/...` on failure, then raise the count.")
    if summary.get("protections"):
        lines.append("")
        lines.append("Protection present: use `--engine camoufox`, a residential proxy "
                     "matched to the identity timezone, and a real solver or manual "
                     "headed pass. A protocol-only flow will not pass Turnstile.")
    return "\n".join(lines) + "\n"
