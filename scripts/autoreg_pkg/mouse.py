"""Humanized mouse movement over Playwright's trusted CDP input.

Playwright's `page.mouse` dispatches real CDP input events (isTrusted=true).
The helpers add curved paths, jitter and dwell so sliders, press-and-hold and
coordinate clicks do not look like a straight teleport.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any, Iterable


def _bezier(x1: float, y1: float, x2: float, y2: float, *,
            steps: int, jitter: float, rng: random.Random) -> list[tuple[float, float]]:
    """Cubic Bezier with a perpendicular offset on the control points."""
    dx = x2 - x1
    dy = y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    nx = -dy / dist
    ny = dx / dist
    bend = rng.uniform(-0.35, 0.35) * dist * 0.35
    cx1 = x1 + dx * 0.33 + nx * bend
    cy1 = y1 + dy * 0.33 + ny * bend
    cx2 = x1 + dx * 0.66 - nx * bend * 0.7
    cy2 = y1 + dy * 0.66 - ny * bend * 0.7
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        # ease in-out so the cursor accelerates and decelerates
        e = t * t * (3 - 2 * t)
        x = (1 - e) ** 3 * x1 + 3 * (1 - e) ** 2 * e * cx1 + 3 * (1 - e) * e ** 2 * cx2 + e ** 3 * x2
        y = (1 - e) ** 3 * y1 + 3 * (1 - e) ** 2 * e * cy1 + 3 * (1 - e) * e ** 2 * cy2 + e ** 3 * y2
        x += rng.uniform(-jitter, jitter)
        y += rng.uniform(-jitter, jitter)
        points.append((x, y))
    points[-1] = (x2, y2)
    return points


def _point(page: Any, params: dict[str, Any]) -> tuple[float, float]:
    if "x" in params and "y" in params:
        return float(params["x"]), float(params["y"])
    target = params.get("target") or params.get("locator") or params.get("selector")
    if target is None:
        raise ValueError("mouse action needs x/y or target")
    locator = page.locator(target).first
    box = locator.bounding_box()
    if not box:
        raise ValueError("target has no bounding box")
    rng = random.SystemRandom()
    return (box["x"] + box["width"] * rng.uniform(0.35, 0.65),
            box["y"] + box["height"] * rng.uniform(0.35, 0.65))


def human_mouse_move(page: Any, x: float, y: float, *, steps: int = 25,
                     jitter: float = 1.5, seed: int | None = None) -> None:
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    current = page.mouse
    try:
        start = page.evaluate("() => [window.__autoreg_mx || 0, window.__autoreg_my || 0]")
    except Exception:
        current = None
        start = (0, 0)
    path = _bezier(start[0], start[1], x, y, steps=steps, jitter=jitter, rng=rng)
    for px, py in path:
        page.mouse.move(px, py)
        time.sleep(rng.uniform(0.002, 0.008))
    try:
        page.evaluate("([x, y]) => { window.__autoreg_mx = x; window.__autoreg_my = y; }", [x, y])
    except Exception:
        pass


def human_click(page: Any, x: float, y: float, *, delay_ms: tuple[int, int] = (45, 120)) -> None:
    human_mouse_move(page, x, y)
    page.mouse.down()
    time.sleep(random.SystemRandom().uniform(*delay_ms) / 1000)
    page.mouse.up()


def human_drag(page: Any, x1: float, y1: float, x2: float, y2: float, *,
               duration_ms: int = 1200, steps: int = 40) -> None:
    rng = random.SystemRandom()
    human_mouse_move(page, x1, y1, steps=max(10, steps // 3))
    page.mouse.down()
    path = _bezier(x1, y1, x2, y2, steps=steps, jitter=1.2, rng=rng)
    total = max(1, duration_ms) / len(path)
    for px, py in path:
        page.mouse.move(px, py)
        time.sleep(total / 1000)
    page.mouse.up()


def press_hold(page: Any, x: float, y: float, *, duration_ms: int = 2500,
               tremor_px: float = 1.2) -> None:
    """Press-and-hold with a small circular tremor (Microsoft-style buttons)."""
    rng = random.SystemRandom()
    human_mouse_move(page, x, y)
    page.mouse.down()
    end = time.time() + duration_ms / 1000
    while time.time() < end:
        angle = rng.uniform(0, math.pi * 2)
        r = rng.uniform(0.3, tremor_px)
        page.mouse.move(x + math.cos(angle) * r, y + math.sin(angle) * r)
        time.sleep(rng.uniform(0.035, 0.07))
    page.mouse.up()
