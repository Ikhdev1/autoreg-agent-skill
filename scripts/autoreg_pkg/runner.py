"""Batch runner: one identity, mailbox, proxy and profile per account.

Retries reuse the same identity and mailbox.  That matters: creating a new
mailbox after a late failure loses the account that may already exist, and a
new profile destroys the cookies that verification needs.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser import resolve_engine
from .config import Settings
from .flow import Adapter, FlowContext, load_adapter, run_flow
from .identity import align_to_geo, make_profile, validate_profile
from .mail import MailSession, Mailbox, make_provider
from .proxy import Proxy, ProxyError, ProxyPool, pool_from_settings
from .store import Store
from .util import (ConfigError, FlowError, classify_error, friendly_exc, now_iso,
                   policy_for, random_password, random_person, render, run_stamp,
                   short_id, slugify, write_json)

_DIRECT_GEO: dict[str, Any] | None = None
_DIRECT_LOCK = threading.Lock()


@dataclass
class RunSummary:
    run_id: str
    service: str
    total: int
    dry_run: bool = False
    success: int = 0
    failed: int = 0
    verified: int = 0
    unverified: int = 0
    pending: int = 0
    accounts: list[int] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "service": self.service, "total": self.total,
            "dry_run": self.dry_run, "success": self.success, "failed": self.failed,
            "verified": self.verified, "unverified": self.unverified,
            "pending": self.pending,
            "accounts": self.accounts, "errors": self.errors,
            "results": self.results, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def load_profile(settings: Settings, profile_id_or_path: str) -> dict[str, Any]:
    p = Path(profile_id_or_path)
    if not p.is_file():
        p = settings.paths.profiles / (profile_id_or_path + ".json")
    if not p.is_file():
        raise ConfigError(f"Profile not found: {profile_id_or_path}")
    profile = json.loads(p.read_text(encoding="utf-8"))
    errors = validate_profile(profile)
    if errors:
        raise ConfigError(f"Invalid profile {p}: {', '.join(errors)}")
    return profile


def save_profile(settings: Settings, profile: dict[str, Any]) -> Path:
    settings.paths.ensure()
    path = settings.paths.profiles / (profile["id"] + ".json")
    write_json(path, profile)
    return path


def run_batch(settings: Settings, adapter_path: str | Path, *,
              count: int = 1, concurrency: int = 1, engine: str | None = None,
              headed: bool = False, proxy: str | None = None,
              dry_run: bool = False, no_verify: bool = False,
              var_overrides: dict[str, Any] | None = None,
              email: str | None = None, password: str | None = None,
              stop_on_error: bool = False) -> RunSummary:
    adapter = load_adapter(adapter_path)
    settings.paths.ensure()
    if count < 1:
        raise ConfigError("--count must be >= 1")
    if concurrency < 1 or concurrency > 8:
        raise ConfigError("--concurrency must be 1..8")
    if email and count > 1:
        raise ConfigError("--email can be used only with --count 1")
    if password and count > 1:
        raise ConfigError("--password can be used only with --count 1")
    chosen_engine = resolve_engine(
        engine or adapter.engine or settings.get("identity.engine") or settings.get("engine"),
        stealth=adapter.stealth,
    )
    run_id = f"{run_stamp()}-{slugify(adapter.name)}-{short_id(4)}"
    summary = RunSummary(run_id=run_id, service=adapter.name, total=count, dry_run=dry_run)
    pool = pool_from_settings(settings, proxy)
    stop = threading.Event()

    if dry_run:
        for i in range(count):
            task = f"{adapter.name}-{i + 1}"
            summary.results.append({
                "task": task, "ok": None, "status": "dry-run",
                "engine": chosen_engine, "mode": adapter.mode,
                "steps": len(adapter.steps),
                "mail": (adapter.get("mail") or {}).get("provider")
                if isinstance(adapter.get("mail"), dict) else adapter.get("mail"),
                "proxy": pool.pick(task).safe() if pool else "direct",
            })
            print(f"[dry-run] {task}: mode={adapter.mode} steps={len(adapter.steps)} "
                  f"engine={chosen_engine} mail={summary.results[-1]['mail']} "
                  f"proxy={summary.results[-1]['proxy']}", file=sys.stderr)
        summary.finished_at = now_iso()
        return summary

    lock = threading.Lock()

    def worker(index: int) -> dict[str, Any]:
        task = f"{adapter.name}-{index + 1}-{short_id(3)}"
        return _run_one(settings, adapter, index=index, task=task, run_id=run_id,
                        pool=pool, engine=chosen_engine, headed=headed,
                        no_verify=no_verify, var_overrides=var_overrides or {},
                        email=email, password=password, stop=stop)

    futures = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for i in range(count):
            if stop.is_set():
                break
            futures.append(executor.submit(worker, i))
            if i < count - 1:
                delay = float(settings.get("limits.min_delay", 2.0))
                if delay > 0:
                    time.sleep(delay)
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"task": "?", "ok": False, "error": friendly_exc(exc)}
            with lock:
                summary.results.append(result)
                if result.get("ok"):
                    summary.success += 1
                    if result.get("status") == "verified":
                        summary.verified += 1
                    elif result.get("status") == "unverified":
                        summary.unverified += 1
                    elif result.get("status") == "pending":
                        summary.pending += 1
                    if result.get("account_id"):
                        summary.accounts.append(int(result["account_id"]))
                    print(f"[ok] {result['task']}: {result.get('email')} "
                          f"({result.get('status')})", file=sys.stderr)
                elif result.get("status") != "dry-run":
                    summary.failed += 1
                    summary.errors.append(f"{result.get('task')}: {result.get('error')}")
                    print(f"[fail] {result.get('task')}: {result.get('error')}", file=sys.stderr)
                    if stop_on_error:
                        stop.set()
    summary.finished_at = now_iso()
    return summary


def _run_one(settings: Settings, adapter: Adapter, *, index: int, task: str,
             run_id: str, pool: ProxyPool, engine: str, headed: bool,
             no_verify: bool, var_overrides: dict[str, Any],
             email: str | None, password: str | None,
             stop: threading.Event) -> dict[str, Any]:
    started_at = now_iso()
    artifact_root = settings.paths.artifacts / run_id / task
    artifact_root.mkdir(parents=True, exist_ok=True)
    store = Store(settings)
    provider = None
    proxy: Proxy | None = None
    result: dict[str, Any] = {"task": task, "ok": False, "email": email,
                              "artifact_dir": str(artifact_root)}
    try:
        if stop.is_set():
            raise FlowError("stopped by --stop-on-error")

        proxy = pool.pick(task)
        if proxy is None and bool(settings.get("proxy.required", False)):
            raise ProxyError("proxy.required is true but no live proxy is configured")
        geo = None
        if proxy is not None and bool(settings.get("proxy.health_check", True)):
            # Probe before creating a mailbox or paying for a captcha.
            geo = pool.probe(proxy)
        elif bool(settings.get("identity.align_geo", True)):
            geo = _geo_for(pool, proxy)

        # Mail must be created before the profile seed (email is a good seed).
        mail_spec = adapter.get("mail")
        early_vars = {"service": adapter.name, "run_id": run_id, **var_overrides}
        if isinstance(mail_spec, (dict, str)) and mail_spec:
            mail_spec = render(mail_spec, early_vars)
            provider = make_provider(settings, mail_spec)
            if email:
                ptype = _provider_type(mail_spec)
                if ptype not in {"imap", "outlook_graph", "graph", "outlook"}:
                    raise ConfigError(
                        f"--email is only supported with fixed-mailbox providers "
                        f"(imap/outlook_graph); adapter uses {ptype!r}"
                    )
                mailbox = Mailbox(address=email)
            else:
                mailbox = provider.create()
            email = mailbox.address
        else:
            mailbox = None

        identity_cfg = adapter.get("identity") or {}
        if not isinstance(identity_cfg, dict):
            raise ConfigError("identity must be a mapping")
        fixed = identity_cfg.get("profile")
        if fixed:
            profile = load_profile(settings, str(fixed))
            if geo:
                align_to_geo(profile, geo)
        else:
            seed = str(identity_cfg.get("seed") or email or task)
            os_name = identity_cfg.get("os")
            profile = make_profile(
                seed, os_name=os_name, geo=geo, engine=engine,
                canvas_noise=bool(settings.get("identity.canvas_noise", False)),
            )
            for key in ("locale", "timezone"):
                if identity_cfg.get(key):
                    profile[key] = identity_cfg[key]
            if identity_cfg.get("languages"):
                profile["languages"] = list(identity_cfg["languages"])
        errors = validate_profile(profile)
        if errors:
            raise ConfigError(f"Invalid profile: {', '.join(errors)}")
        profile["engine"] = engine
        save_profile(settings, profile)
        profile_dir = settings.paths.profiles / profile["id"] / engine
        profile_dir.mkdir(parents=True, exist_ok=True)

        person = random_person()
        password = password or random_password()
        runtime_vars: dict[str, Any] = {
            "service": adapter.name,
            "run_id": run_id,
            "task": task,
            "email": email or "",
            "password": password,
            "profile_id": profile["id"],
            "proxy": proxy.safe() if proxy else "direct",
            **person,
        }
        runtime_vars.update(var_overrides)
        adapter_vars = adapter.get("vars") or {}
        if not isinstance(adapter_vars, dict):
            raise ConfigError("vars must be a mapping")
        runtime_vars.update(render(adapter_vars, runtime_vars))
        if "base_url" not in runtime_vars and runtime_vars.get("signup_url"):
            from urllib.parse import urlparse
            parsed = urlparse(str(runtime_vars["signup_url"]))
            runtime_vars["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
        result["email"] = email
        result["profile_id"] = profile["id"]
        result["proxy"] = runtime_vars["proxy"]

        attempts = int(adapter.get("attempts") or settings.get("limits.attempts", 2))
        mail_cfg = adapter.get("mail") or {}
        defer_verify = bool(mail_cfg.get("defer")) if isinstance(mail_cfg, dict) else False
        last_error: str | None = None
        # A retry must not move the baseline forward: the first attempt may
        # already have asked for a code that arrived between attempts.
        carried_ids: set[str] = set()
        carried_armed = False
        for attempt in range(1, max(1, attempts) + 1):
            if stop.is_set():
                last_error = "stopped by --stop-on-error"
                break
            artifacts = artifact_root / f"attempt-{attempt}"
            artifacts.mkdir(parents=True, exist_ok=True)
            ctx = FlowContext(
                settings=settings, adapter=adapter, store=store, run_id=run_id,
                attempt=attempt, email=email or "", password=password,
                mailbox=mailbox, mail_provider=provider, profile=profile,
                profile_dir=profile_dir, proxy=proxy, artifacts_dir=artifacts,
                engine=engine, headed=headed,
                skip_verify=no_verify or defer_verify,
                log_prefix=f"[{task} a{attempt}] ", vars=dict(runtime_vars),
                mail_ids=set(carried_ids), mail_armed=carried_armed,
            )
            try:
                flow_result = run_flow(ctx)
                status = ("verified" if flow_result.verified
                          else "unverified" if flow_result.verified is False
                          else "pending" if defer_verify
                          else "created")
                mail_session = None
                if provider is not None and mailbox is not None:
                    try:
                        mail_session = MailSession.snapshot(
                            provider, mailbox,
                            ignored_ids=ctx.mail_ids,
                            baseline_armed=ctx.mail_armed,
                        )
                    except Exception as exc:
                        # Losing the bookkeeping must not discard a finished run.
                        result["mail_session_error"] = friendly_exc(exc)
                account_id = store.upsert_account(
                    service=adapter.name, email=email or "", password=password,
                    status=status, profile_id=profile["id"],
                    proxy=proxy.safe() if proxy else None, engine=engine,
                    proxy_secret=proxy.authenticated_url if proxy else None,
                    mail_session=mail_session,
                    meta={
                        "verify_error": flow_result.verify_error,
                        "mode": adapter.mode,
                        "engine": flow_result.meta.get("engine", engine),
                        "url": flow_result.meta.get("url"),
                        "profile_id": profile["id"],
                        "vars": _safe_vars(runtime_vars),
                    },
                    artifact_dir=str(artifacts),
                )
                store.record_run(run_id=run_id, service=adapter.name, email=email or "",
                                 status="success", started_at=started_at,
                                 finished_at=now_iso(), artifact_dir=str(artifacts),
                                 meta={"attempt": attempt, "account_id": account_id,
                                       "verified": flow_result.verified})
                result.update({"ok": True, "status": status, "account_id": account_id,
                               "verify_error": flow_result.verify_error})
                return result
            except Exception as exc:
                kind = classify_error(exc)
                last_error = f"[{kind}] {friendly_exc(exc)}"
                result["kind"] = kind
                store.record_run(run_id=run_id, service=adapter.name, email=email or "",
                                 status="failed", started_at=started_at,
                                 finished_at=now_iso(), error=last_error,
                                 artifact_dir=str(artifacts),
                                 meta={"attempt": attempt, "kind": kind})
                if policy_for(kind) == "stop":
                    break
                if attempt < attempts:
                    time.sleep(min(30, 2 ** attempt))
            finally:
                carried_ids, carried_armed = set(ctx.mail_ids), ctx.mail_armed
                save_profile(settings, profile)
        result.update({"ok": False, "error": last_error or "unknown error",
                       "status": "failed"})
        return result
    except Exception as exc:
        kind = classify_error(exc)
        result.update({"ok": False, "kind": kind,
                       "error": f"[{kind}] {friendly_exc(exc)}", "status": "failed"})
        try:
            store.record_run(run_id=run_id, service=adapter.name, email=email or "",
                             status="failed", started_at=started_at,
                             finished_at=now_iso(), error=result["error"],
                             artifact_dir=str(artifact_root))
        except Exception:
            pass
        return result
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass
        store.close()


def _safe_vars(values: dict[str, Any]) -> dict[str, Any]:
    """Variables needed to re-render verify steps, without credentials."""
    blocked = {"password", "email", "otp", "otp_link", "otp_sender", "otp_subject",
               "captcha_token", "cookies", "proxy", "run_id", "task"}
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in blocked or key.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def _provider_type(spec: Any) -> str:
    if isinstance(spec, str):
        return spec.lower()
    if isinstance(spec, dict):
        return str(spec.get("type") or spec.get("provider") or "").lower()
    return ""


def _geo_for(pool: ProxyPool, proxy: Proxy | None) -> dict[str, Any] | None:
    global _DIRECT_GEO
    if proxy is None:
        with _DIRECT_LOCK:
            if _DIRECT_GEO is None:
                _DIRECT_GEO = pool.probe(None)
            return _DIRECT_GEO
    try:
        return pool.probe(proxy)
    except ProxyError:
        raise
