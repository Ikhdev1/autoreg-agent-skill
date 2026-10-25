"""Browser launch with persistent, consistent identities.

Engine policy:
- `camoufox`  : Gecko anti-detect browser.  Best for fingerprint-sensitive
                targets.  Gets a full generated fingerprint per profile.
- `patchright`: patched Chromium (undetected Playwright fork).
- `playwright`: stock Chromium, last resort.

All engines use a persistent `user_data_dir`, one per profile, so cookies and
local storage survive between the registration and the verification run.
"""
from __future__ import annotations

import importlib.util
import platform
from pathlib import Path
from typing import Any

from .identity import (camoufox_fingerprint, chromium_args, compare_fingerprint,
                       init_script, profile_fingerprint_check_js)
from .proxy import Proxy
from .util import AutoregError, ConfigError, redact

ENGINES = ("auto", "camoufox", "patchright", "playwright", "invisible")


class BrowserError(AutoregError):
    pass


def engine_available(engine: str) -> tuple[bool, str]:
    if engine == "camoufox":
        if not importlib.util.find_spec("camoufox"):
            return False, "camoufox package not installed"
        try:
            # Do not call camoufox_path/installed_verstr: they can trigger the
            # multiversion cleanup.  Just look for the binary under INSTALL_DIR.
            from camoufox.pkgman import INSTALL_DIR
            root = Path(INSTALL_DIR)
            candidates = sorted(root.glob("browsers/official/*/camoufox.exe"))
            candidates.append(root / "camoufox.exe")
            candidates += sorted(root.glob("*/camoufox.exe"))
            for candidate in candidates:
                if candidate.is_file():
                    detail = "camoufox " + candidate.parent.name
                    version_file = candidate.parent / "version.json"
                    if not version_file.is_file() and candidate.parent == root:
                        version_file = root / "version.json"
                    if version_file.is_file():
                        try:
                            import json as _json
                            data = _json.loads(version_file.read_text(encoding="utf-8"))
                            version = f"{data.get('version', '')} {data.get('release', '')}".strip()
                            if version:
                                detail = "camoufox " + version
                        except Exception:
                            pass
                    return True, detail
            return False, "camoufox browser not fetched (run: python -m camoufox fetch)"
        except Exception as exc:
            return False, f"camoufox check failed: {redact(exc)}"
    if engine == "patchright":
        return (importlib.util.find_spec("patchright") is not None,
                "patchright installed" if importlib.util.find_spec("patchright") else "patchright not installed")
    if engine == "playwright":
        return (importlib.util.find_spec("playwright") is not None,
                "playwright installed" if importlib.util.find_spec("playwright") else "playwright not installed")
    if engine == "invisible":
        if not importlib.util.find_spec("invisible_playwright"):
            return False, "invisible_playwright not installed (pip install invisible-playwright)"
        try:
            from invisible_playwright import ensure_binary
            path = ensure_binary(progress=None, status=None)
            return True, f"invisible_playwright {path.name}"
        except Exception as exc:
            return False, f"invisible_playwright binary not ready: {redact(exc)}"
    if engine == "auto":
        for candidate in ("invisible", "camoufox", "patchright", "playwright"):
            ok, _ = engine_available(candidate)
            if ok:
                return True, f"auto -> {candidate}"
        return False, "no browser engine available"
    return False, f"unknown engine {engine!r}"


def resolve_engine(engine: str | None, *, stealth: bool = False) -> str:
    engine = (engine or "auto").lower()
    if engine not in ENGINES:
        raise ConfigError(f"Unknown engine {engine!r}; use {', '.join(ENGINES)}")
    if engine == "auto":
        if stealth:
            ok, _ = engine_available("camoufox")
            if ok:
                return "camoufox"
        for candidate in ("patchright", "camoufox", "playwright"):
            ok, _ = engine_available(candidate)
            if ok:
                return candidate
        raise BrowserError("No browser engine available. Install patchright or camoufox.")
    ok, detail = engine_available(engine)
    if not ok:
        raise BrowserError(f"Engine {engine} is unavailable: {detail}")
    return engine


class BrowserSession:
    def __init__(self, *, profile: dict[str, Any], profile_dir: Path,
                 proxy: Proxy | None = None, engine: str | None = None,
                 headed: bool = False, stealth: bool = False,
                 har_path: Path | None = None, humanize: bool = True,
                 timeout_step: float = 20.0, timeout_nav: float = 45.0):
        self.profile = profile
        self.profile_dir = Path(profile_dir)
        self.proxy = proxy
        self.engine = resolve_engine(engine, stealth=stealth)
        self.headed = headed
        self.har_path = har_path
        self.humanize = humanize
        self.timeout_step = timeout_step
        self.timeout_nav = timeout_nav
        self.context: Any = None
        self.page: Any = None
        self._cf_cm: Any = None
        self._pw: Any = None
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self.engine == "camoufox":
            self._start_camoufox()
        elif self.engine == "invisible":
            self._start_invisible()
        else:
            self._start_chromium()
        pages = getattr(self.context, "pages", []) or []
        self.page = pages[0] if pages else self.context.new_page()
        self.page.set_default_timeout(int(self.timeout_step * 1000))
        self.page.set_default_navigation_timeout(int(self.timeout_nav * 1000))
        return self

    def _start_camoufox(self) -> None:
        from camoufox.sync_api import Camoufox
        opts: dict[str, Any] = {
            "headless": not self.headed,
            "persistent_context": True,
            "user_data_dir": str(self.profile_dir),
            "os": self.profile["os"],
            "locale": self.profile["locale"],
            "timezone_id": self.profile["timezone"],
            "humanize": self.humanize,
            "block_webrtc": bool(self.proxy),
            "enable_cache": True,
            "i_know_what_im_doing": True,
            "fingerprint": camoufox_fingerprint(self.profile),
            "window": (self.profile["viewport"]["width"], self.profile["viewport"]["height"]),
        }
        if self.proxy:
            opts["proxy"] = self.proxy.playwright()
        self._cf_cm = Camoufox(**opts)
        self.context = self._cf_cm.__enter__()

    def _start_invisible(self) -> None:
        from invisible_playwright import InvisiblePlaywright
        opts: dict[str, Any] = {
            "headless": not self.headed,
            "profile_dir": str(self.profile_dir),
            "locale": self.profile["locale"],
            "timezone": self.profile["timezone"],
            "humanize": self.humanize,
        }
        if self.proxy:
            opts["proxy"] = self.proxy.playwright()
        self._cf_cm = InvisiblePlaywright(**opts)
        self.context = self._cf_cm.__enter__()

    def _start_chromium(self) -> None:
        from .identity import host_os
        if self.profile["os"] != host_os() and self.profile.get("ua") in (None, "native"):
            raise BrowserError(
                f"Chromium cannot convincingly claim {self.profile['os']} on a {host_os()} host "
                "without a matching UA. Use --engine camoufox for cross-OS identities, "
                "or set identity.ua explicitly."
            )
        if self.engine == "patchright":
            from patchright.sync_api import sync_playwright
        else:
            from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        browser_type = self._pw.chromium
        screen = self.profile["screen"]
        viewport = self.profile["viewport"]
        opts: dict[str, Any] = {
            "headless": not self.headed,
            "locale": self.profile["locale"],
            "timezone_id": self.profile["timezone"],
            "viewport": {"width": viewport["width"], "height": viewport["height"]},
            "screen": {"width": screen["width"], "height": screen["height"]},
            "device_scale_factor": screen["pixel_ratio"],
            "color_scheme": "light",
            "reduced_motion": "no-preference",
            "forced_colors": "none",
            "is_mobile": False,
            "has_touch": False,
            "args": chromium_args(self.profile, proxy=bool(self.proxy)),
            "ignore_default_args": ["--enable-automation"],
        }
        if self.profile.get("ua") not in (None, "native"):
            opts["user_agent"] = self.profile["ua"]
        if self.proxy:
            opts["proxy"] = self.proxy.playwright()
        if self.har_path:
            opts["record_har_path"] = str(self.har_path)
            opts["record_har_content"] = "embed"
            opts["record_har_mode"] = "full"
        candidates: list[dict[str, Any]] = [
            {"channel": "chromium"}, {}, {"channel": "chrome"},
        ]
        if self.engine == "playwright":
            exe = _find_playwright_chromium()
            if exe:
                candidates.append({"executable_path": str(exe)})
        last: Exception | None = None
        for extra in candidates:
            try:
                launch_opts = dict(opts)
                launch_opts.update(extra)
                self.context = browser_type.launch_persistent_context(
                    str(self.profile_dir), **launch_opts)
                break
            except Exception as exc:
                last = exc
                self.context = None
        if self.context is None:
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            raise BrowserError(f"Chromium launch failed: {redact(last)}")
        # Patchright deliberately ignores add_init_script; its profile is built
        # from native values instead.  Playwright applies the patches.
        if self.engine != "patchright":
            self.context.add_init_script(init_script(self.profile))
        # Init scripts only apply to pages created afterwards.  The persistent
        # context opens an initial about:blank page during launch; drop it so
        # the page used by the flow gets the patches.
        for old in list(getattr(self.context, "pages", []) or []):
            try:
                old.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cf_cm is not None:
            try:
                self._cf_cm.__exit__(None, None, None)
            except Exception:
                pass
        elif self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- helpers ---------------------------------------------------------
    def goto(self, url: str, *, wait_until: str = "domcontentloaded",
             timeout: float | None = None) -> Any:
        return self.page.goto(url, wait_until=wait_until,
                              timeout=int((timeout or self.timeout_nav) * 1000))

    def screenshot(self, path: Path, *, full_page: bool = True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), full_page=full_page)
        except Exception:
            pass
        return path

    def content(self) -> str:
        try:
            return self.page.content()
        except Exception:
            return ""

    def actual_fingerprint(self) -> dict[str, Any]:
        return self.page.evaluate(profile_fingerprint_check_js())


def fpcheck(settings: Any, profile: dict[str, Any], *, engine: str = "auto",
            headed: bool = False, proxy: Proxy | None = None,
            profile_dir: Path | None = None) -> tuple[dict[str, Any], list[str], str]:
    """Launch, read the fingerprint from inside the page, compare to profile."""
    directory = profile_dir or (settings.paths.profiles / (profile["id"] + "-fpcheck"))
    with BrowserSession(profile=profile, profile_dir=directory, proxy=proxy,
                        engine=engine, headed=headed, humanize=False) as session:
        session.page.goto("data:text/html,<html><body style='font:14px sans-serif'>autoreg fp</body></html>")
        actual = session.actual_fingerprint()
        mismatches = compare_fingerprint(
            profile, actual, strict_hardware=(session.engine != "camoufox"))
        return actual, mismatches, session.engine


def system_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "engines": {},
    }
    for engine in ("camoufox", "patchright", "playwright"):
        ok, detail = engine_available(engine)
        report["engines"][engine] = {"available": ok, "detail": detail}
    return report


def _find_playwright_chromium() -> Path | None:
    """Playwright 1.61 expects build 1228; a newer cached build can still run."""
    import glob
    import os
    base = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    matches = sorted(glob.glob(base + r"\chromium-*\chrome-win64\chrome.exe"))
    return Path(matches[-1]) if matches else None
