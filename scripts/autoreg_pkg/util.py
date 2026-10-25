"""Small, dependency-light helpers shared by the autoreg pack.

Everything here is deliberately boring: file IO, YAML/JSON, variable rendering,
redaction, name/password generation.  No service-specific logic.
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import string
import time
import unicodedata
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import yaml


class AutoregError(RuntimeError):
    """Base error for the pack.  Message must be actionable for a human/agent."""


class ConfigError(AutoregError):
    pass


class FlowError(AutoregError):
    def __init__(self, message: str, *, step: int | None = None,
                 action: str | None = None, artifacts: str | None = None):
        self.step = step
        self.action = action
        self.artifacts = artifacts
        prefix = ""
        if step is not None:
            prefix = f"step {step}"
            if action:
                prefix += f" ({action})"
            prefix += ": "
        super().__init__(prefix + message)


class RenderError(AutoregError):
    pass


class MailError(AutoregError):
    pass


class MailTimeout(MailError):
    pass


class CaptchaError(AutoregError):
    pass


# --------------------------------------------------------------------------
# time / ids
# --------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


def run_stamp() -> str:
    return utcnow().strftime("%Y%m%d-%H%M%S")


def short_id(n: int = 8) -> str:
    return secrets.token_hex(n // 2)


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------

def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"YAML file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level YAML in {p} must be a mapping, got {type(data).__name__}")
    return data


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_write(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def load_dotenv(*paths: str | Path) -> None:
    """Minimal .env loader.  Real environment variables win."""
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate_env(value: Any) -> Any:
    """Replace ${VAR} / ${VAR:-default} recursively."""
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        found = os.getenv(name)
        if found is not None and found != "":
            return found
        if default is not None:
            return default
        raise ConfigError(f"Environment variable {name} is required but not set")

    return _ENV_RE.sub(repl, value)


# --------------------------------------------------------------------------
# data access / rendering
# --------------------------------------------------------------------------

def deep_get(obj: Any, path: str, default: Any = None) -> Any:
    """Resolve `a.b.0.c` against dict/list.  Returns default on any miss."""
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*\}\}")


def render(value: Any, variables: dict[str, Any], *, strict: bool = True) -> Any:
    """Render {{var}} placeholders.

    A string that is exactly one placeholder keeps the variable's type (so
    `timeout: "{{n}}"` can yield an int).  Missing variables raise with the
    variable name, because a silently empty selector is much harder to debug.
    """
    if isinstance(value, dict):
        return {k: render(v, variables, strict=strict) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables, strict=strict) for v in value]
    if not isinstance(value, str):
        return value

    whole = _VAR_RE.fullmatch(value.strip())
    if whole:
        name = whole.group(1)
        if name in variables:
            return variables[name]
        if strict:
            raise RenderError(f"Unknown variable {{{{{name}}}}}. Known: {', '.join(sorted(variables)) or '(none)'}")
        return ""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return str(variables[name])
        if strict:
            raise RenderError(f"Unknown variable {{{{{name}}}}}. Known: {', '.join(sorted(variables)) or '(none)'}")
        return ""

    return _VAR_RE.sub(repl, value)


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------
# redaction / text
# --------------------------------------------------------------------------

_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret|refresh_token|access_token)\s*[:=]\s*)['\"]?[^\s'\"&,}]+"),
    re.compile(r"(?i)([?&](?:key|token|api_key|password)=)[^&\s]+"),
]


def redact(text: Any) -> str:
    s = str(text)
    for pat in _REDACT_PATTERNS:
        s = pat.sub(lambda m: m.group(1) + "***", s)
    return s


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._skip += 1
        elif tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


# --------------------------------------------------------------------------
# random identity material
# --------------------------------------------------------------------------

FIRST_NAMES = [
    "James", "Olivia", "Daniel", "Emma", "Noah", "Mia", "Lucas", "Ava",
    "Henry", "Sofia", "Ethan", "Chloe", "Leo", "Grace", "Jack", "Lily",
]
LAST_NAMES = [
    "Miller", "Turner", "Walker", "Bennett", "Carter", "Hayes", "Brooks",
    "Reed", "Foster", "Morgan", "Bailey", "Sullivan", "Coleman", "Parker",
]


def random_person(rng: random.Random | None = None) -> dict[str, str]:
    r = rng or random.SystemRandom()
    first = r.choice(FIRST_NAMES)
    last = r.choice(LAST_NAMES)
    return {
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}",
        "username": f"{first.lower()}{last.lower()}{r.randint(10, 9999)}",
    }


def random_password(length: int = 16, rng: random.Random | None = None) -> str:
    r = rng or random.SystemRandom()
    # Guarantee the classes sites commonly require, then shuffle.
    lower = r.choice(string.ascii_lowercase)
    upper = r.choice(string.ascii_uppercase)
    digit = r.choice(string.digits)
    symbol = r.choice("!@#$%^&*")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    rest = [r.choice(alphabet) for _ in range(max(4, length - 4))]
    chars = list(lower + upper + digit + symbol) + rest
    r.shuffle(chars)
    return "".join(chars)


def random_local_part(prefix: str = "u", rng: random.Random | None = None) -> str:
    r = rng or random.SystemRandom()
    return f"{prefix}{r.randint(100000, 999999)}{r.choice(string.ascii_lowercase)}"


def slugify(text: str, max_len: int = 60) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-._")
    return (s[:max_len] or "item").lower()


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------

def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def friendly_exc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact(exc)}"


_PERMANENT = {"blocked", "config", "target", "mail_burned"}


def classify_error(exc: BaseException) -> str:
    """Map an exception to a stable failure class.

    The runner uses this to stop retrying permanent failures (blocked account,
    bad adapter, wrong selector) while still retrying mail/captcha/proxy/429.
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "mailtimeout" in name or "mailtimeout" in text:
        return "mail"
    if "mailerror" in name or "mailerror" in text:
        return "mail"
    if "captcha" in name or "captchaerror" in text:
        return "captcha"
    if "proxy" in name or "proxyerror" in text:
        return "proxy"
    if "config" in name or "render" in name or "configerror" in text \
            or "rendererror" in text:
        return "config"
    if "ratelimit" in text or "rate limit" in text or "too many requests" in text \
            or "429" in text or "retryafter" in text:
        return "ratelimit"
    if "blocked" in text or "forbidden" in text or "403" in text \
            or "unauthorized" in text or "401" in text or "banned" in text:
        return "blocked"
    if "timed out" in text or "timeout" in text or "connection" in text \
            or "network" in text or "temporarily" in text:
        return "retryable"
    if "locator" in text or "selector" in text or "wait_for" in text \
            or "waiting for" in text:
        return "target"
    return "unknown"


def policy_for(kind: str) -> str:
    """Return `retry` or `stop` for a failure class."""
    return "stop" if kind in _PERMANENT else "retry"
