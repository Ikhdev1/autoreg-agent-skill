"""Work directory, settings and provider resolution.

A *home* is a per-project `.autoreg/` directory.  It keeps profiles, the
encrypted account store, run ledgers and artifacts.  The pack itself lives in
the skill directory, so several projects can share one installation without
sharing identities.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import ConfigError, deep_get, interpolate_env, load_dotenv, load_yaml

DEFAULTS: dict[str, Any] = {
    "engine": "auto",
    "timeout": {
        "nav": 45,
        "step": 20,
        "otp": 180,
        "captcha": 180,
    },
    "limits": {
        "concurrency": 1,
        "min_delay": 2.0,
        "attempts": 2,
        "poll_interval": 2.0,
    },
    "identity": {
        "engine": "auto",
        "align_geo": True,
        "humanize": True,
        "canvas_noise": False,
    },
    "mail": {
        "default": "mock",
        "providers": {},
    },
    "captcha": {
        "default": "none",
        "providers": {},
    },
    "vision": {
        "default": "openai_compat",
        "providers": {},
    },
    "stt": {
        "default": "openai_compat",
        "providers": {},
    },
    "proxy": {
        "default": "direct",
        "strategy": "per_account",
        "pool": [],
        "health_check": True,
        "required": False,
    },
    "paths": {},
}

CONFIG_TEMPLATE = """\
# autoreg project settings.  Secrets belong in environment variables or .env,
# never in this file.  ${VAR} and ${VAR:-default} are expanded on load.

engine: auto                 # auto | camoufox | patchright | playwright

timeout:
  nav: 45                    # navigation, seconds
  step: 20                   # per-step default
  otp: 180                   # wait for an email code
  captcha: 180               # wait for a solver

limits:
  concurrency: 1             # keep at 1 unless you know the target tolerates more
  min_delay: 2.0             # seconds between account starts
  attempts: 2
  poll_interval: 2.0         # mail polling interval

identity:
  align_geo: true            # timezone/locale from the proxy exit
  humanize: true

mail:
  default: mock
  providers: {}
  # cloudflare_temp_email:
  #   base: ${TEMPMAIL_BASE}
  #   admin_key: ${TEMPMAIL_ADMIN_KEY}
  #   domain: ${TEMPMAIL_DOMAIN}
  # mailtm: {}
  # outlook_graph:
  #   email: ${OUTLOOK_EMAIL}
  #   client_id: ${OUTLOOK_CLIENT_ID}
  #   refresh_token: ${OUTLOOK_REFRESH_TOKEN}
  # imap:
  #   host: imap.example.com
  #   port: 993
  #   user: ${IMAP_USER}
  #   password: ${IMAP_PASSWORD}

captcha:
  default: none              # none | static | 2captcha | yescaptcha | local | manual | empty
  providers: {}
  # 2captcha:
  #   key: ${TWOCAPTCHA_KEY}
  # yescaptcha:
  #   key: ${YESCAPTCHA_KEY}
  # local:
  #   base: http://127.0.0.1:8889

# Vision/STT are used by solve_image_captcha and solve_audio_captcha.
# Any OpenAI-compatible endpoint works: DashScope, OpenRouter, local vLLM/Ollama.
vision:
  default: openai_compat
  providers: {}
  # openai_compat:
  #   base: http://127.0.0.1:16432
  #   key: local-vote
  #   model: qwen3-vl-plus

stt:
  default: openai_compat
  providers: {}
  # openai_compat:
  #   base: http://127.0.0.1:16432
  #   key: local-vote
  #   model: whisper-1

proxy:
  default: direct            # direct or a proxy URL
  strategy: per_account      # per_account | per_attempt | round_robin
  health_check: true         # probe the exit before spending a mailbox/captcha
  required: false            # fail closed when no proxy is configured
  pool: []
  # pool:
  #   - http://user:pass@host:port
"""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_home(explicit: str | Path | None = None) -> Path:
    """Accept either a project directory or the .autoreg home itself."""
    if explicit:
        home = Path(explicit).expanduser()
        if home.name == ".autoreg" or (home / "config.yaml").is_file() \
                or (home / "accounts.db").is_file():
            return home.resolve()
        return (home / ".autoreg").resolve()
    if os.getenv("AUTOREG_HOME"):
        return Path(os.environ["AUTOREG_HOME"]).expanduser().resolve()
    return (Path.cwd() / ".autoreg").resolve()


@dataclass
class Paths:
    home: Path
    config: Path
    db: Path
    key: Path
    profiles: Path
    runs: Path
    artifacts: Path
    adapters: Path
    recon: Path

    @classmethod
    def from_home(cls, home: Path) -> "Paths":
        return cls(
            home=home,
            config=home / "config.yaml",
            db=home / "accounts.db",
            key=home / "key",
            profiles=home / "profiles",
            runs=home / "runs",
            artifacts=home / "artifacts",
            adapters=home / "adapters",
            recon=home / "recon",
        )

    def ensure(self) -> None:
        for p in (self.home, self.profiles, self.runs, self.artifacts,
                  self.adapters, self.recon):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    home: Path
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.paths = Paths.from_home(self.home)

    # -- lookups ---------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        return deep_get(self.raw, path, default)

    def timeout(self, name: str, default: float | None = None) -> float:
        value = self.get(f"timeout.{name}", default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 20.0

    def provider(self, kind: str, name: str | None,
                 overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge provider config from settings with adapter-level overrides."""
        if not name:
            name = self.get(f"{kind}.default")  # type: ignore[arg-type]
        if not name:
            raise ConfigError(f"No {kind} provider selected")
        configured = self.get(f"{kind}.providers.{name}", {}) or {}
        if not isinstance(configured, dict):
            raise ConfigError(f"{kind}.providers.{name} must be a mapping")
        merged = dict(configured)
        if overrides:
            merged.update({k: v for k, v in overrides.items() if v is not None})
        merged.setdefault("type", name)
        merged["_name"] = name
        return merged

    def provider_inline(self, kind: str, spec: Any) -> dict[str, Any]:
        """Adapter may pass a provider name or an inline provider mapping."""
        if spec is None:
            return self.provider(kind, None)
        if isinstance(spec, str):
            return self.provider(kind, spec)
        if isinstance(spec, dict):
            name = spec.get("type") or spec.get("provider") or self.get(f"{kind}.default")
            if not name:
                raise ConfigError(f"Inline {kind} provider has no type")
            configured = self.get(f"{kind}.providers.{name}", {}) or {}
            merged = dict(configured)
            merged.update(spec)
            merged["type"] = name
            merged["_name"] = spec.get("_name", name)
            return merged
        raise ConfigError(f"{kind} provider must be a name or mapping, got {type(spec).__name__}")


def ensure_home(home: Path, *, create_config: bool = False, force: bool = False) -> Settings:
    paths = Paths.from_home(home)
    paths.ensure()
    if create_config and (force or not paths.config.is_file()):
        paths.config.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    gitignore = home / ".gitignore"
    if create_config and not gitignore.is_file():
        gitignore.write_text(
            "key\naccounts.db\naccounts.db-*\nprofiles/\nartifacts/\nruns/\nrecon/\n"
            "adapters/\n.env\n*.log\n", encoding="utf-8")
    return load_settings(home)


def load_settings(home: str | Path | None = None, *, require_config: bool = False) -> Settings:
    home_path = resolve_home(home)
    paths = Paths.from_home(home_path)
    # Load .env from the project dir, the home dir and their parent.
    load_dotenv(Path.cwd() / ".env", home_path / ".env", home_path.parent / ".env")
    if require_config and not paths.config.is_file():
        raise ConfigError(f"No config at {paths.config}. Run: autoreg init --home {home_path.parent}")
    raw: dict[str, Any] = {}
    if paths.config.is_file():
        raw = load_yaml(paths.config)
    raw = interpolate_env(raw or {})
    merged = _deep_merge(DEFAULTS, raw)
    return Settings(home=home_path, raw=merged)


def write_default_config(home: Path, *, force: bool = False) -> Path:
    paths = Paths.from_home(home)
    paths.ensure()
    if paths.config.is_file() and not force:
        return paths.config
    paths.config.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    return paths.config
