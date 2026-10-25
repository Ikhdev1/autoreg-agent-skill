"""Vision-driven image captcha solving.

The pack keeps the image/audio solvers generic: any OpenAI-compatible vision
endpoint works (DashScope, OpenRouter, local vLLM/Ollama).  Use them for
grid/click challenges; a solver token still wins for Turnstile/reCAPTCHA.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import requests

from .util import AutoregError, redact


class VisionError(AutoregError):
    pass


class VisionClient:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = dict(cfg or {})
        self.base = str(cfg.get("base") or "http://127.0.0.1:16432").rstrip("/")
        self.key = cfg.get("key") or "local-vote"
        self.model = cfg.get("model") or "qwen3-vl-plus"
        self.timeout = float(cfg.get("timeout") or 60)
        self.max_tokens = int(cfg.get("max_tokens") or 64)
        self.temperature = float(cfg.get("temperature") or 0)

    def ask(self, image_bytes: bytes, prompt: str, *, proxy: Any = None) -> str:
        url = f"{self.base}/v1/chat/completions"
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        headers = {"Authorization": f"Bearer {self.key}",
                   "Content-Type": "application/json"}
        proxies = proxy.requests() if proxy is not None else None
        response = requests.post(url, json=payload, headers=headers,
                                 proxies=proxies, timeout=self.timeout)
        if response.status_code >= 400:
            raise VisionError(f"vision HTTP {response.status_code}: {redact(response.text[:300])}")
        try:
            data = response.json()
        except ValueError as exc:
            raise VisionError(f"vision non-JSON: {redact(response.text[:200])}") from exc
        try:
            return str(data["choices"][0]["message"]["content"])
        except Exception as exc:
            raise VisionError(f"vision response missing content: {redact(data)}") from exc


def parse_indices(text: str, *, max_index: int = 9) -> list[int]:
    """Parse "1,3,7" / "1-3-7" / "1 3 7" / "tiles 1 and 3" into a list."""
    if not text:
        return []
    found: list[int] = []
    for match in re.finditer(r"\b([1-9]\d*)\b", text):
        value = int(match.group(1))
        if 1 <= value <= max_index and value not in found:
            found.append(value)
    return sorted(found)


def _tile_query_js() -> str:
    return """(args) => {
      const {tileSelector, targetSelector, indices} = args;
      const tiles = [];
      const walk = (node) => {
        if (!node) return;
        if (node.shadowRoot) { walk(node.shadowRoot); }
        if (node.matches && node.matches(tileSelector)) { tiles.push(node); }
        for (const child of node.children || []) { walk(child); }
      };
      const root = targetSelector ? document.querySelector(targetSelector) : document;
      walk(root || document);
      let clicked = 0;
      for (const idx of indices) {
        const el = tiles[idx - 1];
        if (el) { el.click(); clicked++; }
      }
      return {tiles: tiles.length, clicked};
    }"""


def solve_grid(page: Any, *, target: str, prompt: str, tile_selector: str = "img",
               vision: VisionClient | None = None, proxy: Any = None,
               max_index: int = 9) -> dict[str, Any]:
    """Screenshot a grid, ask a VLM, click the selected tiles."""
    if vision is None:
        raise VisionError("solve_grid needs a vision client")
    locator = page.locator(target).first
    box = locator.bounding_box()
    if not box:
        raise VisionError(f"solve_grid: target {target!r} has no bounding box")
    image = locator.screenshot(type="png")
    answer = vision.ask(image, prompt, proxy=proxy)
    indices = parse_indices(answer, max_index=max_index)
    if not indices:
        return {"ok": False, "answer": answer, "indices": [], "clicked": 0,
                "tiles": 0, "reason": "no indices parsed"}
    result = page.evaluate(_tile_query_js(), {
        "tileSelector": tile_selector, "targetSelector": target,
        "indices": indices,
    })
    return {"ok": bool(result.get("clicked")), "answer": answer,
            "indices": indices, "clicked": result.get("clicked", 0),
            "tiles": result.get("tiles", 0)}


def click_by_vision(page: Any, *, target: str | None, description: str,
                    vision: VisionClient, proxy: Any = None,
                    timeout: float = 60.0) -> dict[str, Any]:
    """Screenshot an element/page, ask the VLM for a point, click it."""
    if target:
        locator = page.locator(target).first
        box = locator.bounding_box()
        if not box:
            raise VisionError(f"click_by_vision: target {target!r} not found")
        image = locator.screenshot(type="png")
        offset_x, offset_y = box["x"], box["y"]
    else:
        image = page.screenshot(type="png", full_page=False)
        offset_x = offset_y = 0
    prompt = (
        "Return ONLY JSON with the click point, relative to this image, in pixels: "
        '{"x": <number>, "y": <number>}. Target: ' + description
    )
    answer = vision.ask(image, prompt, proxy=proxy)
    match = re.search(r'\{\s*"x"\s*:\s*([0-9.]+)\s*,\s*"y"\s*:\s*([0-9.]+)\s*\}', answer)
    if not match:
        match = re.search(r"x\s*[:=]\s*([0-9.]+).*?y\s*[:=]\s*([0-9.]+)", answer, re.S)
    if not match:
        raise VisionError(f"click_by_vision: could not parse a point from {redact(answer)!r}")
    x = offset_x + float(match.group(1))
    y = offset_y + float(match.group(2))
    page.mouse.click(x, y)
    return {"ok": True, "x": x, "y": y, "answer": answer}


def solve_recaptcha_audio(page: Any, *, stt: dict[str, Any] | None = None,
                          timeout: float = 60.0, proxy: Any = None) -> dict[str, Any]:
    """Best-effort reCAPTCHA audio path.  Needs an STT provider."""
    if not stt or not stt.get("base"):
        raise VisionError("solve_audio_captcha needs an STT provider (stt.base/key/model)")
    frame = page.frame_locator('iframe[src*="recaptcha"]')
    try:
        frame.locator("#recaptcha-audio-button").first.click(timeout=int(timeout * 1000))
    except Exception as exc:
        raise VisionError(f"recaptcha audio button not found: {exc}") from exc
    try:
        frame.locator("#audio-response").first.wait_for(state="visible",
                                                        timeout=int(timeout * 1000))
    except Exception:
        pass
    src = frame.locator("audio").first.evaluate("el => el.src")
    if not src:
        raise VisionError("recaptcha audio src not found")
    audio = requests.get(src, timeout=30).content
    if stt.get("base"):
        url = f"{stt['base'].rstrip('/')}/v1/audio/transcriptions"
        files = {"file": ("audio.mp3", audio, "audio/mpeg")}
        data = {"model": stt.get("model") or "whisper-1"}
        headers = {"Authorization": f"Bearer {stt.get('key') or ''}"}
        response = requests.post(url, files=files, data=data, headers=headers,
                                 proxies=proxy.requests() if proxy is not None else None,
                                 timeout=float(stt.get("timeout") or 60))
        if response.status_code >= 400:
            raise VisionError(f"stt HTTP {response.status_code}: {redact(response.text[:300])}")
        text = response.json().get("text", "")
    else:
        raise VisionError("STT provider needs a base URL")
    answer = re.sub(r"[^A-Za-z0-9 ]", "", text).strip()
    frame.locator("#audio-response").first.fill(answer)
    frame.locator("#recaptcha-verify-button").first.click()
    return {"ok": True, "answer": answer, "audio_bytes": len(audio)}
