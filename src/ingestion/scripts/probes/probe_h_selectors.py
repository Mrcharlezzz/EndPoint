"""Probe H — UI selector resilience.

Walks through the selectors PlaywrightTokenRefresher relies on to drive
the Assignments → Past-due flow:

  1. Sidebar toggle       ``#menurb``
  2. Assignments cascade  four fallback selectors
  3. Past-due cascade     three fallback selectors

At each stage records: which selector hit (if any), whether the bounding-box
fallback was needed, time to click, and screenshots to /tmp/probe_h_*.png.
Rerun whenever Teams ships a UI change to catch selector rot.

Usage: cd src/ingestion && uv run python scripts/probes/probe_h_selectors.py
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    TEAMS_URL,
    filter_sso,
    load_cookies,
    load_creds,
)

# Mirrors ingestion.daemon.auth.refresh.PlaywrightTokenRefresher — keep in sync.
SIDEBAR_TOGGLE = "#menurb"
ASSIGNMENTS_SELECTORS: tuple[str, ...] = (
    "button[data-tid='app-bar-2a84919f-59d8-4441-a975-2a8c2643b741']",
    "button[aria-label='Assignments']",
    "button[title='Assignments']",
    "a[aria-label='Assignments']",
)
PAST_DUE_SELECTORS: tuple[str, ...] = (
    "button:has-text('Past due')",
    "[role='button']:has-text('Past due')",
    "text=Past due",
)

SCREENSHOT_DIR = Path("/tmp")


def _screenshot(page: Any, label: str) -> None:
    path = SCREENSHOT_DIR / f"probe_h_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        print(f"  screenshot: {path}")
    except Exception as exc:
        print(f"  screenshot FAILED ({label}): {exc}")


def _try_direct_click(page: Any, selectors: tuple[str, ...], label: str) -> dict[str, Any]:
    for selector in selectors:
        started = time.monotonic()
        try:
            locator = page.locator(selector).first
            locator.click(timeout=3000)
            return {
                "hit": selector,
                "mode": "direct",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            print(f"    miss ({selector}): {type(exc).__name__}")
            continue
    # Bounding-box fallback
    for selector in selectors:
        started = time.monotonic()
        try:
            locator = page.locator(selector).first
            box = locator.bounding_box()
            if box is None:
                continue
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page.mouse.click(x, y)
            return {
                "hit": selector,
                "mode": "bounding_box",
                "x": x,
                "y": y,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            print(f"    box-miss ({selector}): {type(exc).__name__}")
            continue
    return {"hit": None, "mode": "none"}


def _click_past_due(page: Any) -> dict[str, Any]:
    """Past-due lives inside the Assignments iframe. Try all frames."""
    frames = list(getattr(page, "frames", []))
    targets: list[Any] = [*frames, page]
    for idx, target in enumerate(targets):
        for selector in PAST_DUE_SELECTORS:
            started = time.monotonic()
            try:
                locator = target.locator(selector).first
                locator.click(timeout=3000)
                return {
                    "hit": selector,
                    "frame_idx": idx,
                    "frame_url": getattr(target, "url", "<page>"),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            except Exception:
                continue
    return {"hit": None}


def main() -> int:
    from playwright.sync_api import sync_playwright

    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} refresh_url={refresh_url}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)

            # Settle after initial navigation.
            page.wait_for_timeout(5000)
            _screenshot(page, "01_initial")

            # Stage 1: sidebar toggle
            print("[stage 1] sidebar toggle")
            started = time.monotonic()
            sidebar_hit = False
            try:
                page.locator(SIDEBAR_TOGGLE).click(timeout=3000)
                sidebar_hit = True
                print(f"  hit #menurb in {int((time.monotonic() - started) * 1000)}ms")
            except Exception as exc:
                print(f"  miss #menurb: {type(exc).__name__}: {exc!s:.80}")
            page.wait_for_timeout(1000)
            _screenshot(page, "02_after_sidebar")

            # Stage 2: Assignments cascade
            print("[stage 2] Assignments cascade")
            assignments = _try_direct_click(page, ASSIGNMENTS_SELECTORS, "assignments")
            if assignments["hit"]:
                print(f"  hit ({assignments['mode']}): {assignments['hit']} in {assignments['elapsed_ms']}ms")
            else:
                print("  ALL SELECTORS FAILED")
            page.wait_for_timeout(3000)
            _screenshot(page, "03_after_assignments")

            # Stage 3: Past-due inside iframe
            print("[stage 3] Past-due cascade")
            past_due = _click_past_due(page)
            if past_due["hit"]:
                print(
                    f"  hit: {past_due['hit']} in frame#{past_due['frame_idx']} "
                    f"({past_due['frame_url']}) in {past_due['elapsed_ms']}ms"
                )
            else:
                print("  ALL SELECTORS FAILED")
            page.wait_for_timeout(2000)
            _screenshot(page, "04_after_past_due")

            frame_summary = [str(getattr(f, "url", "?"))[:80] for f in page.frames]
            print(f"\n[frames at end] {len(frame_summary)}")
            for u in frame_summary:
                print(f"  {u}")

            print("\n[summary]")
            print(f"  sidebar_toggle: {'hit' if sidebar_hit else 'miss'}")
            print(f"  assignments   : {assignments}")
            print(f"  past_due      : {past_due}")
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
