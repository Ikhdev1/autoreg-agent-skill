"""Temporary-mail and mailbox providers with reliable code extraction.

Lessons baked in from production use:
- identify new mail by message *id*, never by a timestamp comparison between
  two clocks;
- retry 5xx, fail fast on 4xx (a 5xx means "cannot read right now", a 4xx
  means the request is wrong);
- strip HTML before extracting a code and normalise Unicode first;
- on timeout, report what *was* in the inbox, so "mail never arrived" and
  "filter is wrong" are distinguishable.
"""
from __future__ import annotations

import email
import imaplib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

from .config import Settings
from .util import (ConfigError, MailError, MailTimeout, html_to_text, redact)

# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

@dataclass
class Message:
    id: str
    sender: str = ""
    subject: str = ""
    body: str = ""
    received_at: float = 0.0
    to: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return html_to_text(self.body)

    def brief(self) -> str:
        return f"{self.received_at:.0f} {self.sender!r} {self.subject!r}"


@dataclass
class Mailbox:
    address: str
    token: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


MAIL_SESSION_VERSION = 1


class MailSessionError(ConfigError):
    """Stored mailbox access is missing, unreadable or mismatched.

    Deliberately carries no payload: the session holds credentials.
    """


def _detached(value: Any) -> Any:
    """Deep copy through JSON so a stored snapshot can never alias live state."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise MailSessionError("Invalid stored mail session") from exc


@dataclass
class MailSession:
    """Everything needed to read one mailbox again in a later process."""

    provider_config: dict[str, Any]
    mailbox: Mailbox
    ignored_ids: set[str] = field(default_factory=set)
    baseline_armed: bool = False

    @classmethod
    def snapshot(cls, provider: Any, mailbox: Mailbox, *,
                 ignored_ids: Any = (), baseline_armed: bool = False) -> "MailSession":
        config = _detached(dict(getattr(provider, "cfg", None) or {}))
        config.setdefault("type", str(getattr(provider, "name", "") or ""))
        # Outlook rotates its refresh token while reading; keep the newest one.
        rotated = getattr(provider, "refresh_token", None)
        if isinstance(rotated, str) and rotated:
            config["refresh_token"] = rotated
        return cls(
            provider_config=config,
            mailbox=Mailbox(
                address=mailbox.address,
                token=mailbox.token,
                meta=_detached(dict(mailbox.meta or {})),
            ),
            ignored_ids={str(mid) for mid in ignored_ids},
            baseline_armed=bool(baseline_armed),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MAIL_SESSION_VERSION,
            "provider_config": _detached(self.provider_config),
            "mailbox": {
                "address": self.mailbox.address,
                "token": self.mailbox.token,
                "meta": _detached(dict(self.mailbox.meta or {})),
            },
            "ignored_ids": sorted(self.ignored_ids),
            "baseline_armed": bool(self.baseline_armed),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MailSession":
        if not isinstance(data, dict) or data.get("version") != MAIL_SESSION_VERSION:
            raise MailSessionError("Invalid stored mail session")
        config = data.get("provider_config")
        if (not isinstance(config, dict)
                or not isinstance(config.get("type"), str)
                or not config["type"].strip()):
            raise MailSessionError("Invalid stored mail session")
        box = data.get("mailbox")
        if not isinstance(box, dict):
            raise MailSessionError("Invalid stored mail session")
        address, token, meta = box.get("address"), box.get("token"), box.get("meta", {})
        if not isinstance(address, str) or not address.strip():
            raise MailSessionError("Invalid stored mail session")
        if token is not None and not isinstance(token, str):
            raise MailSessionError("Invalid stored mail session")
        if not isinstance(meta, dict):
            raise MailSessionError("Invalid stored mail session")
        ignored = data.get("ignored_ids", [])
        if not isinstance(ignored, list) or any(not isinstance(mid, str) for mid in ignored):
            raise MailSessionError("Invalid stored mail session")
        armed = data.get("baseline_armed", False)
        if not isinstance(armed, bool):
            raise MailSessionError("Invalid stored mail session")
        return cls(
            provider_config=_detached(config),
            mailbox=Mailbox(address=address, token=token, meta=_detached(meta)),
            ignored_ids=set(ignored),
            baseline_armed=armed,
        )


@dataclass
class CodeResult:
    code: str | None
    message: Message
    link: str | None = None
    how: str = ""


# --------------------------------------------------------------------------
# code extraction
# --------------------------------------------------------------------------

_SPACED_6 = re.compile(r"(?<!\d)(\d{3})[ \-](\d{3})(?!\d)")
_ISO_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_DATE = re.compile(r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)")
_TAG_CODE = re.compile(r">\s*([A-Za-z0-9]{4,12})\s*<")
_LABELED = re.compile(
    r"(?i)\b(?:code|otp|verification(?:\s+code)?|verify|passcode|pin|token)\b"
    r"[^\n]{0,40}?\b(?=[A-Za-z0-9]*\d)([A-Za-z0-9]{4,12})\b")
# A bare 6-digit number is the common OTP length.  No broad 4-8 digit fallback:
# it happily matches years and order ids.
_ANCHORED_6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_STOPWORDS = {"code", "codes", "verify", "token", "email", "account", "signup", "login"}


def _valid_code(value: str | None, *, email: str | None = None) -> bool:
    if not value:
        return False
    code = value.strip()
    if not (4 <= len(code) <= 12):
        return False
    if not any(ch.isdigit() for ch in code):
        return False
    if code.lower() in _STOPWORDS:
        return False
    if set(code) == {"0"}:
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", code):
        return False  # a year, not an OTP
    if email:
        local = email.split("@", 1)[0]
        if code in email or code in local:
            return False
    return True


def extract_code(text: str, pattern: str | None = None,
                 *, email: str | None = None) -> tuple[str | None, str]:
    """Return (code, how).  `how` is html_tag/keyword/anchored6/custom/none.

    The cascade is deliberate: an isolated tag (`<td>7F3K9Q</td>`) is the
    strongest signal, then a labeled code, then a bare 6-digit number.  ISO
    timestamps and dates are removed first because their fragments look like
    OTPs, and digits from the recipient address are ignored.
    """
    if not text:
        return None, "none"

    # 1. A code alone in an HTML tag.
    for match in _TAG_CODE.finditer(text):
        candidate = match.group(1)
        if _valid_code(candidate, email=email):
            return candidate, "html_tag"

    clean = html_to_text(text) if "<" in text and ">" in text else text
    clean = unicodedata.normalize("NFKC", clean)
    clean = _ISO_TS.sub(" ", clean)
    clean = _DATE.sub(" ", clean)
    clean = _SPACED_6.sub(r"\1\2", clean)
    if email:
        clean = clean.replace(email, " ")
        clean = clean.replace(email.split("@", 1)[0], " ")

    if pattern:
        match = re.search(pattern, clean, re.I | re.M)
        if not match:
            return None, "custom"
        value = match.group(1) if match.groups() else match.group(0)
        return value.strip(), "custom"

    match = _LABELED.search(clean)
    if match and _valid_code(match.group(1), email=email):
        return match.group(1), "keyword"
    match = _ANCHORED_6.search(clean)
    if match and _valid_code(match.group(1), email=email):
        return match.group(1), "anchored6"
    return None, "none"


def extract_link(text: str, pattern: str | None = None) -> str | None:
    if not text or not pattern:
        return None
    clean = unicodedata.normalize("NFKC", text)
    m = re.search(pattern, clean, re.I)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").lower()


def _norm_ts(value: Any) -> float:
    """Normalise seconds / milliseconds / ISO to epoch seconds."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    text = str(value).strip()
    if text.isdigit():
        return _norm_ts(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _http_json(method: str, url: str, *, headers: dict[str, str] | None = None,
               json_body: Any = None, params: dict[str, Any] | None = None,
               timeout: float = 30.0, retries: int = 3,
               allow_text: bool = False) -> Any:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, params=params,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise MailError(f"network error {method} {redact(url)}: {redact(exc)}") from exc
        if 500 <= response.status_code < 600:
            last = MailError(f"HTTP {response.status_code} {method} {redact(url)}")
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise last
        if response.status_code >= 400:
            raise MailError(
                f"HTTP {response.status_code} {method} {redact(url)}: {redact(response.text[:300])}"
            )
        if allow_text:
            return response.text
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text
    raise MailError(f"retries exhausted {method} {redact(url)} ({last})")


def _fill(value: Any, mapping: dict[str, str]) -> Any:
    """Replace single-brace {address} placeholders used by custom providers."""
    if isinstance(value, dict):
        return {k: _fill(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, mapping) for v in value]
    if isinstance(value, str):
        out = value
        for key, replacement in mapping.items():
            out = out.replace("{" + key + "}", str(replacement))
        return out
    return value


# --------------------------------------------------------------------------
# base provider
# --------------------------------------------------------------------------

class MailProvider:
    name = "base"

    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = dict(cfg or {})
        self.settings: Settings | None = None

    def create(self) -> Mailbox:
        raise NotImplementedError

    def messages(self, box: Mailbox) -> list[Message]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- generic polling -------------------------------------------------
    def wait_for(self, box: Mailbox, predicate: Callable[[Message], bool], *,
                 timeout: float = 180.0, interval: float = 2.0,
                 since_ids: set[str] | None = None,
                 stop_event: Any = None) -> Message | None:
        deadline = time.time() + timeout
        polls = 0
        errors = 0
        last: list[Message] = []
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MailError("stopped while waiting for mail")
            try:
                messages = self.messages(box)
                errors = 0
            except MailError:
                errors += 1
                if errors >= 5:
                    raise
                time.sleep(interval)
                continue
            polls += 1
            last = sorted(messages, key=lambda m: m.received_at)
            for message in last:
                if since_ids is not None and message.id in since_ids:
                    continue
                try:
                    if predicate(message):
                        return message
                except Exception:
                    continue
            time.sleep(interval)
        seen = "; ".join(m.brief() for m in last[-5:]) or "(inbox empty)"
        raise MailTimeout(
            f"no matching mail in {timeout:.0f}s after {polls} polls; last: {seen}"
        )

    def wait_code(self, box: Mailbox, *, code_regex: str | None = None,
                  link_regex: str | None = None,
                  sender_contains: str | None = None,
                  subject_contains: str | None = None,
                  timeout: float = 180.0, interval: float = 2.0,
                  since_ids: set[str] | None = None,
                  stop_event: Any = None) -> CodeResult:
        sender_needle = _norm(sender_contains or "")
        subject_needle = _norm(subject_contains or "")

        def predicate(message: Message) -> bool:
            if sender_needle and sender_needle not in _norm(message.sender):
                return False
            if subject_needle and subject_needle not in _norm(message.subject):
                return False
            text = message.body + "\n" + message.subject
            code, _ = extract_code(text, code_regex, email=box.address)
            link = extract_link(message.body, link_regex)
            return bool(code or link)

        message = self.wait_for(
            box, predicate, timeout=timeout, interval=interval,
            since_ids=since_ids, stop_event=stop_event,
        )
        assert message is not None
        text = message.body + "\n" + message.subject
        code, how = extract_code(text, code_regex, email=box.address)
        link = extract_link(message.body, link_regex)
        return CodeResult(code=code, message=message, link=link, how=how)

    def snapshot_ids(self, box: Mailbox) -> set[str]:
        return {m.id for m in self.messages(box)}


# --------------------------------------------------------------------------
# mock / local test provider
# --------------------------------------------------------------------------

class MockProvider(MailProvider):
    name = "mock"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.base = str(self.cfg.get("base") or "http://127.0.0.1:8765").rstrip("/")

    def create(self) -> Mailbox:
        data = _http_json("POST", f"{self.base}/api/mailboxes", json_body={})
        address = (data or {}).get("address")
        if not address:
            raise MailError(f"mock mailbox creation returned no address: {data}")
        return Mailbox(address=address, token=(data or {}).get("token"), meta={"base": self.base})

    def messages(self, box: Mailbox) -> list[Message]:
        base = box.meta.get("base", self.base)
        data = _http_json("GET", f"{base}/api/inbox", params={"email": box.address})
        out: list[Message] = []
        for item in (data or {}).get("messages", []):
            out.append(Message(
                id=str(item.get("id") or ""),
                sender=str(item.get("from_address") or item.get("from") or ""),
                subject=str(item.get("subject") or ""),
                body=str(item.get("body") or item.get("text") or ""),
                received_at=_norm_ts(item.get("received_at") or item.get("date")),
                to=str(item.get("to_address") or item.get("to") or box.address),
                raw=item,
            ))
        return out


# --------------------------------------------------------------------------
# Voidash — the workhorse inbox in several October 2026 farms
# --------------------------------------------------------------------------

class VoidashProvider(MailProvider):
    name = "voidash"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.base = str(self.cfg.get("base") or "https://api.voidash.com/api/v1").rstrip("/")
        self.domain = self.cfg.get("domain") or "voidash.bond"

    def create(self) -> Mailbox:
        data = _http_json("POST", f"{self.base}/inboxes",
                          json_body={"domain": self.domain})
        address = (data or {}).get("address")
        token = (data or {}).get("session_key") or (data or {}).get("token")
        if not address or not token:
            raise MailError(f"Voidash create returned no address/session_key: {data}")
        return Mailbox(address=str(address), token=str(token),
                       meta={"base": self.base, "domain": self.domain})

    def _headers(self, box: Mailbox) -> dict[str, str]:
        return {"Authorization": f"Bearer {box.token}", "Accept": "application/json"}

    def messages(self, box: Mailbox) -> list[Message]:
        base = box.meta.get("base", self.base)
        data = _http_json("GET", f"{base}/messages", headers=self._headers(box))
        items = data if isinstance(data, list) else (
            (data or {}).get("messages") or (data or {}).get("data") or [])
        out: list[Message] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or item.get("_id") or "")
            body = (item.get("body") or item.get("text") or item.get("html")
                    or item.get("body_text") or "")
            if not body and mid:
                try:
                    full = _http_json("GET", f"{base}/messages/{mid}",
                                      headers=self._headers(box))
                    if isinstance(full, dict):
                        body = (full.get("body") or full.get("text")
                                or full.get("html") or "")
                except MailError:
                    pass
            sender = item.get("from") or item.get("from_address") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or ""
            out.append(Message(
                id=mid, sender=str(sender),
                subject=str(item.get("subject") or ""),
                body=str(body or ""),
                received_at=_norm_ts(item.get("created_at") or item.get("date")
                                     or item.get("timestamp")),
                to=str(item.get("to") or item.get("to_address") or box.address),
                raw=item,
            ))
        return out


# --------------------------------------------------------------------------
# Cloudflare Worker temp email (custom worker contract, configurable)
# --------------------------------------------------------------------------

class CloudflareTempEmailProvider(MailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.base = str(self.cfg.get("base") or "").rstrip("/")
        if not self.base:
            raise ConfigError("mail.cloudflare_temp_email.base is required")
        self.admin_key = self.cfg.get("admin_key") or self.cfg.get("key")
        self.domain = self.cfg.get("domain")
        self.create_path = self.cfg.get("create_path", "/api/mailboxes")
        self.list_path = self.cfg.get("list_path", "/api/inbox")
        self.list_query = self.cfg.get("list_query", {"email": "{address}"})
        self.messages_path = self.cfg.get("messages_path", "messages")
        self.address_path = self.cfg.get("address_path", "emails.0")
        self.token_path = self.cfg.get("token_path")
        self.auth_header = self.cfg.get("auth_header", "Authorization")
        self.auth_scheme = self.cfg.get("auth_scheme", "Bearer ")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "autoreg/1"}
        if self.admin_key:
            headers[self.auth_header] = f"{self.auth_scheme}{self.admin_key}"
        return headers

    def create(self) -> Mailbox:
        body: dict[str, Any] = {}
        if self.domain:
            body["domain"] = self.domain
        data = _http_json("POST", self.base + self.create_path,
                          headers={**self._headers(), "Content-Type": "application/json"},
                          json_body=body)
        address = _deep(data, self.address_path)
        if not address:
            raise MailError(f"temp-mail create returned no address at {self.address_path}: {data}")
        token = _deep(data, self.token_path) if self.token_path else None
        return Mailbox(address=str(address), token=token, meta={"base": self.base})

    def messages(self, box: Mailbox) -> list[Message]:
        base = box.meta.get("base", self.base)
        params = _fill(self.list_query, {"address": box.address})
        data = _http_json("GET", base + self.list_path,
                          headers=self._headers(), params=params)
        if not isinstance(data, dict):
            messages = data if isinstance(data, list) else []
        else:
            messages = _deep(data, self.messages_path, []) or []
        out: list[Message] = []
        mapping = self.cfg.get("map") or {}
        for item in messages:
            if not isinstance(item, dict):
                continue
            out.append(Message(
                id=str(_pick(item, mapping, "id", "id") or ""),
                sender=str(_pick(item, mapping, "sender", "from_address", "from") or ""),
                subject=str(_pick(item, mapping, "subject", "subject") or ""),
                body=str(_pick(item, mapping, "body", "body", "text", "raw_text") or ""),
                received_at=_norm_ts(_pick(item, mapping, "received_at", "received_at", "date")),
                to=str(_pick(item, mapping, "to", "to_address", "to") or box.address),
                raw=item,
            ))
        return out


# --------------------------------------------------------------------------
# mail.tm / mail.gw
# --------------------------------------------------------------------------

class MailTMProvider(MailProvider):
    name = "mailtm"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.base = str(self.cfg.get("base") or "https://api.mail.tm").rstrip("/")
        self._details: dict[str, Message] = {}

    def _random_address(self, domain: str) -> str:
        import secrets
        return f"ar{secrets.token_hex(6)}@{domain}".lower()

    def create(self) -> Mailbox:
        address = self.cfg.get("address")
        password = self.cfg.get("password")
        if address and password:
            token_data = _http_json("POST", f"{self.base}/token",
                                    json_body={"address": address, "password": password})
            return Mailbox(address=address, token=token_data["token"], meta={"base": self.base})
        domains = _http_json("GET", f"{self.base}/domains")
        members = domains if isinstance(domains, list) else (domains or {}).get("hydra:member") or []
        if not members:
            raise MailError("mail.tm returned no domains")
        domain = members[0]["domain"]
        address = (address or self._random_address(domain)).lower()
        password = password or "Autoreg!" + __import__("secrets").token_urlsafe(12)
        _http_json("POST", f"{self.base}/accounts",
                   json_body={"address": address, "password": password})
        token_data = _http_json("POST", f"{self.base}/token",
                                json_body={"address": address, "password": password})
        return Mailbox(address=address, token=token_data["token"],
                       meta={"base": self.base, "password": password})

    def _auth(self, box: Mailbox) -> dict[str, str]:
        return {"Authorization": f"Bearer {box.token}", "Accept": "application/json"}

    def messages(self, box: Mailbox) -> list[Message]:
        base = box.meta.get("base", self.base)
        data = _http_json("GET", f"{base}/messages?page=1", headers=self._auth(box))
        members = data if isinstance(data, list) else (data or {}).get("hydra:member") or []
        out: list[Message] = []
        for item in members:
            mid = str(item.get("id") or "")
            sender = ((item.get("from") or {}).get("address") or "")
            subject = str(item.get("subject") or "")
            intro = str(item.get("intro") or "")
            received = _norm_ts(item.get("createdAt"))
            # Fetch the full body once per message; the list endpoint truncates.
            detailed = self._details.get(mid)
            if detailed is None and mid:
                try:
                    full = _http_json("GET", f"{base}/messages/{mid}", headers=self._auth(box))
                    body = _body_from_mailtm(full)
                    detailed = Message(id=mid, sender=sender, subject=subject,
                                       body=body, received_at=received,
                                       to=str((full or {}).get("to") or box.address), raw=full or {})
                    self._details[mid] = detailed
                except MailError:
                    detailed = None
            out.append(detailed or Message(
                id=mid, sender=sender, subject=subject, body=intro,
                received_at=received, to=box.address, raw=item,
            ))
        return out


def _body_from_mailtm(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("text"):
        return str(item["text"])
    html = item.get("html")
    if isinstance(html, list):
        html = "\n".join(str(x) for x in html)
    return str(html or "")


class MailGWProvider(MailTMProvider):
    """mail.gw speaks the same API as mail.tm."""

    name = "mailgw"

    def __init__(self, cfg: dict[str, Any] | None = None):
        merged = dict(cfg or {})
        merged.setdefault("base", "https://api.mail.gw")
        super().__init__(merged)


# --------------------------------------------------------------------------
# IMAP (Outlook / Yandex / custom domains)
# --------------------------------------------------------------------------

class ImapProvider(MailProvider):
    name = "imap"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.host = self.cfg.get("host")
        self.port = int(self.cfg.get("port") or 993)
        self.ssl = bool(self.cfg.get("ssl", True))
        self.user = self.cfg.get("user") or self.cfg.get("address")
        self.password = self.cfg.get("password")
        self.address = self.cfg.get("address") or self.user
        self.mailbox = self.cfg.get("mailbox") or "INBOX"
        self.oauth2 = self.cfg.get("oauth2") or (
            self.cfg.get("access_token") if self.cfg.get("access_token") else None
        )
        self.alias = self.cfg.get("alias") or {}
        if isinstance(self.alias, str):
            self.alias = {"template": self.alias}
        self.catch_all = bool(self.cfg.get("catch_all") or self.alias.get("catch_all"))
        self.alias_domain = self.cfg.get("alias_domain") or self.alias.get("domain")
        self.alias_prefix = self.cfg.get("alias_prefix") or self.alias.get("prefix") or "u"
        self.search = self.cfg.get("search")
        self.mailboxes = list(self.cfg.get("mailboxes") or [self.mailbox])
        self.recipient_headers = [
            h.lower() for h in (self.cfg.get("recipient_headers")
                                or ["To", "Delivered-To", "X-Original-To", "Cc"])
        ]
        if not self.host or not self.user:
            raise ConfigError("mail.imap requires host and user/address")

    def _make_alias(self) -> str:
        import secrets
        tag = f"{self.alias.get('tag_prefix') or 'reg'}{secrets.token_hex(4)}"
        base_user = (self.address or self.user or "").split("@", 1)[0].split("+", 1)[0]
        domain = self.alias_domain or (self.address or self.user or "").split("@")[-1]
        template = self.alias.get("template")
        if template:
            return str(template).format(
                user=base_user, domain=domain, tag=tag,
                rand=secrets.token_hex(4), prefix=self.alias_prefix,
            )
        if self.catch_all:
            return f"{self.alias_prefix}{tag}@{domain}"
        raise ConfigError("mail.imap alias needs alias.template or catch_all+alias_domain")

    def create(self) -> Mailbox:
        if self.alias or self.catch_all:
            address = self._make_alias()
            return Mailbox(address=address, token=self.password,
                           meta={"provider": "imap", "alias": True,
                                 "base_address": self.address})
        return Mailbox(address=self.address, token=self.password,
                       meta={"provider": "imap"})

    def _search_criteria(self, address: str) -> str:
        if self.search:
            return str(self.search).format(address=address)
        if self.alias or self.catch_all:
            return f'TO "{address}"'
        return "ALL"

    def _connect(self) -> imaplib.IMAP4:
        if self.ssl:
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        else:
            conn = imaplib.IMAP4(self.host, self.port, timeout=30)
        if self.oauth2:
            token = str(self.oauth2)

            def auth(_: bytes) -> bytes:
                return f"user={self.user}\x01auth=Bearer {token}\x01\x01".encode()

            conn.authenticate("XOAUTH2", auth)
        else:
            if not self.password:
                raise ConfigError("mail.imap requires password or access_token")
            conn.login(self.user, str(self.password))
        return conn

    def messages(self, box: Mailbox) -> list[Message]:
        conn = self._connect()
        try:
            out: list[Message] = []
            seen: set[tuple[str, str]] = set()
            for mailbox in self.mailboxes:
                target = f'"{mailbox}"' if " " in mailbox else mailbox
                status, _ = conn.select(target, readonly=True)
                if status != "OK":
                    raise MailError(f"IMAP select {mailbox} failed: {status}")
                criteria = self._search_criteria(box.address)
                status, data = conn.uid("search", None, criteria)
                if status != "OK":
                    raise MailError(f"IMAP search {criteria!r} failed: {status}")
                uids = (data[0] or b"").split()[-40:]
                for uid in uids:
                    marker = (mailbox, uid.decode(errors="replace"))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    status, payload = conn.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        continue
                    message = _parse_email(payload[0][1],
                                           uid.decode(errors="replace"), box.address)
                    message.raw["mailbox"] = mailbox
                    if (self.alias or self.catch_all) and not self._recipient_ok(message, box.address):
                        continue
                    out.append(message)
            out.sort(key=lambda m: m.received_at)
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _recipient_ok(self, message: Message, address: str) -> bool:
        needle = address.lower()
        headers = (message.raw or {}).get("headers") or {}
        if needle in (message.to or "").lower():
            return True
        matches = [value for key, value in headers.items()
                   if key in self.recipient_headers and needle in str(value).lower()]
        if matches:
            return True
        # If the provider did not expose recipient headers, trust the server-side
        # TO search rather than dropping a valid message.
        return not any(key in headers for key in self.recipient_headers)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_email(raw: bytes, uid: str, address: str) -> Message:
    msg = email.message_from_bytes(raw)
    headers = {key.lower(): _decode_header(value) for key, value in msg.items()}
    sender = _decode_header(msg.get("From"))
    subject = _decode_header(msg.get("Subject"))
    to = _decode_header(msg.get("To")) or address
    date = msg.get("Date")
    try:
        received = parsedate_to_datetime(date).timestamp() if date else 0.0
    except (TypeError, ValueError):
        received = 0.0
    body = ""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in {"text/plain", "text/html"}:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            parts.append(html_to_text(text) if ctype == "text/html" else text)
        body = "\n".join(parts)
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            body = html_to_text(text) if msg.get_content_type() == "text/html" else text
        except Exception:
            body = ""
    return Message(id=uid, sender=sender, subject=subject, body=body,
                   received_at=received, to=to,
                   raw={"uid": uid, "headers": headers})


# --------------------------------------------------------------------------
# Microsoft Graph (Outlook mailbox with refresh token)
# --------------------------------------------------------------------------

class OutlookGraphProvider(MailProvider):
    name = "outlook_graph"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.email = self.cfg.get("email") or self.cfg.get("address")
        self.client_id = self.cfg.get("client_id")
        self.refresh_token = self.cfg.get("refresh_token")
        self.tenant = self.cfg.get("tenant") or "common"
        self.scope = self.cfg.get("scope") or "https://graph.microsoft.com/.default offline_access"
        self._access_token = self.cfg.get("access_token")
        self._expires_at = 0.0
        if not self.email:
            raise ConfigError("mail.outlook_graph requires email")

    def create(self) -> Mailbox:
        return Mailbox(address=self.email, meta={"provider": "outlook_graph"})

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        if not (self.client_id and self.refresh_token):
            if self._access_token:
                return self._access_token
            raise ConfigError("mail.outlook_graph requires refresh_token+client_id or access_token")
        # requests' json= cannot send form encoding; do it explicitly.
        response = requests.post(
            f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
                "scope": self.scope,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise MailError(f"Outlook token refresh failed: HTTP {response.status_code} {redact(response.text[:300])}")
        payload = response.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in") or 3600)
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]
        return self._access_token

    def messages(self, box: Mailbox) -> list[Message]:
        token = self._token()
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me/messages",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={
                "$top": "25",
                "$select": "id,from,subject,receivedDateTime,body,toRecipients",
                "$orderby": "receivedDateTime desc",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise MailError(f"Graph messages failed: HTTP {response.status_code} {redact(response.text[:300])}")
        out: list[Message] = []
        for item in (response.json() or {}).get("value", []):
            sender = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            body = ((item.get("body") or {}).get("content") or "")
            out.append(Message(
                id=str(item.get("id") or ""),
                sender=sender,
                subject=str(item.get("subject") or ""),
                body=str(body),
                received_at=_norm_ts(item.get("receivedDateTime")),
                to=box.address,
                raw={"id": item.get("id")},
            ))
        return out


# --------------------------------------------------------------------------
# custom HTTP provider (declarative; good for adding a new temp-mail API)
# --------------------------------------------------------------------------

class CustomHTTPProvider(MailProvider):
    name = "custom_http"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.base = str(self.cfg.get("base") or "").rstrip("/")
        if not self.base:
            raise ConfigError("custom_http mail provider requires base")
        self.headers = dict(self.cfg.get("headers") or {})
        self.create_cfg = dict(self.cfg.get("create") or {})
        self.list_cfg = dict(self.cfg.get("list") or {})

    def create(self) -> Mailbox:
        cfg = self.create_cfg
        method = str(cfg.get("method") or "POST").upper()
        path = str(cfg.get("path") or "/api/mailboxes")
        body = _fill(cfg.get("json"), {})
        data = _http_json(method, self.base + path,
                          headers={**self.headers, "Content-Type": "application/json"},
                          json_body=body, params=_fill(cfg.get("query"), {}))
        address = _deep(data, cfg.get("address_path") or "address")
        if not address:
            raise MailError(f"custom mail create returned no address at {cfg.get('address_path')}: {data}")
        token = _deep(data, cfg["token_path"]) if cfg.get("token_path") else None
        return Mailbox(address=str(address), token=token,
                       meta={"base": self.base, "create": data})

    def messages(self, box: Mailbox) -> list[Message]:
        cfg = self.list_cfg
        method = str(cfg.get("method") or "GET").upper()
        path = _fill(str(cfg.get("path") or "/api/inbox"), {"address": box.address})
        query = _fill(cfg.get("query"), {"address": box.address})
        data = _http_json(method, self.base + path, headers=self.headers,
                          params=query, json_body=_fill(cfg.get("json"), {"address": box.address}))
        raw_messages = _deep(data, cfg.get("messages_path") or "messages", []) or []
        mapping = cfg.get("map") or {}
        out: list[Message] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            out.append(Message(
                id=str(_pick(item, mapping, "id", "id") or ""),
                sender=str(_pick(item, mapping, "sender", "from", "from_address") or ""),
                subject=str(_pick(item, mapping, "subject", "subject") or ""),
                body=str(_pick(item, mapping, "body", "body", "text", "raw_text") or ""),
                received_at=_norm_ts(_pick(item, mapping, "received_at", "received_at", "date")),
                to=str(_pick(item, mapping, "to", "to", "to_address") or box.address),
                raw=item,
            ))
        return out


# --------------------------------------------------------------------------
# helpers / factory
# --------------------------------------------------------------------------

def _pick(item: dict[str, Any], mapping: dict[str, str], canonical: str,
          *defaults: str) -> Any:
    if canonical in mapping:
        return _deep(item, mapping[canonical])
    for key in defaults:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _deep(data: Any, path: str | None, default: Any = None) -> Any:
    if not path:
        return data
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


PROVIDERS: dict[str, type[MailProvider]] = {
    "mock": MockProvider,
    "cloudflare_temp_email": CloudflareTempEmailProvider,
    "cloudflare": CloudflareTempEmailProvider,
    "cf_temp_email": CloudflareTempEmailProvider,
    "mailtm": MailTMProvider,
    "mail.tm": MailTMProvider,
    "mailgw": MailGWProvider,
    "mail.gw": MailGWProvider,
    "voidash": VoidashProvider,
    "imap": ImapProvider,
    "outlook_graph": OutlookGraphProvider,
    "outlook": OutlookGraphProvider,
    "graph": OutlookGraphProvider,
    "custom_http": CustomHTTPProvider,
    "custom": CustomHTTPProvider,
}


def make_provider(settings: Settings, spec: Any = None, *,
                  resolved: bool = False) -> MailProvider:
    """Build a provider.

    `resolved=True` means the spec already came from a stored session: it is
    used verbatim, never merged with whatever the project config says today,
    because the stored mailbox token belongs to that exact backend.
    """
    if resolved:
        if not isinstance(spec, dict):
            raise ConfigError("A restored mail session needs a provider mapping")
        cfg = dict(spec)
    else:
        cfg = settings.provider_inline("mail", spec)
    type_name = str(cfg.get("type") or cfg.get("_name") or "").lower()
    cls = PROVIDERS.get(type_name)
    if not cls:
        raise ConfigError(
            f"Unknown mail provider {type_name!r}. Available: {', '.join(sorted(PROVIDERS))}"
        )
    provider = cls(cfg)
    provider.settings = settings
    return provider
