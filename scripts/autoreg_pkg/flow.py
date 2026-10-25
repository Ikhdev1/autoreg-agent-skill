"""Declarative browser flow engine.

An adapter is a YAML file with ordered steps.  Each step is either compact
(`- click: "button[type=submit]"`) or explicit (`- action: click, target: ...`).
The engine renders `{{variables}}` immediately before each step, so an `otp`
step can populate `{{otp}}` for the next `fill`.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import captcha as captcha_mod
from .browser import BrowserSession
from .config import Settings
from .mail import MailError, Mailbox, MailProvider
from .proxy import Proxy
from .util import (ConfigError, FlowError, deep_get, friendly_exc, load_yaml,
                   parse_bool, redact, render)

BROWSER_ACTIONS = {
    "goto", "wait_for", "wait_url", "fill", "type", "click", "press",
    "check", "uncheck", "select", "browser_fetch", "otp", "captcha",
    "sleep", "eval", "extract", "assert", "screenshot", "log", "set",
    "mail_arm", "hook",
    "mouse_move", "mouse_click", "mouse_drag", "press_hold",
    "fill_native", "fill_code",
    "solve_image_captcha", "solve_audio_captcha", "solve_slider",
    "click_by_vision",
}
PROTOCOL_ACTIONS = {
    "request", "otp", "assert", "extract", "set", "log", "sleep",
    "mail_arm", "hook",
}
ALL_ACTIONS = BROWSER_ACTIONS | PROTOCOL_ACTIONS


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------

def load_adapter(path: str | Path) -> "Adapter":
    p = Path(path)
    if p.is_dir():
        for name in ("adapter.yaml", "adapter.yml", "service.yaml"):
            if (p / name).is_file():
                p = p / name
                break
        else:
            candidates = sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml"))
            if not candidates:
                raise ConfigError(f"No adapter YAML in {p}")
            p = candidates[0]
    data = load_yaml(p)
    adapter = Adapter(path=p, data=data)
    adapter.validate()
    return adapter


@dataclass
class Adapter:
    path: Path
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.path.stem)

    @property
    def mode(self) -> str:
        mode = str(self.data.get("mode") or "browser").lower()
        if mode not in {"browser", "protocol"}:
            raise ConfigError(f"{self.path}: mode must be browser or protocol")
        return mode

    @property
    def stealth(self) -> bool:
        return parse_bool(self.data.get("stealth"), default=False)

    @property
    def engine(self) -> str | None:
        return self.data.get("engine")

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.data.get("steps") or [])

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        if not isinstance(self.data.get("name"), str) or not self.data.get("name"):
            raise ConfigError(f"{self.path}: missing string field 'name'")
        steps = self.data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f"{self.path}: 'steps' must be a non-empty list")
        allowed = PROTOCOL_ACTIONS if self.mode == "protocol" else BROWSER_ACTIONS
        for index, raw in enumerate(steps, 1):
            if not isinstance(raw, dict):
                raise ConfigError(f"{self.path}: step {index} must be a mapping")
            action = _step_action(raw, self.path, index)
            if action not in ALL_ACTIONS:
                raise ConfigError(
                    f"{self.path}: step {index} has unknown action {action!r}. "
                    f"Known: {', '.join(sorted(ALL_ACTIONS))}"
                )
            if action not in allowed:
                raise ConfigError(
                    f"{self.path}: step {index} action {action!r} is not valid in {self.mode} mode"
                )
        mail = self.data.get("mail")
        if self.mode == "browser" and _has_action(steps, "otp") and not mail:
            raise ConfigError(f"{self.path}: an 'otp' step requires a 'mail' section")
        verify = self.data.get("verify")
        if verify is not None and not isinstance(verify, (dict, list)):
            raise ConfigError(f"{self.path}: 'verify' must be a mapping or list")


def _step_action(raw: dict[str, Any], path: Path, index: int) -> str:
    if "action" in raw:
        return str(raw["action"])
    keys = [k for k in raw if k in ALL_ACTIONS]
    if len(keys) != 1:
        raise ConfigError(
            f"{path}: step {index} must contain exactly one action key "
            f"(found {keys or 'none'}); use `action:` for explicit form"
        )
    return keys[0]


def _has_action(steps: list[dict[str, Any]], action: str) -> bool:
    for raw in steps:
        try:
            if _step_action(raw, Path("?"), 0) == action:
                return True
        except ConfigError:
            continue
    return False


def normalize_step(raw: dict[str, Any], path: Path, index: int) -> dict[str, Any]:
    """Return {action, params, optional, timeout, retry, as}."""
    action = _step_action(raw, path, index)
    meta_keys = {"action", "optional", "timeout", "retry", "as", "params"}
    if "action" in raw:
        params = {k: v for k, v in raw.items() if k not in meta_keys}
        if isinstance(raw.get("params"), dict):
            params.update(raw["params"])
    else:
        value = raw[action]
        if isinstance(value, dict):
            params = dict(value)
        elif action in {"fill"}:
            raise ConfigError(f"{path}: step {index} fill needs {{target, value}}")
        else:
            params = {"value": value}
    return {
        "action": action,
        "params": params,
        "optional": parse_bool(raw.get("optional"), False),
        "timeout": raw.get("timeout"),
        "retry": raw.get("retry"),
        "as": raw.get("as"),
    }


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------

@dataclass
class FlowContext:
    settings: Settings
    adapter: Adapter
    store: Any
    run_id: str
    attempt: int
    email: str
    password: str | None
    mailbox: Mailbox | None
    mail_provider: MailProvider | None
    profile: dict[str, Any]
    profile_dir: Path
    proxy: Proxy | None
    artifacts_dir: Path
    engine: str | None = None
    headed: bool = False
    skip_verify: bool = False
    log_prefix: str = ""
    vars: dict[str, Any] = field(default_factory=dict)
    mail_ids: set[str] = field(default_factory=set)
    mail_armed: bool = False
    # A context that inherited its baseline (restored session, or a verify of a
    # pending account) must not take a fresh one: the mail it is waiting for may
    # already be in the inbox.
    mail_auto_arm: bool = True
    page: Any = None
    session: Any = None
    http: Any = None
    _hooks: dict[str, Callable[..., Any]] | None = None
    log_lines: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        line = redact(message)
        self.log_lines.append(line)
        print(f"{self.log_prefix}{line}", file=sys.stderr, flush=True)

    def render(self, value: Any) -> Any:
        return render(value, self.vars)

    def set_var(self, key: str, value: Any) -> None:
        self.vars[key] = value

    def artifact(self, name: str) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self.artifacts_dir / name

    def record(self, event: dict[str, Any]) -> None:
        if self.store is not None:
            try:
                self.store.record_run(
                    run_id=self.run_id, service=self.adapter.name,
                    email=self.email, status=event.get("status", "event"),
                    started_at=event.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    finished_at=event.get("finished_at"),
                    error=event.get("error"),
                    artifact_dir=str(self.artifacts_dir),
                    meta=event.get("meta") or {},
                )
            except Exception:
                pass

    def load_hooks(self) -> dict[str, Callable[..., Any]]:
        if self._hooks is not None:
            return self._hooks
        spec = self.adapter.get("hooks")
        hooks: dict[str, Callable[..., Any]] = {}
        if isinstance(spec, str):
            hook_path = (self.adapter.path.parent / spec).resolve()
            if hook_path.is_file():
                module_name = "autoreg_hooks_" + re.sub(r"\W+", "_", self.adapter.name)
                module_spec = importlib.util.spec_from_file_location(module_name, hook_path)
                if module_spec and module_spec.loader:
                    module = importlib.util.module_from_spec(module_spec)
                    module_spec.loader.exec_module(module)
                    for name, obj in vars(module).items():
                        if callable(obj) and not name.startswith("_"):
                            hooks[name] = obj
        elif isinstance(spec, dict):
            raise ConfigError("hooks must be a path string to a Python file")
        self._hooks = hooks
        return hooks


@dataclass
class FlowResult:
    ok: bool
    verified: bool | None
    verify_error: str | None
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# locator / steps
# --------------------------------------------------------------------------

def resolve_locator(page: Any, target: Any, *, strict: bool = False) -> Any:
    if isinstance(target, str):
        locator = page.locator(target)
    elif isinstance(target, dict):
        if target.get("role"):
            kwargs = {}
            if target.get("name") is not None:
                kwargs["name"] = target["name"]
            if target.get("exact") is not None:
                kwargs["exact"] = bool(target["exact"])
            locator = page.get_by_role(target["role"], **kwargs)
        elif target.get("label"):
            locator = page.get_by_label(target["label"], exact=bool(target.get("exact", False)))
        elif target.get("placeholder"):
            locator = page.get_by_placeholder(target["placeholder"], exact=bool(target.get("exact", False)))
        elif target.get("text"):
            locator = page.get_by_text(target["text"], exact=bool(target.get("exact", False)))
        elif target.get("test_id"):
            locator = page.get_by_test_id(target["test_id"])
        elif target.get("css"):
            locator = page.locator(target["css"])
        elif target.get("xpath"):
            locator = page.locator("xpath=" + target["xpath"])
        else:
            raise ConfigError(f"Unsupported locator mapping: {target}")
        if target.get("nth") is not None:
            locator = locator.nth(int(target["nth"]))
        elif target.get("last"):
            locator = locator.last
        elif not strict:
            locator = locator.first
    else:
        raise ConfigError(f"target must be a string or mapping, got {type(target).__name__}")
    return locator


def _target(params: dict[str, Any]) -> Any:
    target = (params.get("target") or params.get("locator")
              or params.get("selector"))
    if target is None and isinstance(params.get("value"), (str, dict)):
        target = params["value"]
    if target is None:
        raise ConfigError("step requires 'target' (CSS/Playwright selector or locator mapping)")
    return target


def _timeout_ms(params: dict[str, Any], ctx: FlowContext) -> int:
    value = params.get("timeout")
    if value is None:
        value = ctx.settings.timeout("step", 20)
    return int(float(value) * 1000)


def _retry(params: dict[str, Any]) -> tuple[int, float]:
    value = params.get("retry")
    if value is None:
        return 0, 0.0
    if isinstance(value, int):
        return value, 1.0
    if isinstance(value, dict):
        return int(value.get("times", 0)), float(value.get("delay", 1.0))
    return 0, 0.0


def _run_step(fn: Callable[[], Any], *, retries: int, delay: float,
              ctx: FlowContext, label: str) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt < retries:
                ctx.log(f"  retry {attempt + 1}/{retries} after {label}: {friendly_exc(exc)}")
                time.sleep(delay)
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# browser execution
# --------------------------------------------------------------------------

def run_flow(ctx: FlowContext) -> FlowResult:
    if ctx.adapter.mode == "protocol":
        from .protocol import run_protocol_flow
        return run_protocol_flow(ctx)
    return run_browser_flow(ctx)


def run_browser_flow(ctx: FlowContext) -> FlowResult:
    session = BrowserSession(
        profile=ctx.profile,
        profile_dir=ctx.profile_dir,
        proxy=ctx.proxy,
        engine=ctx.engine,
        headed=ctx.headed,
        stealth=ctx.adapter.stealth,
        humanize=bool(ctx.settings.get("identity.humanize", True)),
        timeout_step=ctx.settings.timeout("step", 20),
        timeout_nav=ctx.settings.timeout("nav", 45),
    )
    ctx.session = session
    verified: bool | None = None
    verify_error: str | None = None
    final_url: str | None = None
    try:
        with session:
            ctx.page = session.page
            _arm_if_needed(ctx)
            steps = ctx.adapter.steps
            try:
                for index, raw in enumerate(steps, 1):
                    step = normalize_step(raw, ctx.adapter.path, index)
                    params = dict(step["params"])
                    if step["timeout"] is not None:
                        params["timeout"] = step["timeout"]
                    params = ctx.render(params)
                    ctx.log(f"[{index}/{len(steps)}] {step['action']}")
                    started = time.time()
                    try:
                        _run_step(
                            lambda: execute_browser_step(ctx, step["action"], params),
                            retries=_retry(params)[0],
                            delay=_retry(params)[1],
                            ctx=ctx, label=step["action"],
                        )
                    except Exception as exc:
                        if step["optional"]:
                            ctx.log(f"  optional step failed: {friendly_exc(exc)}")
                            continue
                        raise FlowError(friendly_exc(exc), step=index, action=step["action"])
                    if time.time() - started > 15:
                        ctx.log(f"  slow step: {time.time() - started:.1f}s")
                if ctx.adapter.get("verify") and not ctx.skip_verify:
                    verified, verify_error = verify_in_browser(ctx, session)
                    if verified:
                        ctx.log("verify: ok")
                    else:
                        ctx.log(f"verify: FAILED: {verify_error}")
                final_url = ctx.page.url
            except Exception as exc:
                _capture_failure(ctx, session, exc)
                raise
        return FlowResult(ok=True, verified=verified, verify_error=verify_error,
                          meta={"engine": session.engine, "url": final_url})
    finally:
        ctx.session = None


def _arm_if_needed(ctx: FlowContext, *, force: bool = False) -> None:
    """Freeze the set of messages that existed before the code was requested.

    An automatic arm must stay a one-shot decision: re-arming after the mail
    arrived (for example at the start of verify) would swallow the very
    message the flow is waiting for.  `force` is the deliberate `mail_arm`
    action, which is the only way to move the baseline forward.
    """
    if not ctx.mail_provider or not ctx.mailbox:
        return
    if not force:
        if not ctx.mail_auto_arm or ctx.mail_armed:
            return
        mail = ctx.adapter.get("mail")
        arm_at_start = parse_bool(mail.get("arm_at_start"), True) \
            if isinstance(mail, dict) else True
        if not (_has_action(ctx.adapter.steps, "otp") or arm_at_start):
            return
    try:
        ctx.mail_ids = ctx.mail_provider.snapshot_ids(ctx.mailbox)
    except MailError as exc:
        ctx.log(f"mail baseline failed (fresh box assumed): {friendly_exc(exc)}")
        ctx.mail_ids = set()
        return
    ctx.mail_armed = True
    ctx.log(f"mail baseline: {len(ctx.mail_ids)} message(s)")


def _capture_failure(ctx: FlowContext, session: BrowserSession, exc: Exception) -> None:
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    try:
        session.screenshot(ctx.artifact("failure.png"), full_page=True)
    except Exception:
        pass
    try:
        ctx.artifact("failure.html").write_text(session.content(), encoding="utf-8")
    except Exception:
        pass
    payload = {
        "error": redact(exc), "url": getattr(ctx.page, "url", None),
        "step": getattr(exc, "step", None), "action": getattr(exc, "action", None),
        "email": ctx.email, "profile": ctx.profile.get("id"),
        "proxy": ctx.proxy.safe() if ctx.proxy else "direct",
    }
    try:
        ctx.artifact("failure.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                                encoding="utf-8")
    except Exception:
        pass


def execute_browser_step(ctx: FlowContext, action: str, params: dict[str, Any]) -> Any:
    page = ctx.page
    if page is None:
        raise FlowError("browser page is not available")
    timeout = _timeout_ms(params, ctx)

    if action == "goto":
        url = params.get("url") or params.get("value")
        if not url:
            raise ConfigError("goto requires url/value")
        wait_until = params.get("wait_until") or "domcontentloaded"
        return page.goto(str(url), wait_until=wait_until, timeout=timeout)

    if action == "wait_for":
        target = _target(params)
        state = params.get("state") or "visible"
        locator = resolve_locator(page, target)
        return locator.wait_for(state=state, timeout=timeout)

    if action == "wait_url":
        pattern = params.get("url") or params.get("value")
        return page.wait_for_url(pattern, timeout=timeout)

    if action == "fill":
        target = _target(params)
        value = params.get("value", params.get("text"))
        if value is None:
            raise ConfigError("fill requires value")
        locator = resolve_locator(page, target)
        if params.get("human"):
            locator.click(timeout=timeout)
            if hasattr(locator, "press_sequentially"):
                locator.press_sequentially(str(value), delay=int(params.get("delay", 40)))
            else:
                locator.type(str(value), delay=int(params.get("delay", 40)))
        else:
            locator.fill(str(value), timeout=timeout)
        return None

    if action == "type":
        target = _target(params)
        text = params.get("text", params.get("value"))
        locator = resolve_locator(page, target)
        locator.click(timeout=timeout)
        if hasattr(locator, "press_sequentially"):
            locator.press_sequentially(str(text), delay=int(params.get("delay", 40)))
        else:
            locator.type(str(text), delay=int(params.get("delay", 40)))
        return None

    if action == "click":
        target = _target(params)
        locator = resolve_locator(page, target)
        if params.get("human"):
            from .mouse import human_click
            box = locator.bounding_box()
            if not box:
                raise FlowError("click: target has no bounding box")
            human_click(page, box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2)
            return None
        kwargs: dict[str, Any] = {"timeout": timeout}
        if params.get("button"):
            kwargs["button"] = params["button"]
        if params.get("click_count"):
            kwargs["click_count"] = int(params["click_count"])
        return locator.click(**kwargs)

    if action == "press":
        target = _target(params)
        key = params.get("key") or params.get("value")
        return resolve_locator(page, target).press(str(key), timeout=timeout)

    if action == "check":
        return resolve_locator(page, _target(params)).check(timeout=timeout)

    if action == "uncheck":
        return resolve_locator(page, _target(params)).uncheck(timeout=timeout)

    if action == "select":
        target = _target(params)
        value = params.get("value")
        if isinstance(value, list):
            return resolve_locator(page, target).select_option(value, timeout=timeout)
        return resolve_locator(page, target).select_option(str(value), timeout=timeout)

    if action in {"mouse_move", "mouse_click", "mouse_drag", "press_hold",
                  "fill_native", "fill_code"}:
        return _do_mouse(ctx, action, params)

    if action == "solve_image_captcha":
        return _do_solve_image(ctx, params)
    if action == "solve_audio_captcha":
        return _do_solve_audio(ctx, params)
    if action == "solve_slider":
        return _do_solve_slider(ctx, params)
    if action == "click_by_vision":
        return _do_click_by_vision(ctx, params)

    if action == "browser_fetch":
        return _browser_fetch(ctx, params)

    if action == "otp":
        return _do_otp(ctx, params)

    if action == "captcha":
        return _do_captcha(ctx, params)

    if action == "sleep":
        seconds = float(params.get("value", params.get("seconds", 1)))
        time.sleep(seconds)
        return None

    if action == "eval":
        js = params.get("js") or params.get("value")
        if not js:
            raise ConfigError("eval requires js/value")
        result = page.evaluate(str(js), params.get("args"))
        if params.get("as"):
            ctx.set_var(str(params["as"]), result)
        return result

    if action == "extract":
        return _do_extract(ctx, params)

    if action == "assert":
        return _do_assert(ctx, params)

    if action == "screenshot":
        name = params.get("name") or params.get("value") or f"screenshot-{int(time.time())}.png"
        if not str(name).lower().endswith(".png"):
            name = str(name) + ".png"
        path = ctx.artifact(str(name))
        page.screenshot(path=str(path), full_page=bool(params.get("full_page", True)))
        return str(path)

    if action == "log":
        ctx.log(str(params.get("value") or params.get("message") or ""))
        return None

    if action == "set":
        if params.get("key") is not None and "value" in params:
            ctx.set_var(str(params["key"]), params["value"])
            return None
        values = params.get("values") if isinstance(params.get("values"), dict) else params
        for key, value in values.items():
            if key in {"action", "target", "optional", "timeout", "retry"}:
                continue
            ctx.set_var(str(key), value)
        return None

    if action == "mail_arm":
        _arm_if_needed(ctx, force=True)
        return len(ctx.mail_ids)

    if action == "hook":
        return _call_hook(ctx, params)

    raise ConfigError(f"Unhandled browser action {action!r}")


def _browser_fetch(ctx: FlowContext, params: dict[str, Any]) -> dict[str, Any]:
    """Run fetch() inside the page — needed when direct HTTP is Cloudflare-blocked.

    This is the browser-context equivalent of protocol `request`: it uses the
    page's cookies, sessionStorage and origin, and stores the same `last_*`
    variables for `extract`/`assert`.
    """
    url = params.get("url") or params.get("value")
    if not url:
        raise ConfigError("browser_fetch requires url")
    method = str(params.get("method") or "GET").upper()
    headers = dict(params.get("headers") or {})
    body_json = params.get("json")
    body_text = params.get("body", params.get("data"))
    timeout = float(params.get("timeout") or ctx.settings.timeout("step", 20))
    expect = params.get("expect")
    if isinstance(expect, int):
        expected = {expect}
    elif isinstance(expect, (list, tuple, set)):
        expected = {int(x) for x in expect}
    else:
        expected = set()
    result = ctx.page.evaluate("""async (args) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), args.timeout * 1000);
      try {
        const opts = {method: args.method, headers: args.headers,
                      credentials: args.credentials, signal: controller.signal};
        if (args.json !== null) {
          opts.headers = {...opts.headers, 'Content-Type': 'application/json'};
          opts.body = JSON.stringify(args.json);
        } else if (args.body !== null) {
          opts.body = args.body;
        }
        const r = await fetch(args.url, opts);
        const text = await r.text();
        let json = null;
        try { json = JSON.parse(text); } catch (e) {}
        const headers = {};
        r.headers.forEach((v, k) => { headers[k] = v; });
        return {status: r.status, text, json, headers};
      } finally { clearTimeout(timer); }
    }""", {
        "url": str(url), "method": method, "headers": headers,
        "json": body_json, "body": body_text,
        "credentials": params.get("credentials") or "include",
        "timeout": timeout,
    }) or {}
    status = int(result.get("status") or 0)
    if expected and status not in expected:
        raise FlowError(f"{method} {url} -> HTTP {status} (expected {sorted(expected)}): "
                        f"{redact((result.get('text') or '')[:400])}")
    if not expected and not (200 <= status < 400):
        raise FlowError(f"{method} {url} -> HTTP {status}: "
                        f"{redact((result.get('text') or '')[:400])}")
    ctx.set_var("last_status", status)
    ctx.set_var("last_json", result.get("json"))
    ctx.set_var("last_text", result.get("text"))
    ctx.set_var("last_headers", result.get("headers"))
    ctx.set_var("last_response", result)
    if params.get("save"):
        ctx.set_var(str(params["save"]),
                    result.get("json") if result.get("json") is not None else result.get("text"))
    ctx.log(f"  FETCH {status} {method} {url}")
    return result


def _vision_client(ctx: FlowContext):
    from .vision import VisionClient
    spec = ctx.render(ctx.adapter.get("vision") or {})
    if isinstance(spec, dict):
        name = spec.get("provider") or None
        overrides = {k: v for k, v in spec.items() if k != "provider"}
    else:
        name = None
        overrides = {}
    cfg = ctx.settings.provider("vision", name, overrides)
    return VisionClient(cfg)


def _stt_config(ctx: FlowContext) -> dict[str, Any]:
    spec = ctx.render(ctx.adapter.get("stt") or {})
    if isinstance(spec, dict):
        name = spec.get("provider") or None
        overrides = {k: v for k, v in spec.items() if k != "provider"}
    else:
        name = None
        overrides = {}
    return ctx.settings.provider("stt", name, overrides)


def _do_solve_image(ctx: FlowContext, params: dict[str, Any]) -> Any:
    from .vision import solve_grid
    target = params.get("target") or params.get("selector")
    if not target:
        raise ConfigError("solve_image_captcha requires target (CSS selector)")
    prompt = (params.get("prompt") or params.get("value")
              or "Select the tiles described by the prompt. Answer with tile numbers, 1-based.")
    tile_selector = params.get("tile_selector") or "img"
    max_index = int(params.get("max_index", 9))
    result = solve_grid(ctx.page, target=target, prompt=prompt,
                        tile_selector=tile_selector,
                        vision=_vision_client(ctx), proxy=ctx.proxy,
                        max_index=max_index)
    ctx.set_var("vision_result", result)
    if not result.get("ok"):
        raise FlowError(f"solve_image_captcha failed: {result.get('reason') or 'no tiles clicked'}")
    return result


def _do_solve_audio(ctx: FlowContext, params: dict[str, Any]) -> Any:
    from .vision import solve_recaptcha_audio
    result = solve_recaptcha_audio(
        ctx.page, stt=_stt_config(ctx),
        timeout=float(params.get("timeout") or ctx.settings.timeout("captcha", 180)),
        proxy=ctx.proxy,
    )
    return result


def _do_click_by_vision(ctx: FlowContext, params: dict[str, Any]) -> Any:
    from .vision import click_by_vision
    target = params.get("target") or params.get("selector")
    description = (params.get("description") or params.get("value")
                   or params.get("prompt") or "the main action button")
    result = click_by_vision(
        ctx.page, target=target, description=description,
        vision=_vision_client(ctx), proxy=ctx.proxy,
        timeout=float(params.get("timeout") or ctx.settings.timeout("step", 20)),
    )
    ctx.set_var("vision_click", result)
    ctx.log(f"click_by_vision: ({result['x']:.0f}, {result['y']:.0f})")
    return result


def _do_solve_slider(ctx: FlowContext, params: dict[str, Any]) -> Any:
    hooks = ctx.load_hooks()
    if "solve_slider" in hooks:
        return _call_hook(ctx, {"name": "solve_slider", **params})
    raise ConfigError(
        "solve_slider needs a hook: implement gap detection + drag in hooks.py"
    )


def _do_mouse(ctx: FlowContext, action: str, params: dict[str, Any]) -> Any:
    """Mouse actions use page.mouse (trusted CDP input)."""
    from . import mouse
    page = ctx.page

    def point_from(params: dict[str, Any], *, prefix: str = "") -> tuple[float, float]:
        xkey = prefix + "x"
        ykey = prefix + "y"
        if xkey in params and ykey in params:
            return float(params[xkey]), float(params[ykey])
        target = params.get(prefix + "target") or params.get("target") \
            or params.get(prefix + "locator") or params.get(prefix + "selector")
        if target is None:
            raise ConfigError(f"{action}: needs {prefix}x/{prefix}y or {prefix}target")
        locator = resolve_locator(page, target)
        box = locator.bounding_box()
        if not box:
            raise FlowError(f"{action}: target has no bounding box")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    if action == "mouse_move":
        x, y = point_from(params)
        mouse.human_mouse_move(page, x, y, steps=int(params.get("steps", 25)),
                               jitter=float(params.get("jitter", 1.5)))
        return None
    if action == "mouse_click":
        x, y = point_from(params)
        mouse.human_click(page, x, y)
        return None
    if action == "mouse_drag":
        x1, y1 = point_from(params)
        if "to_x" in params and "to_y" in params:
            x2, y2 = float(params["to_x"]), float(params["to_y"])
        else:
            x2, y2 = point_from({**params, "target": params.get("to")})
        mouse.human_drag(page, x1, y1, x2, y2,
                         duration_ms=int(params.get("duration_ms", 1200)),
                         steps=int(params.get("steps", 40)))
        return None
    if action == "press_hold":
        x, y = point_from(params)
        mouse.press_hold(page, x, y,
                         duration_ms=int(params.get("duration_ms", 2500)),
                         tremor_px=float(params.get("tremor_px", 1.2)))
        return None
    if action == "fill_native":
        target = _target(params)
        value = params.get("value", params.get("text"))
        selector = target if isinstance(target, str) else None
        if selector is None:
            raise ConfigError("fill_native needs a CSS selector target")
        page.evaluate("""(args) => {
          const el = document.querySelector(args[0]);
          if (!el) throw new Error('fill_native: not found');
          const proto = Object.getPrototypeOf(el);
          const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')
              && Object.getOwnPropertyDescriptor(proto, 'value').set;
          if (setter) { setter.call(el, args[1]); }
          else { el.value = args[1]; }
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", [selector, value])
        return None
    if action == "fill_code":
        target = params.get("target") or params.get("selector") or params.get("value")
        value = params.get("value") or params.get("text") or ctx.vars.get("otp")
        digits = [d for d in str(value or "") if d.isdigit()]
        cells = page.locator('input[maxlength="1"]:visible').all()
        if digits and len(cells) >= len(digits):
            for el, digit in zip(cells, digits):
                el.click()
                el.type(digit, delay=int(params.get("delay", 60)))
            return None
        if target:
            locator = resolve_locator(page, target)
            locator.fill(str(value), timeout=int(params.get("timeout", 20)) * 1000)
            return None
        raise FlowError("fill_code: no single-cell inputs and no target")
    raise ConfigError(f"Unhandled mouse action {action!r}")


def _do_otp(ctx: FlowContext, params: dict[str, Any]) -> Any:
    if not ctx.mail_provider or not ctx.mailbox:
        raise FlowError("otp step requires a configured mailbox")
    timeout = float(params.get("timeout") or ctx.settings.timeout("otp", 180))
    interval = float(params.get("interval") or ctx.settings.get("limits.poll_interval", 2.0))
    code_regex = params.get("code_regex") or params.get("pattern") or _mail_opt(ctx, "code_regex")
    link_regex = params.get("link_regex") or _mail_opt(ctx, "link_regex")
    result = ctx.mail_provider.wait_code(
        ctx.mailbox,
        code_regex=code_regex,
        link_regex=link_regex,
        sender_contains=params.get("sender_contains") or _mail_opt(ctx, "sender_contains"),
        subject_contains=params.get("subject_contains") or _mail_opt(ctx, "subject_contains"),
        timeout=timeout, interval=interval,
        since_ids=set(ctx.mail_ids),
    )
    ctx.mail_ids.add(result.message.id)
    ctx.set_var("otp", result.code or "")
    ctx.set_var("otp_link", result.link or "")
    ctx.set_var("otp_sender", result.message.sender)
    ctx.set_var("otp_subject", result.message.subject)
    ctx.log(f"mail: code={'received' if result.code else 'none'} "
            f"link={'yes' if result.link else 'no'}")
    target = params.get("target")
    if target and result.code:
        resolve_locator(ctx.page, target).fill(result.code, timeout=_timeout_ms(params, ctx))
    elif result.link and params.get("follow_link", True):
        ctx.page.goto(result.link, wait_until="domcontentloaded",
                      timeout=int(ctx.settings.timeout("nav", 45) * 1000))
    return result.code


def _mail_opt(ctx: FlowContext, key: str) -> Any:
    mail = ctx.adapter.get("mail")
    return mail.get(key) if isinstance(mail, dict) else None


def _do_captcha(ctx: FlowContext, params: dict[str, Any]) -> Any:
    adapter_spec = ctx.adapter.get("captcha") or {}
    if isinstance(adapter_spec, str):
        adapter_spec = {"provider": adapter_spec}
    if isinstance(adapter_spec, dict):
        adapter_spec = ctx.render(adapter_spec)
    merged = dict(adapter_spec) if isinstance(adapter_spec, dict) else {}
    merged.update(params)
    provider = str(merged.get("provider") or ctx.settings.get("captcha.default") or "none")
    kind = str(merged.get("captcha_type") or merged.get("kind") or merged.get("type") or "turnstile")
    if provider in {"none", "off"}:
        if merged.get("click"):
            _click_captcha_widget(ctx)
        return None
    if provider == "manual":
        if merged.get("click", True):
            _click_captcha_widget(ctx)
        token = captcha_mod.wait_for_token(
            ctx.page, kind, float(merged.get("timeout") or ctx.settings.timeout("captcha", 180)))
        return token
    merged.setdefault("kind", kind)
    spec = captcha_mod.spec_from(ctx.settings, merged, page_url=ctx.page.url)
    token = captcha_mod.solve(ctx.settings, spec, ctx.page, proxy=ctx.proxy)
    if token:
        ok = captcha_mod.inject(ctx.page, token, spec.type, spec.callback)
        ctx.log(f"captcha: {spec.type} token injected ({'field' if ok else 'callback'})")
        ctx.set_var("captcha_token", token)
    return token


def _click_captcha_widget(ctx: FlowContext) -> None:
    page = ctx.page
    for selector in ('.cf-turnstile', '.h-captcha', '.g-recaptcha', 'iframe[src*="challenges.cloudflare.com"]'):
        try:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=8000)
                return
        except Exception:
            continue
    ctx.log("captcha: no clickable widget found")


def _do_extract(ctx: FlowContext, params: dict[str, Any]) -> Any:
    name = params.get("as")
    if not name:
        raise ConfigError("extract requires 'as'")
    value: Any = None
    if params.get("js"):
        value = ctx.page.evaluate(str(params["js"]), params.get("args"))
    elif params.get("path"):
        source = params.get("from") or "last_json"
        data = ctx.vars.get(str(source), source) if isinstance(source, str) else source
        value = deep_get(data, str(params["path"]), params.get("default"))
        if value is None and params.get("required", True):
            raise FlowError(f"path {params['path']!r} not found in {source}")
    elif params.get("selector") or params.get("target"):
        locator = resolve_locator(ctx.page, _target(params))
        if params.get("attr"):
            value = locator.get_attribute(str(params["attr"]))
        else:
            value = locator.inner_text()
    elif params.get("regex"):
        source = params.get("from") or "page"
        if source == "page":
            text = ctx.page.content()
        elif source in ctx.vars:
            text = str(ctx.vars[source])
        else:
            text = str(source)
        match = re.search(str(params["regex"]), text, re.I | re.M)
        if match:
            group = int(params.get("group", 1))
            value = match.group(group) if match.groups() else match.group(0)
    else:
        raise ConfigError("extract needs js, selector/target or regex")
    ctx.set_var(str(name), value)
    ctx.log(f"extract {name}: {'found' if value is not None else 'missing'}")
    return value


def _do_assert(ctx: FlowContext, params: dict[str, Any]) -> None:
    timeout = _timeout_ms(params, ctx)
    compact = params.get("value")
    if compact is not None and not any(k in params for k in
                                      ("selector", "target", "text", "url_contains", "js", "var")):
        if isinstance(compact, str) and compact.startswith("text="):
            params = {**params, "text": compact[5:]}
        else:
            params = {**params, "selector": compact}
    if params.get("selector") or params.get("target"):
        locator = resolve_locator(ctx.page, _target(params))
        if params.get("visible") is False:
            locator.wait_for(state="hidden", timeout=timeout)
        else:
            locator.wait_for(state="visible", timeout=timeout)
        return None
    if params.get("text") is not None:
        needle = str(params["text"])
        body = ctx.page.inner_text("body")
        if params.get("equals"):
            if body.strip() != needle:
                raise FlowError(f"page text != {needle!r}")
        elif needle not in body:
            raise FlowError(f"page text does not contain {needle!r}")
        return None
    if params.get("url_contains"):
        needle = str(params["url_contains"])
        if needle not in ctx.page.url:
            raise FlowError(f"url {ctx.page.url!r} does not contain {needle!r}")
        return None
    if params.get("js"):
        result = ctx.page.evaluate(str(params["js"]), params.get("args"))
        if params.get("truthy", True) and not result:
            raise FlowError(f"JS assertion failed: {params['js']}")
        return None
    if params.get("var"):
        name = str(params["var"])
        actual = ctx.vars.get(name)
        if "equals" in params and actual != params["equals"]:
            raise FlowError(f"var {name}={actual!r} != {params['equals']!r}")
        if "contains" in params and str(params["contains"]) not in str(actual):
            raise FlowError(f"var {name}={actual!r} does not contain {params['contains']!r}")
        if params.get("exists") and actual in (None, ""):
            raise FlowError(f"var {name} is empty")
        return None
    raise ConfigError("assert needs selector/text/url_contains/js/var")


def _call_hook(ctx: FlowContext, params: dict[str, Any]) -> Any:
    name = params.get("name") or params.get("value")
    if not name:
        raise ConfigError("hook requires name")
    hooks = ctx.load_hooks()
    fn = hooks.get(str(name))
    if not fn:
        raise ConfigError(f"hook {name!r} not found (available: {', '.join(sorted(hooks)) or 'none'})")
    try:
        count = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        count = 1
    result = fn(ctx, ctx.page) if count >= 2 else fn(ctx)
    if isinstance(result, dict):
        for key, value in result.items():
            ctx.set_var(str(key), value)
    return result


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify_in_browser(ctx: FlowContext, session: BrowserSession) -> tuple[bool | None, str | None]:
    verify = ctx.adapter.get("verify")
    if not verify:
        return None, None  # type: ignore[return-value]
    try:
        if ctx.mail_provider and ctx.mailbox:
            _arm_if_needed(ctx)
        if isinstance(verify, list):
            for index, raw in enumerate(verify, 1):
                step = normalize_step(raw, ctx.adapter.path, index)
                params = ctx.render(step["params"])
                execute_browser_step(ctx, step["action"], params)
            return True, None
        if not isinstance(verify, dict):
            return False, "verify must be a mapping or list"
        timeout = float(verify.get("timeout") or ctx.settings.timeout("step", 20))
        url = verify.get("url")
        if url:
            session.page.goto(str(ctx.render(url)), wait_until="domcontentloaded",
                              timeout=int(ctx.settings.timeout("nav", 45) * 1000))
        if verify.get("steps"):
            for index, raw in enumerate(verify["steps"], 1):
                step = normalize_step(raw, ctx.adapter.path, index)
                params = ctx.render(step["params"])
                execute_browser_step(ctx, step["action"], params)
        if verify.get("selector"):
            resolve_locator(session.page, ctx.render(verify["selector"])).wait_for(
                state="visible", timeout=int(timeout * 1000))
        if verify.get("text"):
            body = session.page.inner_text("body")
            if str(verify["text"]) not in body:
                return False, f"text {verify['text']!r} not found"
        if verify.get("url_contains"):
            if str(verify["url_contains"]) not in session.page.url:
                return False, f"url does not contain {verify['url_contains']!r}"
        if verify.get("js"):
            result = session.page.evaluate(str(verify["js"]))
            if verify.get("truthy", True) and not result:
                return False, "JS verify returned false"
        return True, None
    except Exception as exc:
        return False, friendly_exc(exc)
