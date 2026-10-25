#!/usr/bin/env python3
"""Local signup target used by the pack's tests.

Endpoints:
  POST /api/mailboxes           -> {address}
  GET  /api/inbox?email=        -> {messages:[...]}   (mock mail provider)
  POST /api/send-code           -> {ok:true}           (creates a mail message)
  POST /api/verify              -> {ok:true, redirect}
  GET  /welcome                 -> session-gated page
  GET  /fp                      -> browser fingerprint page
  POST /api/fp, GET /api/fp     -> last fingerprint payload
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

STATE_LOCK = threading.RLock()
MAILBOXES: dict[str, list[dict[str, Any]]] = {}
MAIL_TOKENS: dict[str, str] = {}   # session token -> address (Voidash-style)
CODES: dict[str, tuple[str, float]] = {}
ACCOUNTS: dict[str, dict[str, Any]] = {}
SESSIONS: dict[str, str] = {}
FINGERPRINT: dict[str, Any] = {}
GRID_CLICKS: list[int] = []
SEQ = 0


def _next_id(prefix: str) -> str:
    global SEQ
    with STATE_LOCK:
        SEQ += 1
        return f"{prefix}{SEQ}"


SIGNUP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Mock signup</title>
<style>
 body{font:16px/1.4 system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px}
 label{display:block;margin:14px 0 4px;font-weight:600}
 input{width:100%;padding:9px;box-sizing:border-box}
 button{margin-top:14px;padding:10px 18px;cursor:pointer}
 #error{color:#b00020;margin-top:10px;white-space:pre-wrap}
 #code-sent{color:#087f23;margin-top:8px}
 .cf-turnstile{border:1px dashed #999;padding:10px;margin-top:14px;color:#555}
</style></head><body>
<h1>Create account</h1>
<form id="signup" novalidate>
  <label for="name">Full name</label><input id="name" name="name" autocomplete="name">
  <label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" required>
  <button id="send-code" type="button">Send code</button>
  <div id="code-sent" hidden>Code sent</div>
  <label for="code">Verification code</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code">
  <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="new-password" required>
  <div id="cf-turnstile" class="cf-turnstile" data-sitekey="mock-sitekey" data-callback="onTurnstile">Mock Turnstile widget</div>
  <input type="hidden" name="cf-turnstile-response" id="cf-turnstile-response">
  <button id="create" type="submit">Create account</button>
  <div id="error"></div>
</form>
<script>
function onTurnstile(token) {
  document.getElementById('cf-turnstile-response').value = token;
  window.__mockTurnstile = token;
}
document.getElementById('send-code').addEventListener('click', async () => {
  const email = document.getElementById('email').value.trim();
  const err = document.getElementById('error');
  err.textContent = '';
  if (!email) { err.textContent = 'email required'; return; }
  const r = await fetch('/api/send-code', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email})});
  const d = await r.json();
  if (!d.ok) { err.textContent = d.error || 'send failed'; return; }
  document.getElementById('code-sent').hidden = false;
});
document.getElementById('signup').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('error');
  err.textContent = '';
  const payload = {
    name: document.getElementById('name').value,
    email: document.getElementById('email').value.trim(),
    password: document.getElementById('password').value,
    code: document.getElementById('code').value.trim(),
    token: document.getElementById('cf-turnstile-response').value,
  };
  const r = await fetch('/api/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const d = await r.json();
  if (d.ok) { location.href = d.redirect; }
  else { err.textContent = d.error || 'registration failed'; }
});
</script></body></html>"""

FP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Mock fingerprint</title></head>
<body style="font:14px monospace;padding:20px">
<h3>Browser fingerprint</h3><pre id="fp">measuring...</pre>
<script>
const gl = document.createElement('canvas').getContext('webgl');
const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
const data = {
  ua: navigator.userAgent, platform: navigator.platform,
  language: navigator.language, languages: navigator.languages,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory ?? null,
  webdriver: navigator.webdriver,
  screen: {width:screen.width,height:screen.height,availWidth:screen.availWidth,
           availHeight:screen.availHeight,colorDepth:screen.colorDepth,pixelDepth:screen.pixelDepth},
  devicePixelRatio: window.devicePixelRatio,
  inner: {width: innerWidth, height: innerHeight},
  webgl: gl ? {vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
               renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)} : null,
};
document.getElementById('fp').textContent = JSON.stringify(data, null, 2);
fetch('/api/fp', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
</script></body></html>"""

GRID_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Mock grid</title>
<style>
#grid{display:grid;grid-template-columns:repeat(3,90px);gap:6px;margin:20px}
.tile{height:80px;border:1px solid #888;background:#f5f5f5;cursor:pointer;display:flex;align-items:center;justify-content:center;font:14px sans-serif}
</style></head><body>
<h3>Click the described tiles</h3>
<div id="grid">
""" + "\n".join(
    f'<div class="tile" data-idx="{i}" onclick="fetch(\'/api/grid-click\',{{method:\'POST\',headers:{{\'Content-Type\':\'application/json\'}},body:JSON.stringify({{idx:{i}}})}})">{i}</div>'
    for i in range(1, 10)
) + """
</div>
<div id="done"></div>
<script>
window.__gridClicks = [];
</script>
</body></html>"""


WELCOME_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Welcome</title></head>
<body style="font:16px system-ui;padding:40px">
<h1>Welcome, {email}</h1>
<p>Your account was created.</p>
<p><a href="/logout" id="logout">Sign out</a></p>
</body></html>"""


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any,
                   headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str, status: int = 200,
                   cookies: list[str] | None = None) -> None:
    body = html.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for cookie in cookies or []:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _cookie(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoregMock/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        if path == "/":
            _html_response(self, "<h1>Mock target</h1><p><a href='/signup'>Sign up</a> | "
                                 "<a href='/fp'>Fingerprint</a></p>")
            return
        if path == "/signup":
            _html_response(self, SIGNUP_HTML)
            return
        if path == "/fp":
            _html_response(self, FP_HTML)
            return
        if path == "/welcome":
            token = _cookie(self, "session")
            email = SESSIONS.get(token or "")
            if not email:
                _html_response(self, "<p>Not signed in.</p><p><a href='/signup'>Sign up</a></p>",
                               status=401)
                return
            _html_response(self, WELCOME_HTML.format(email=email))
            return
        if path == "/logout":
            token = _cookie(self, "session")
            if token:
                SESSIONS.pop(token, None)
            _html_response(self, "<p>Signed out.</p>", cookies=["session=; Path=/; Max-Age=0"])
            return
        if path == "/api/inbox":
            email = (parse_qs(parsed.query).get("email") or [""])[0]
            with STATE_LOCK:
                messages = list(MAILBOXES.get(email, []))
            _json_response(self, 200, {"messages": messages})
            return
        if path == "/api/voidash/messages":
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            with STATE_LOCK:
                email = MAIL_TOKENS.get(token)
                messages = list(MAILBOXES.get(email, [])) if email else None
            if messages is None:
                _json_response(self, 401, {"error": "unauthorized"})
                return
            _json_response(self, 200, {"messages": messages})
            return
        if path == "/api/fp":
            _json_response(self, 200, FINGERPRINT)
            return
        if path == "/grid":
            _html_response(self, GRID_HTML)
            return
        if path == "/api/grid":
            _json_response(self, 200, {"clicks": list(GRID_CLICKS)})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/voidash/inboxes":
            address = f"v{secrets.token_hex(5)}@mock.test"
            session_key = "sk_" + secrets.token_hex(12)
            now = time.time()
            with STATE_LOCK:
                MAIL_TOKENS[session_key] = address
                MAILBOXES[address] = [{
                    "id": f"old-{address}",
                    "from_address": "no-reply@mock.test",
                    "to_address": address,
                    "subject": "Your verification code",
                    "body": "Your code is 111111. It expires in 10 minutes.",
                    "received_at": int(now * 1000),
                }]
            _json_response(self, 200, {"address": address, "session_key": session_key})
            return
        if path == "/api/mailboxes":
            email = f"u{secrets.token_hex(5)}@mock.test"
            with STATE_LOCK:
                MAILBOXES.setdefault(email, [])
            _json_response(self, 200, {"address": email, "id": email})
            return
        if path == "/api/send-code":
            data = _read_json(self)
            email = str(data.get("email") or "").strip().lower()
            if not email:
                _json_response(self, 400, {"ok": False, "error": "email required"})
                return
            code = f"{secrets.randbelow(1_000_000):06d}"
            now = time.time()
            with STATE_LOCK:
                CODES[email] = (code, now)
                MAILBOXES.setdefault(email, []).append({
                    "id": _next_id("m"),
                    "from_address": "no-reply@mock.test",
                    "to_address": email,
                    "subject": "Your verification code",
                    "body": f"Your code is {code}. It expires in 10 minutes.",
                    "received_at": int(now * 1000),
                })
            _json_response(self, 200, {"ok": True})
            return
        if path == "/api/verify":
            data = _read_json(self)
            email = str(data.get("email") or "").strip().lower()
            password = str(data.get("password") or "")
            code = str(data.get("code") or "").strip()
            token = str(data.get("token") or "").strip()
            if token != "mock-token":
                _json_response(self, 400, {"ok": False, "error": "captcha required"})
                return
            with STATE_LOCK:
                expected = CODES.get(email)
            if not expected or expected[0] != code or time.time() - expected[1] > 600:
                _json_response(self, 400, {"ok": False, "error": "invalid code"})
                return
            if len(password) < 8:
                _json_response(self, 400, {"ok": False, "error": "password too short"})
                return
            session = "sess_" + secrets.token_hex(12)
            with STATE_LOCK:
                ACCOUNTS[email] = {"password": password, "token": session}
                SESSIONS[session] = email
            _json_response(self, 200, {"ok": True, "redirect": "/welcome"},
                           headers={"Set-Cookie": (
                               f"session={session}; Path=/; Max-Age=86400; "
                               "HttpOnly; SameSite=Lax")})
            return
        if path == "/api/fp":
            global FINGERPRINT
            FINGERPRINT = _read_json(self)
            _json_response(self, 200, {"ok": True})
            return
        if path == "/api/rpc":
            data = _read_json(self)
            _json_response(self, 200, {
                "result": {"id": data.get("id"), "token": "rpc-token"},
                "echo": data.get("query"),
            })
            return
        if path == "/api/grid-click":
            data = _read_json(self)
            idx = int(data.get("idx") or 0)
            GRID_CLICKS.append(idx)
            _json_response(self, 200, {"ok": True, "idx": idx})
            return
        if path == "/api/v1/chat/completions":
            # Deterministic mock vision: tile list for the grid prompt,
            # a point JSON for click_by_vision prompts.
            payload = _read_json(self)
            content = ""
            try:
                content = payload["messages"][0]["content"][0]["text"]
            except Exception:
                pass
            if "Return ONLY JSON" in content or "click point" in content:
                answer = json.dumps({"x": 45, "y": 40})
            else:
                answer = "1,3,7"
            _json_response(self, 200, {"choices": [{"message": {"content": answer}}]})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})


def start(host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{server.server_address[1]}"
    return server, thread, base_url


def stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server, _, base_url = start(args.host, args.port)
    print(f"mock site: {base_url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
