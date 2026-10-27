"""Re-check stored accounts using the persistent profile (or protocol cookies)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .config import Settings
from .flow import Adapter, FlowContext, _has_action, load_adapter, verify_in_browser
from .identity import validate_profile
from .mail import (MailError, MailSession, MailSessionError, Mailbox, MailTimeout,
                   make_provider)
from .proxy import parse_proxy
from .runner import load_profile, save_profile
from .store import Account, Store
from .util import ConfigError, friendly_exc

FIXED_MAIL = {"imap", "outlook_graph", "graph", "outlook"}


def _can_read_existing_mail(adapter: Adapter) -> bool:
    spec = adapter.get("mail")
    if not spec:
        return False
    if isinstance(spec, str):
        return spec.lower() in FIXED_MAIL
    if isinstance(spec, dict):
        if spec.get("create") is False:
            return True
        return str(spec.get("provider") or spec.get("type") or "").lower() in FIXED_MAIL
    return False


def _verify_needs_mail(adapter: Adapter) -> bool:
    """True when the verify block waits for an emailed code or link."""
    verify = adapter.get("verify")
    steps: list[Any] = []
    if isinstance(verify, list):
        steps = verify
    elif isinstance(verify, dict) and isinstance(verify.get("steps"), list):
        steps = verify["steps"]
    return _has_action(steps, "otp")


def _is_mail_failure(error: str | None) -> bool:
    """Verification errors that mean "mail was not available", not "bad account"."""
    return str(error or "").startswith(("MailError:", "MailTimeout:", "MailSessionError:"))


def verify_account(settings: Settings, adapter: Adapter, account: Account, *,
                   engine: str | None = None, headed: bool = False,
                   proxy_url: str | None = None,
                   store: Store | None = None) -> dict[str, Any]:
    own_store = store is None
    store = store or Store(settings)
    password = store.get_password(account.id) or ""
    mail_provider = None
    mailbox = None
    try:
        if not account.profile_id:
            raise ConfigError("account has no profile_id")
        profile = load_profile(settings, account.profile_id)
        errors = validate_profile(profile)
        if errors:
            raise ConfigError(f"invalid profile: {', '.join(errors)}")
        chosen_engine = engine if engine and engine != "auto" else (account.engine or "auto")
        if chosen_engine == "auto":
            from .browser import resolve_engine
            chosen_engine = resolve_engine(None, stealth=adapter.stealth)
        proxy_raw = proxy_url or store.get_proxy(account.id) or account.proxy or ""
        proxy_raw = str(proxy_raw).replace(" (auth)", "").strip()
        proxy = parse_proxy(proxy_raw) if proxy_raw and proxy_raw != "direct" else None
        profile_dir = settings.paths.profiles / profile["id"] / chosen_engine
        profile_dir.mkdir(parents=True, exist_ok=True)
        artifacts = settings.paths.artifacts / f"verify-{account.service}-{account.id}"
        artifacts.mkdir(parents=True, exist_ok=True)

        base_vars: dict[str, Any] = {
            "service": account.service,
            "email": account.email,
            "password": password,
            "profile_id": profile["id"],
            "run_id": f"verify-{account.id}",
        }
        base_vars.update((account.meta or {}).get("vars") or {})
        base_vars.update({k: v for k, v in (account.meta or {}).items() if k != "vars"})
        base_vars["email"] = account.email
        base_vars["password"] = password

        need_mail = _verify_needs_mail(adapter)
        stored = None
        restore_error: str | None = None
        try:
            stored = store.get_mail_session(account.id)
        except ConfigError as exc:
            restore_error = str(exc) if isinstance(exc, MailSessionError) else friendly_exc(exc)
        if stored is not None and stored.mailbox.address != account.email:
            stored = None
            restore_error = "Stored mail session does not match account"

        if stored is not None:
            try:
                mail_provider = make_provider(settings, stored.provider_config, resolved=True)
            except ConfigError as exc:
                raise MailSessionError(friendly_exc(exc)) from exc
            mailbox = Mailbox(address=stored.mailbox.address,
                              token=stored.mailbox.token,
                              meta=dict(stored.mailbox.meta))
        elif account.email and _can_read_existing_mail(adapter):
            mail_provider = make_provider(settings, adapter.get("mail"))
            mailbox = Mailbox(address=account.email)
        elif need_mail:
            # The account was registered with a temporary inbox whose token was
            # never stored.  Inventing a fresh mailbox would silently read the
            # wrong one, so say what is missing.
            raise MailSessionError(restore_error or "No stored mail session for this account")

        ctx = FlowContext(
            settings=settings, adapter=adapter, store=store,
            run_id=f"verify-{account.id}", attempt=1,
            email=account.email, password=password,
            mailbox=mailbox, mail_provider=mail_provider, profile=profile,
            profile_dir=profile_dir, proxy=proxy, artifacts_dir=artifacts,
            engine=chosen_engine, headed=headed, vars=base_vars,
            log_prefix=f"[verify {account.id}] ",
            mail_ids=set(stored.ignored_ids) if stored else set(),
            mail_armed=bool(stored.baseline_armed) if stored else False,
            # A pending account is waiting for a confirmation that may already
            # have arrived: taking a new baseline here would exclude it.
            mail_auto_arm=bool(stored is None and account.status != "pending"),
        )

        if adapter.mode == "protocol":
            from .protocol import _session, verify_protocol
            ctx.http = _session(ctx)
            try:
                verified, error = verify_protocol(ctx)
            finally:
                try:
                    ctx.http.close()
                except Exception:
                    pass
                ctx.http = None
        else:
            try:
                with BrowserSession(profile=profile, profile_dir=profile_dir,
                                    proxy=proxy, engine=chosen_engine,
                                    headed=headed) as session:
                    ctx.page = session.page
                    ctx.session = session
                    verified, error = verify_in_browser(ctx, session)
            finally:
                if profile.get("camoufox_fp") is not None:
                    save_profile(settings, profile)

        session_error: str | None = None
        if mail_provider is not None and mailbox is not None:
            try:
                store.set_mail_session(account.id, MailSession.snapshot(
                    mail_provider, mailbox,
                    ignored_ids=ctx.mail_ids, baseline_armed=ctx.mail_armed))
            except Exception as exc:
                # Bookkeeping must never turn a real result into a false one.
                session_error = friendly_exc(exc)
                print(f"[verify {account.id}] mail session not saved: {session_error}",
                      file=sys.stderr)

        def _result(ok: bool, status: str, error: str | None) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "account_id": account.id, "email": account.email,
                "ok": ok, "status": status, "error": error,
            }
            if session_error:
                payload["mail_session_error"] = session_error
            return payload

        if not verified and account.status == "pending" and _is_mail_failure(error):
            store.mark_status(account.id, "pending", verified=False, error=error)
            return _result(False, "pending", error)
        status = "verified" if verified else "invalid"
        store.mark_status(account.id, status, verified=bool(verified), error=error)
        return _result(bool(verified), status, error)
    except Exception as exc:
        error = friendly_exc(exc)
        keep = (account.status == "pending" and isinstance(exc, MailError)) or \
            isinstance(exc, MailSessionError)
        if keep:
            # Phase B ran before the confirmation mail arrived, the provider
            # hiccuped, or the stored mailbox access is gone.  None of these say
            # the account is invalid.
            try:
                store.mark_status(account.id, account.status, verified=False, error=error)
            except Exception:
                pass
            return {"account_id": account.id, "email": account.email,
                    "ok": False, "status": account.status, "error": error}
        try:
            store.mark_status(account.id, "invalid", verified=False, error=error)
        except Exception:
            pass
        return {"account_id": account.id, "email": account.email,
                "ok": False, "status": "invalid", "error": error}
    finally:
        if mail_provider is not None:
            try:
                mail_provider.close()
            except Exception:
                pass
        if own_store:
            store.close()


def verify_all(settings: Settings, adapter_path: str | Path, *,
               engine: str | None = None, headed: bool = False,
               status: str | None = None) -> list[dict[str, Any]]:
    adapter = load_adapter(adapter_path)
    store = Store(settings)
    try:
        accounts = store.list_accounts(service=adapter.name, status=status)
        results = []
        for account in accounts:
            results.append(verify_account(settings, adapter, account,
                                          engine=engine, headed=headed,
                                          store=store))
        return results
    finally:
        store.close()
