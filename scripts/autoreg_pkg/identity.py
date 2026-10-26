"""Deterministic browser identities and fingerprint profiles.

The profile is the source of truth: it is generated once per account, stored
as JSON, and reused by every launch.  Camoufox gets a full generated
fingerprint (UA, screen, WebGL, fonts, media devices, noise seeds).  Chromium
engines get context options plus a small init script for the values Playwright
does not expose.

Consistency beats novelty.  A profile that claims Windows but reports `MacIntel`
is worse than a plain real browser.  Defaults therefore keep the native UA and
native platform, and only override values that are explicitly in the profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
from typing import Any

from .util import ConfigError, now_iso

PROFILE_VERSION = 2

OS_TARGETS = {"windows", "macos", "linux"}

COUNTRY_LOCALE = {
    "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", "NZ": "en-NZ",
    "IE": "en-IE", "DE": "de-DE", "AT": "de-AT", "CH": "de-CH", "FR": "fr-FR",
    "BE": "fr-BE", "NL": "nl-NL", "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT",
    "BR": "pt-BR", "PL": "pl-PL", "CZ": "cs-CZ", "SK": "sk-SK", "SE": "sv-SE",
    "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI", "RU": "ru-RU", "UA": "uk-UA",
    "TR": "tr-TR", "JP": "ja-JP", "KR": "ko-KR", "CN": "zh-CN", "TW": "zh-TW",
    "HK": "zh-HK", "SG": "en-SG", "IN": "en-IN", "ID": "id-ID", "TH": "th-TH",
    "VN": "vi-VN", "PH": "en-PH", "MY": "ms-MY", "MX": "es-MX", "AR": "es-AR",
    "CL": "es-CL", "CO": "es-CO", "ZA": "en-ZA", "AE": "en-AE", "IL": "he-IL",
    "SA": "ar-SA", "EG": "ar-EG",
}

COUNTRY_TZ = {
    "US": "America/New_York", "GB": "Europe/London", "CA": "America/Toronto",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "IE": "Europe/Dublin",
    "DE": "Europe/Berlin", "AT": "Europe/Vienna", "CH": "Europe/Zurich",
    "FR": "Europe/Paris", "BE": "Europe/Brussels", "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid", "IT": "Europe/Rome", "PT": "Europe/Lisbon",
    "BR": "America/Sao_Paulo", "PL": "Europe/Warsaw", "CZ": "Europe/Prague",
    "SK": "Europe/Bratislava", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki", "RU": "Europe/Moscow",
    "UA": "Europe/Kyiv", "TR": "Europe/Istanbul", "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul", "CN": "Asia/Shanghai", "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong", "SG": "Asia/Singapore", "IN": "Asia/Kolkata",
    "ID": "Asia/Jakarta", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila", "MY": "Asia/Kuala_Lumpur", "MX": "America/Mexico_City",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago",
    "CO": "America/Bogota", "ZA": "Africa/Johannesburg", "AE": "Asia/Dubai",
    "IL": "Asia/Jerusalem", "SA": "Asia/Riyadh", "EG": "Africa/Cairo",
}

SCREENS = {
    "windows": [
        (1920, 1080, 1.0), (1536, 864, 1.25), (1366, 768, 1.0),
        (2560, 1440, 1.0), (1440, 900, 1.0), (1680, 1050, 1.0),
        (1920, 1200, 1.0),
    ],
    "macos": [
        (1440, 900, 2.0), (1512, 982, 2.0), (1728, 1117, 2.0),
        (1920, 1080, 1.0), (2560, 1440, 2.0),
    ],
    "linux": [
        (1920, 1080, 1.0), (1366, 768, 1.0), (2560, 1440, 1.0),
        (1440, 900, 1.0),
    ],
}

GPU_CHOICES = {
    "windows": [
        ("Google Inc. (NVIDIA)",
         "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)",
         "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)",
         "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
    "macos": [
        ("Google Inc. (Apple)",
         "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
        ("Google Inc. (Apple)",
         "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)"),
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 640, OpenGL 4.1)"),
    ],
    "linux": [
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"),
        ("Google Inc. (NVIDIA)",
         "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650/PCIe/SSE2, OpenGL 4.5.0)"),
    ],
}


def host_os() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def os_platform(os_name: str) -> str:
    return {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}[os_name]


def os_ua_token(os_name: str) -> str:
    return {"windows": "Windows NT 10.0; Win64; x64",
            "macos": "Macintosh; Intel Mac OS X 10_15_7",
            "linux": "X11; Linux x86_64"}[os_name]


def _seed_int(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)


def make_profile(seed: str, *, os_name: str | None = None,
                 geo: dict[str, Any] | None = None,
                 engine: str = "auto",
                 canvas_noise: bool = False) -> dict[str, Any]:
    rng = random.Random(_seed_int(seed))
    target_os = (os_name or host_os()).lower()
    if target_os not in OS_TARGETS:
        raise ConfigError(f"Unknown target OS {target_os!r}; use one of {sorted(OS_TARGETS)}")

    width, height, dpr = rng.choice(SCREENS[target_os])
    engine_key = (engine or "auto").lower()
    # Patchright ignores add_init_script by design.  Its identity must therefore
    # describe the real Chromium values, or fpcheck will (correctly) catch the
    # lie.  Playwright applies the init script, so it can diversify.
    native_chromium = engine_key in {"patchright", "auto"}
    if engine_key == "invisible":
        # invisible_playwright has its own fingerprint engine; do not lie about
        # hardware values that it will set itself.
        profile_note = "invisible_playwright"
    if native_chromium:
        concurrency = max(2, os.cpu_count() or 8)
        avail_w, avail_h = width, height
        memory = 8
    else:
        concurrency = rng.choice([4, 6, 8, 8, 12, 16])
        memory = 8
        if target_os == "windows":
            avail_h = height - rng.choice([40, 40, 48])
        elif target_os == "macos":
            avail_h = height - 25
        else:
            avail_h = height - rng.choice([0, 24, 28])
        avail_w = width
    viewport_w = width
    viewport_h = max(600, avail_h - rng.choice([80, 88, 96]))
    # Keep a little room for browser chrome; never claim inner > screen.
    viewport_h = min(viewport_h, height - 40)

    country = (geo or {}).get("country_code") or (geo or {}).get("countryCode")
    locale = COUNTRY_LOCALE.get(str(country).upper(), "en-US") if country else "en-US"
    timezone = (geo or {}).get("timezone") or COUNTRY_TZ.get(str(country).upper()) or "UTC"
    languages = [locale] + ([locale.split("-")[0]] if "-" in locale else [])

    webgl_vendor, webgl_renderer = rng.choice(GPU_CHOICES[target_os])
    # Only Playwright gets the init script that can apply this override.
    webgl_override = ({"vendor": webgl_vendor, "renderer": webgl_renderer}
                      if engine_key == "playwright" else None)

    profile_id = "p_" + hashlib.sha256(seed.encode()).hexdigest()[:12]
    profile: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "id": profile_id,
        "seed": seed,
        "created_at": now_iso(),
        "os": target_os,
        "engine": engine,
        "engine_note": "invisible_playwright" if (engine or "").lower() == "invisible" else None,
        "locale": locale,
        "languages": languages,
        "timezone": timezone,
        "geo": geo or None,
        "screen": {
            "width": width,
            "height": height,
            "avail_width": avail_w,
            "avail_height": avail_h,
            "color_depth": 24,
            "pixel_ratio": dpr,
        },
        "viewport": {"width": viewport_w, "height": viewport_h},
        "hardware": {"concurrency": concurrency, "memory": memory},
        "webgl": webgl_override,
        "platform": os_platform(target_os),
        "ua": "native",
        "canvas_noise": bool(canvas_noise),
        "audio_noise": False,
        "webrtc": "proxy",
        "camoufox_fp": None,
    }
    return profile


def align_to_geo(profile: dict[str, Any], geo: dict[str, Any]) -> dict[str, Any]:
    country = str(geo.get("country_code") or geo.get("countryCode") or "").upper()
    if country and country in COUNTRY_LOCALE:
        locale = COUNTRY_LOCALE[country]
        profile["locale"] = locale
        profile["languages"] = [locale] + ([locale.split("-")[0]] if "-" in locale else [])
    if geo.get("timezone"):
        profile["timezone"] = geo["timezone"]
    profile["geo"] = geo
    return profile


def init_script(profile: dict[str, Any]) -> str:
    """Chromium init script.  Only values present in the profile are patched."""
    payload = {
        "platform": profile["platform"],
        "language": profile["locale"],
        "languages": profile["languages"],
        "hardwareConcurrency": profile["hardware"]["concurrency"],
        "deviceMemory": profile["hardware"]["memory"],
        "webgl": profile.get("webgl"),
        "screen": profile["screen"],
        "ua": None if profile.get("ua") in (None, "native") else profile.get("ua"),
    }
    return """(() => {
  const P = %s;
  const def = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); }
    catch (e) {}
  };
  try { def(Navigator.prototype, 'webdriver', false); } catch (e) {}
  try { def(Navigator.prototype, 'platform', P.platform); } catch (e) {}
  try { def(Navigator.prototype, 'language', P.language); } catch (e) {}
  try { def(Navigator.prototype, 'languages', Object.freeze(P.languages)); } catch (e) {}
  try { def(Navigator.prototype, 'hardwareConcurrency', P.hardwareConcurrency); } catch (e) {}
  try { if (P.deviceMemory !== null) def(Navigator.prototype, 'deviceMemory', P.deviceMemory); } catch (e) {}
  try {
    const S = Screen.prototype;
    def(S, 'width', P.screen.width); def(S, 'height', P.screen.height);
    def(S, 'availWidth', P.screen.avail_width); def(S, 'availHeight', P.screen.avail_height);
    def(S, 'colorDepth', P.screen.color_depth); def(S, 'pixelDepth', P.screen.color_depth);
  } catch (e) {}
  if (P.ua) { try { def(Navigator.prototype, 'userAgent', P.ua); } catch (e) {} }
  if (P.webgl) {
    const patch = (proto) => {
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      proto.getParameter = function (param) {
        if (param === 37445) return P.webgl.vendor;
        if (param === 37446) return P.webgl.renderer;
        return orig.apply(this, arguments);
      };
    };
    try { patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
    try { patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}
  }
})();""" % json.dumps(payload, ensure_ascii=True)


def camoufox_fingerprint(profile: dict[str, Any]) -> Any:
    """Generate a full Camoufox fingerprint for this identity.

    Returns a ``browserforge.fingerprints.Fingerprint`` object, which Camoufox
    accepts directly via ``Camoufox(fingerprint=...)``.  Also caches a dict
    view at ``profile['camoufox_fp']`` so ``compare_fingerprint`` can read the
    screen values Camoufox actually chose (it does not always honour the
    requested dimensions exactly).
    """
    try:
        from camoufox.fingerprints import generate_fingerprint
    except Exception as exc:  # pragma: no cover - optional dependency
        raise ConfigError("camoufox is not installed; use --engine patchright") from exc
    screen = profile["screen"]
    fp = generate_fingerprint(
        window=(screen["width"], screen["height"]),
        os=profile["os"],
    )
    try:
        from dataclasses import asdict
        profile["camoufox_fp"] = asdict(fp)
    except Exception:
        pass
    return fp


def validate_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if profile.get("version") != PROFILE_VERSION:
        errors.append(f"unsupported profile version {profile.get('version')}")
    if profile.get("os") not in OS_TARGETS:
        errors.append("os must be windows/macos/linux")
    if not profile.get("locale"):
        errors.append("locale is empty")
    if not profile.get("timezone"):
        errors.append("timezone is empty")
    screen = profile.get("screen") or {}
    viewport = profile.get("viewport") or {}
    for key in ("width", "height", "avail_width", "avail_height", "color_depth", "pixel_ratio"):
        if not isinstance(screen.get(key), (int, float)) or screen.get(key, 0) <= 0:
            errors.append(f"screen.{key} missing or invalid")
    if viewport.get("width", 10**9) > screen.get("width", 0):
        errors.append("viewport.width is wider than screen.width")
    if viewport.get("height", 10**9) > screen.get("height", 0):
        errors.append("viewport.height is taller than screen.height")
    hw = profile.get("hardware") or {}
    if not isinstance(hw.get("concurrency"), int) or hw.get("concurrency", 0) < 2:
        errors.append("hardware.concurrency invalid")
    return errors


def chromium_args(profile: dict[str, Any], *, proxy: bool) -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        f"--lang={profile['locale']}",
    ]
    if proxy:
        args += [
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
    return args


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Profile without the bulky Camoufox blob."""
    out = {k: v for k, v in profile.items() if k != "camoufox_fp"}
    out["has_camoufox_fp"] = bool(profile.get("camoufox_fp"))
    return out


def profile_fingerprint_check_js() -> str:
    return """(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    language: navigator.language,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    webdriver: navigator.webdriver,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory ?? null,
    screen: {
      width: screen.width, height: screen.height,
      availWidth: screen.availWidth, availHeight: screen.availHeight,
      colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth
    },
    devicePixelRatio: window.devicePixelRatio,
    inner: {width: window.innerWidth, height: window.innerHeight},
    webgl: gl ? {
      vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
      renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)
    } : null
  };
})()"""


def compare_fingerprint(profile: dict[str, Any], actual: dict[str, Any],
                        *, strict_screen: bool = True,
                        strict_hardware: bool = True) -> list[str]:
    """Return human-readable mismatches.  Native UA is not compared."""
    bad: list[str] = []
    if bool(actual.get("webdriver")):
        bad.append("navigator.webdriver is true")
    if actual.get("platform") != profile["platform"]:
        bad.append(f"platform {actual.get('platform')!r} != {profile['platform']!r}")
    if actual.get("language") != profile["locale"]:
        bad.append(f"language {actual.get('language')!r} != {profile['locale']!r}")
    if actual.get("timezone") != profile["timezone"]:
        bad.append(f"timezone {actual.get('timezone')!r} != {profile['timezone']!r}")
    if strict_hardware:
        if actual.get("hardwareConcurrency") != profile["hardware"]["concurrency"]:
            bad.append("hardwareConcurrency mismatch")
        if profile["hardware"].get("memory") and actual.get("deviceMemory") not in (None, profile["hardware"]["memory"]):
            bad.append("deviceMemory mismatch")
    screen = actual.get("screen") or {}
    if strict_screen:
        # Camoufox may adjust the exact screen it renders; its generated
        # fingerprint is the truth for that engine.
        fp_screen = (profile.get("camoufox_fp") or {}).get("screen") \
            if not strict_hardware else None
        for key in ("width", "height", "availWidth", "availHeight"):
            if fp_screen and key in fp_screen:
                want = fp_screen[key]
            else:
                want = profile["screen"][{"availWidth": "avail_width",
                                          "availHeight": "avail_height"}.get(key, key)]
            if screen.get(key) != want:
                bad.append(f"screen.{key} {screen.get(key)!r} != {want!r}")
    if strict_hardware and actual.get("devicePixelRatio") != profile["screen"]["pixel_ratio"]:
        bad.append("devicePixelRatio mismatch")
    return bad
