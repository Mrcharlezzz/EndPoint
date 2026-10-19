"""Probe J — DOM discovery for current Teams UI.

Probe I revealed the daemon's Assignments selectors don't match the
current DOM, and the app host has migrated from teams.microsoft.com to
teams.cloud.microsoft. This probe inventories the live DOM so we can
pick up-to-date selectors.

For each frame (main + nested):
  * count + existence check of the current selector tuple
  * full enumeration of every button / [role=button] / a[href] with
    aria-label, title, data-tid, text (first 50 chars), visibility
  * all elements whose text or attributes contain "assign"

Also reports:
  * iframe tree with URLs
  * sidebar toggle state (did #menurb actually expand anything?)
  * attempt Assignments via ``page.get_by_role`` / ``get_by_label``
  * attempt direct deeplink ``/_#/school/assignments``

Screenshot saved to /tmp/probe_j_{01..04}.png.

Usage: cd src/ingestion && uv run python scripts/probes/probe_j_dom_discovery.py
"""

from __future__ import annotations

import contextlib
import json
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

CURRENT_ASSIGNMENTS_SELECTORS: tuple[str, ...] = (
    "button[data-tid='app-bar-2a84919f-59d8-4441-a975-2a8c2643b741']",
    "button[aria-label='Assignments']",
    "button[title='Assignments']",
    "a[aria-label='Assignments']",
)

# JS enumerator: every interactive element with relevant attrs.
ENUMERATE_JS = r"""
() => {
  const selectors = [
    'button', '[role="button"]', 'a[href]', '[role="link"]',
    '[role="tab"]', '[role="menuitem"]',
  ];
  const seen = new Set();
  const out = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const visible = rect.width > 0 && rect.height > 0
        && style.visibility !== 'hidden' && style.display !== 'none';
      out.push({
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role'),
        aria_label: el.getAttribute('aria-label'),
        title: el.getAttribute('title'),
        data_tid: el.getAttribute('data-tid'),
        id: el.id || null,
        cls: (el.className && typeof el.className === 'string')
          ? el.className.slice(0, 60) : null,
        href: el.getAttribute('href'),
        text: (el.innerText || el.textContent || '').trim().slice(0, 60),
        visible,
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        w: Math.round(rect.width),
        h: Math.round(rect.height),
      });
    }
  }
  return out;
}
"""


def _attrs_mention(entry: dict[str, Any], needle: str) -> bool:
    needle_l = needle.lower()
    for key in ("aria_label", "title", "data_tid", "text", "href", "id", "cls"):
        val = entry.get(key)
        if val and needle_l in str(val).lower():
            return True
    return False


def _dump_entries(entries: list[dict[str, Any]], label: str, limit: int = 60) -> None:
    print(f"\n[{label}] {len(entries)} candidates")
    for e in entries[:limit]:
        vis = "V" if e.get("visible") else "-"
        aria = (e.get("aria_label") or "")[:30]
        title = (e.get("title") or "")[:20]
        tid = (e.get("data_tid") or "")[:50]
        text = (e.get("text") or "").replace("\n", " ")[:30]
        coords = f"({e.get('x', '?')},{e.get('y', '?')} {e.get('w', '?')}x{e.get('h', '?')})"
        print(
            f"  {vis} {e.get('tag', '?'):<6} "
            f"aria={aria:<32} title={title:<22} tid={tid:<52} "
            f"text={text:<32} {coords}"
        )
    if len(entries) > limit:
        print(f"  ... +{len(entries) - limit} more")


def inspect_frame(frame: Any, frame_label: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (all_entries, assign_related_entries)."""
    try:
        raw = frame.evaluate(ENUMERATE_JS)
    except Exception as exc:
        print(f"  [{frame_label}] evaluate failed: {exc!s:.120}")
        return ([], [])
    entries = list(raw) if isinstance(raw, list) else []
    assign = [e for e in entries if _attrs_mention(e, "assign")]
    return entries, assign


def check_current_selectors(frame: Any, label: str) -> None:
    print(f"\n[current-selector check in {label}]")
    for sel in CURRENT_ASSIGNMENTS_SELECTORS:
        try:
            cnt = frame.locator(sel).count()
            print(f"  count={cnt:<3}  {sel}")
        except Exception as exc:
            print(f"  ERROR    {sel}: {exc!s:.80}")


def run(cookies: list[dict[str, Any]], refresh_url: str) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()

            t0 = time.monotonic()

            def ts() -> int:
                return int((time.monotonic() - t0) * 1000)

            print(f"[t={ts()}ms] goto {refresh_url}")
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)

            print(f"[t={ts()}ms] settle 8s")
            page.wait_for_timeout(8000)
            with contextlib.suppress(Exception):
                page.screenshot(path="/tmp/probe_j_01_after_goto.png", full_page=False)

            # Sidebar toggle attempt — but report whether it hit
            print(f"[t={ts()}ms] sidebar toggle")
            toggle_hit = False
            try:
                page.locator("#menurb").first.click(timeout=3000)
                toggle_hit = True
                print("  #menurb: HIT")
            except Exception as exc:
                print(f"  #menurb: MISS ({exc!s:.80})")

            # Try alternative sidebar toggles
            alt_toggles = (
                "button[aria-label='Expand navigation']",
                "button[aria-label='Collapse navigation']",
                "button[data-tid='appBarToggleButton']",
                "[data-tid='app-bar-toggle']",
                "button[title='Expand']",
            )
            for sel in alt_toggles:
                try:
                    cnt = page.locator(sel).count()
                    print(f"  alt-toggle count={cnt} {sel}")
                except Exception:
                    pass

            page.wait_for_timeout(2000)
            with contextlib.suppress(Exception):
                page.screenshot(path="/tmp/probe_j_02_after_toggle.png", full_page=False)

            # Frame tree
            frames = list(page.frames)
            print(f"\n[frame tree] {len(frames)} frame(s)")
            for i, f in enumerate(frames):
                print(f"  #{i}  {str(getattr(f, 'url', '?'))[:110]}")

            # Enumerate each frame
            all_by_frame: dict[int, list[dict[str, Any]]] = {}
            assign_by_frame: dict[int, list[dict[str, Any]]] = {}
            for i, f in enumerate(frames):
                entries, assign = inspect_frame(f, f"frame#{i}")
                all_by_frame[i] = entries
                assign_by_frame[i] = assign
                check_current_selectors(f, f"frame#{i}")

            # Assignment-related elements — high-signal
            print("\n" + "=" * 80)
            print("[assign-related elements across all frames]")
            for i, assign in assign_by_frame.items():
                if not assign:
                    continue
                _dump_entries(assign, f"frame#{i} matches", limit=40)

            # Top visible interactive elements in main frame (for context)
            main_entries = all_by_frame.get(0, [])
            visible_left_rail = [
                e for e in main_entries
                if e.get("visible") and 0 <= int(e.get("x", 999)) <= 100
            ]
            _dump_entries(
                visible_left_rail,
                "main-frame: visible elements in left 100px (sidebar candidates)",
                limit=50,
            )

            # Try page.get_by_role / get_by_label
            print("\n[high-level locator attempts]")
            attempts: list[tuple[str, Any]] = [
                ("get_by_role(button, name=Assignments)",
                 lambda: page.get_by_role("button", name="Assignments")),
                ("get_by_role(link, name=Assignments)",
                 lambda: page.get_by_role("link", name="Assignments")),
                ("get_by_label(Assignments)",
                 lambda: page.get_by_label("Assignments")),
                ("get_by_text(Assignments)",
                 lambda: page.get_by_text("Assignments")),
            ]
            for desc, factory in attempts:
                try:
                    loc = factory()
                    cnt = loc.count()
                    print(f"  count={cnt:<3}  {desc}")
                    if cnt > 0:
                        first = loc.first
                        box = first.bounding_box()
                        print(f"    first.box = {box}")
                except Exception as exc:
                    print(f"  ERROR  {desc}: {exc!s:.80}")

            # Direct deeplink attempt
            print(f"\n[t={ts()}ms] try direct Assignments deeplink")
            try:
                page.goto(
                    "https://teams.microsoft.com/_#/school/assignments",
                    timeout=15_000,
                )
                print("  deeplink navigation succeeded")
            except Exception as exc:
                print(f"  deeplink navigation failed: {exc!s:.100}")
            page.wait_for_timeout(5000)
            with contextlib.suppress(Exception):
                page.screenshot(path="/tmp/probe_j_03_after_deeplink.png", full_page=False)
            new_frames = list(page.frames)
            print(f"  frames after deeplink: {len(new_frames)}")
            for i, f in enumerate(new_frames):
                print(f"    #{i}  {str(getattr(f, 'url', '?'))[:110]}")

            # Re-inspect top frame for any new Past-due / Assignments controls
            print(f"\n[t={ts()}ms] post-deeplink: re-enumerate assign-related")
            for i, f in enumerate(new_frames):
                _entries, assign = inspect_frame(f, f"post-deeplink-frame#{i}")
                if assign:
                    _dump_entries(assign, f"post-deeplink-frame#{i}", limit=30)

            with contextlib.suppress(Exception):
                page.screenshot(path="/tmp/probe_j_04_final.png", full_page=False)

            print(f"\n[summary]")
            print(f"  sidebar_toggle_hit={toggle_hit}")
            print(f"  initial_frames={len(frames)} post_deeplink_frames={len(new_frames)}")
            total_assign = sum(len(v) for v in assign_by_frame.values())
            print(f"  total_assign_related_entries={total_assign}")
            print(
                f"  screenshots: /tmp/probe_j_01..04.png"
            )
        finally:
            browser.close()

    return 0


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} refresh_url={refresh_url}")
    print()
    return run(cookies, refresh_url)


if __name__ == "__main__":
    sys.exit(main())


# Suppress unused-import warnings for helpers kept for interactive reuse.
_ = json
