"""Protocol-mode flow: plain HTTP with browser-like TLS (curl_cffi).

Use this when the signup flow has no browser-only challenge.  It is much
faster and cheaper than a browser, but it cannot execute JS or solve an
interactive Turnstile — switch to browser mode for those targets.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .flow import FlowContext, FlowResult, _mail_opt, _retry, _run_step
from .util import ConfigError, FlowError, friendly_exc, redact


def _session(ctx: FlowContext):
    protocol = ctx.adapter.get("protocol") or {}
    impersonate = protocol.get("impersonate", "chrome")
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:  # pragma: no cover
        raise ConfigError("protocol mode requires curl_cffi") from exc
    kwargs: dict[str, Any] = {}
    if impersonate and impersonate not in {"none", False}:
        kwargs["impersonate"] = impersonate
    if ctx.proxy:
        kwargs["proxies"] = ctx.proxy.requests()
    session = curl_requests.Session(**kwargs)
    locale = (ctx.profile or {}).get("locale")
    if locale and "Accept-Language" not in session.headers:
        base = locale.split("-", 1)[0]
        session.headers["Accept-Language"] = f"{locale},{base};q=0.9,en;q=0.8"
    if protocol.get("headers"):
        session.headers.update(ctx.render(protocol["headers"]))
    return session


def run_protocol_flow(ctx: FlowContext) -> FlowResult:
    ctx.http = _session(ctx)
    verified: bool | None = None
    verify_error: str | None = None
    try:
        _arm_if_needed(ctx)
        steps = ctx.adapter.steps
        for index, raw in enumerate(steps, 1):
            from .flow import normalize_step
            step = normalize_step(raw, ctx.adapter.path, index)
            params = dict(step["params"])
            if step["timeout"] is not None:
                params["timeout"] = step["timeout"]
            params = ctx.render(params)
            ctx.log(f"[{index}/{len(steps)}] {step['action']}")
            retries, delay = _retry(params)
            try:
                _run_step(
                    lambda: execute_protocol_step(ctx, step["action"], params),
                    retries=retries, delay=delay, ctx=ctx, label=step["action"],
                )
            except Exception as exc:
                if step["optional"]:
                    ctx.log(f"  optional step failed: {friendly_exc(exc)}")
                    continue
                raise FlowError(friendly_exc(exc), step=index, action=step["action"])
        if ctx.adapter.get("verify") and not ctx.skip_verify:
            verified, verify_error = verify_protocol(ctx)
            ctx.log("verify: ok" if verified else f"verify: FAILED: {verify_error}")
        else:
            verified = None
        ctx.set_var("cookies", _cookies(ctx.http))
        return FlowResult(ok=True, verified=verified, verify_error=verify_error,
                          meta={"mode": "protocol", "cookies": _cookies(ctx.http)})
    finally:
        try:
            ctx.http.close()
        except Exception:
            pass
        ctx.http = None


def _arm_if_needed(ctx: FlowContext, *, force: bool = False) -> None:
    if not ctx.mail_provider or not ctx.mailbox:
        return
    from .flow import _has_action
    if not force:
        if not ctx.mail_auto_arm or ctx.mail_armed:
            return
        mail = ctx.adapter.get("mail")
        arm_at_start = mail.get("arm_at_start", True) if isinstance(mail, dict) else True
        if not (_has_action(ctx.adapter.steps, "otp") or arm_at_start):
            return
    try:
        ctx.mail_ids = ctx.mail_provider.snapshot_ids(ctx.mailbox)
    except Exception as exc:
        ctx.log(f"mail baseline failed (fresh box assumed): {friendly_exc(exc)}")
        ctx.mail_ids = set()
        return
    ctx.mail_armed = True
    ctx.log(f"mail baseline: {len(ctx.mail_ids)} message(s)")


def execute_protocol_step(ctx: FlowContext, action: str, params: dict[str, Any]) -> Any:
    if action == "request":
        return _request(ctx, params)
    if action == "otp":
        return _otp(ctx, params)
    if action == "assert":
        return _assert(ctx, params)
    if action == "extract":
        return _extract(ctx, params)
    if action == "set":
        if params.get("key") is not None and "value" in params:
            ctx.set_var(str(params["key"]), params["value"])
            return None
        for key, value in params.items():
            if key in {"action", "optional", "timeout", "retry"}:
                continue
            ctx.set_var(str(key), value)
        return None
    if action == "log":
        ctx.log(str(params.get("value") or params.get("message") or ""))
        return None
    if action == "sleep":
        time.sleep(float(params.get("value", params.get("seconds", 1))))
        return None
    if action == "mail_arm":
        from .flow import _has_action  # noqa: F401
        _arm_if_needed(ctx, force=True)
        return len(ctx.mail_ids)
    if action == "hook":
        from .flow import _call_hook
        return _call_hook(ctx, params)
    raise ConfigError(f"Unhandled protocol action {action!r}")


def _request(ctx: FlowContext, params: dict[str, Any]) -> dict[str, Any]:
    url = params.get("url") or params.get("value")
    if not url:
        raise ConfigError("request requires url")
    method = str(params.get("method") or "GET").upper()
    headers = dict(params.get("headers") or {})
    body_json = params.get("json")
    data = params.get("data")
    query = params.get("params") or params.get("query")
    timeout = float(params.get("timeout") or ctx.settings.timeout("step", 20))
    expect = params.get("expect")
    if isinstance(expect, int):
        expected = {expect}
    elif isinstance(expect, (list, tuple, set)):
        expected = {int(x) for x in expect}
    else:
        expected = set()
    retries = int(params.get("retries") or 0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response = ctx.http.request(
            method, str(url), headers=headers or None,
            json=body_json, data=data, params=query,
            timeout=timeout,
            allow_redirects=bool(params.get("allow_redirects", True)),
        )
        status = int(response.status_code)
        text = response.text or ""
        try:
            parsed = response.json()
        except Exception:
            parsed = None
        if expected and status not in expected:
            last_error = FlowError(
                f"{method} {url} -> HTTP {status} (expected {sorted(expected)}): {redact(text[:400])}"
            )
            if status >= 500 and attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise last_error
        if not expected and not (200 <= status < 400):
            raise FlowError(f"{method} {url} -> HTTP {status}: {redact(text[:400])}")
        result = {
            "status": status, "json": parsed, "text": text,
            "headers": dict(response.headers),
        }
        ctx.set_var("last_status", status)
        ctx.set_var("last_json", parsed)
        ctx.set_var("last_text", text)
        ctx.set_var("last_headers", dict(response.headers))
        ctx.set_var("last_response", result)
        if params.get("save"):
            ctx.set_var(str(params["save"]), parsed if parsed is not None else text)
        ctx.log(f"  HTTP {status} {method} {url}")
        return result
    raise last_error or FlowError("request failed")


def _otp(ctx: FlowContext, params: dict[str, Any]) -> Any:
    if not ctx.mail_provider or not ctx.mailbox:
        raise FlowError("otp step requires a configured mailbox")
    timeout = float(params.get("timeout") or ctx.settings.timeout("otp", 180))
    interval = float(params.get("interval") or ctx.settings.get("limits.poll_interval", 2.0))
    result = ctx.mail_provider.wait_code(
        ctx.mailbox,
        code_regex=params.get("code_regex") or params.get("pattern") or _mail_opt(ctx, "code_regex"),
        link_regex=params.get("link_regex") or _mail_opt(ctx, "link_regex"),
        sender_contains=params.get("sender_contains") or _mail_opt(ctx, "sender_contains"),
        subject_contains=params.get("subject_contains") or _mail_opt(ctx, "subject_contains"),
        timeout=timeout, interval=interval, since_ids=set(ctx.mail_ids),
    )
    ctx.mail_ids.add(result.message.id)
    ctx.set_var("otp", result.code or "")
    ctx.set_var("otp_link", result.link or "")
    ctx.log(f"mail: code={'received' if result.code else 'none'} "
            f"link={'yes' if result.link else 'no'}")
    return result.code


def _extract(ctx: FlowContext, params: dict[str, Any]) -> Any:
    name = params.get("as")
    if not name:
        raise ConfigError("extract requires 'as'")
    source = params.get("from") or "last_json"
    if source == "last_json":
        data = ctx.vars.get("last_json")
    elif source == "last_text":
        data = ctx.vars.get("last_text") or ""
    elif source == "last_response":
        data = ctx.vars.get("last_response")
    elif source in ctx.vars:
        data = ctx.vars[source]
    else:
        data = source
    if params.get("regex"):
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        match = re.search(str(params["regex"]), text, re.I | re.M)
        if not match:
            if "default" in params:
                ctx.set_var(str(name), params["default"])
                return params["default"]
            raise FlowError(f"regex did not match in {source}")
        group = int(params.get("group", 1))
        value = match.group(group) if match.groups() else match.group(0)
    elif params.get("path"):
        value = _deep(data, str(params["path"]))
        if value is None and "default" in params:
            value = params["default"]
        if value is None and params.get("required", True):
            raise FlowError(f"path {params['path']!r} not found in {source}")
    else:
        value = data
    ctx.set_var(str(name), value)
    ctx.log(f"extract {name}: {'found' if value is not None else 'missing'}")
    return value


def _assert(ctx: FlowContext, params: dict[str, Any]) -> None:
    if "status" in params:
        actual = ctx.vars.get("last_status")
        expected = params["status"]
        if isinstance(expected, list):
            if actual not in expected:
                raise FlowError(f"status {actual} not in {expected}")
        elif actual != expected:
            raise FlowError(f"status {actual} != {expected}")
    if "json_path" in params:
        actual = _deep(ctx.vars.get("last_json"), str(params["json_path"]))
        if "equals" in params and actual != params["equals"]:
            raise FlowError(f"json {params['json_path']}={actual!r} != {params['equals']!r}")
        if params.get("exists") and actual in (None, ""):
            raise FlowError(f"json {params['json_path']} is empty")
    if "text_contains" in params:
        text = str(ctx.vars.get("last_text") or "")
        if str(params["text_contains"]) not in text:
            raise FlowError(f"response text does not contain {params['text_contains']!r}")
    if "var" in params:
        actual = ctx.vars.get(str(params["var"]))
        if "equals" in params and actual != params["equals"]:
            raise FlowError(f"var {params['var']}={actual!r} != {params['equals']!r}")
        if params.get("exists") and actual in (None, ""):
            raise FlowError(f"var {params['var']} is empty")
    if not any(k in params for k in ("status", "json_path", "text_contains", "var")):
        raise ConfigError("assert needs status/json_path/text_contains/var")


def verify_protocol(ctx: FlowContext) -> tuple[bool | None, str | None]:
    verify = ctx.adapter.get("verify")
    if not verify:
        return None, None
    try:
        # A step list must render lazily: an `{{otp}}` placeholder is only
        # filled by the step that came before it.
        if isinstance(verify, list):
            if ctx.mail_provider and ctx.mailbox:
                _arm_if_needed(ctx)
            for raw in verify:
                from .flow import normalize_step
                step = normalize_step(raw, ctx.adapter.path, 0)
                execute_protocol_step(ctx, step["action"], ctx.render(step["params"]))
            return True, None
        verify = ctx.render(verify)
        if ctx.mail_provider and ctx.mailbox:
            _arm_if_needed(ctx)
        if isinstance(verify, dict) and verify.get("request"):
            req = dict(verify["request"])
            req.setdefault("expect", verify.get("expect"))
            _request(ctx, req)
            if verify.get("json_path"):
                actual = _deep(ctx.vars.get("last_json"), str(verify["json_path"]))
                if "equals" in verify and actual != verify["equals"]:
                    return False, f"verify json {verify['json_path']}={actual!r}"
            return True, None
        if isinstance(verify, dict) and verify.get("url"):
            req = {
                "url": verify["url"], "method": verify.get("method", "GET"),
                "expect": verify.get("expect", 200), "headers": verify.get("headers"),
                "params": verify.get("params"),
            }
            _request(ctx, req)
            return True, None
        return False, "verify must contain request/url or be a step list"
    except Exception as exc:
        return False, friendly_exc(exc)


def _deep(data: Any, path: str) -> Any:
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _cookies(session: Any) -> dict[str, str]:
    try:
        return dict(session.cookies.get_dict())
    except Exception:
        return {}
