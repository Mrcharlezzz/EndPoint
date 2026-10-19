"""Probe L — Anti-detection patches in headless.

Probe G showed: at the Teams homepage, headed captures the Microsoft Teams
Web Client appid (5e3ce6c0-...) within 30s, but headless captures zero
Graph tokens. The "only-in-headed" diff is dominated by
``res.cdn.office.net/eduassignmentsui/*`` and ``assignments.edu.cloud.microsoft``
traffic — headless never fetches the Assignments feed at all.

Hypothesis: Teams branches on ``navigator.webdriver`` and friends to
decide whether to render the Assignments tile (and therefore whether to
mint Graph tokens). If we patch the standard headless tells, the right
token should appear without any sidebar walking.

This probe runs two headless captures at the Teams homepage:
  * **control**: vanilla headless, same as probe_g headless mode.
  * **patched**: headless + add_init_script overrides for
    ``navigator.webdriver``, ``navigator.plugins``,
    ``navigator.languages``, ``chrome.runtime``, and a
    ``permissions.query`` spoof, plus the
    ``--disable-blink-features=AutomationControlled`` launch flag.

Then it diffs Graph-token appids and reports whether the target appears.

Usage: cd src/ingestion && uv run python scripts/probes/probe_l_antidetect.py
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
    decode_jwt,
    filter_sso,
    load_cookies,
    load_creds,
    token_fingerprint,
    token_scopes,
)

TARGET_APPID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"

STEALTH_INIT_SCRIPT = r"""
// Standard puppeteer-stealth-style overrides. Each is applied before
// any page script runs, so Teams' detection code sees the patched values.

// 1. navigator.webdriver — most common tell
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. navigator.plugins — empty in headless, non-empty in real Chrome
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const fake = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' },
    ];
    fake.length = fake.length;
    return fake;
  },
});

// 3. navigator.languages — defaults to single ['en-US'] in headless
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en'],
});

// 4. chrome.runtime — missing in headless Chromium
if (!window.chrome) {
  Object.defineProperty(window, 'chrome', {
    value: { runtime: {} },
    writable: true,
  });
}

// 5. permissions.query for 'notifications' — headless returns 'denied'
//    while Notification.permission claims 'default'; mismatch is detectable.
if (navigator.permissions && navigator.permissions.query) {
  const originalQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (parameters) =>
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters);
}

// 6. WebGL vendor/renderer — headless Chrome reports SwiftShader.
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (parameter) {
  if (parameter === 37445) return 'Intel Inc.';            // UNMASKED_VENDOR_WEBGL
  if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
  return getParameter.apply(this, [parameter]);
};
"""

LAUNCH_ARGS_PATCHED: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
)


def run_once(
    cookies: list[dict[str, Any]],
    url: str,
    *,
    patched: bool,
    duration: int = 30,
) -> tuple[dict[str, int], dict[str, dict[str, Any]], list[str]]:
    """Returns (counts, by_appid, only_in_run_paths)."""
    from playwright.sync_api import sync_playwright

    counts = {"total": 0, "bearer": 0, "graph": 0, "csa": 0}
    by_appid: dict[str, dict[str, Any]] = {}
    eduassign_paths: list[str] = []
    t0 = 0.0

    def on_request(request: Any) -> None:
        counts["total"] += 1
        url_str = str(request.url)
        # Track eduassignmentsui asset fetches as a load-divergence signal.
        if "eduassignmentsui" in url_str or "assignments.edu.cloud.microsoft" in url_str:
            eduassign_paths.append(url_str[:120])
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        counts["bearer"] += 1
        token = auth[7:]
        if "/api/csa/" in url_str:
            counts["csa"] += 1
        if "graph.microsoft.com" not in url_str:
            return
        counts["graph"] += 1
        claims = decode_jwt(token) or {}
        appid = str(claims.get("appid", "")) or "<no-appid>"
        t_ms = int((time.monotonic() - t0) * 1000)
        bucket = by_appid.setdefault(
            appid,
            {
                "count": 0,
                "name": str(claims.get("app_displayname", "")),
                "first_t_ms": t_ms,
                "scope_set": set(),
                "fp_set": set(),
            },
        )
        bucket["count"] += 1
        if not bucket["name"]:
            bucket["name"] = str(claims.get("app_displayname", ""))
        bucket["scope_set"].update(token_scopes(token))
        bucket["fp_set"].add(token_fingerprint(token))

    args = list(LAUNCH_ARGS_PATCHED) if patched else []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=args)
        try:
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ) if patched else None,
            )
            if patched:
                with contextlib.suppress(Exception):
                    context.add_init_script(STEALTH_INIT_SCRIPT)
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()
            page.on("request", on_request)
            t0 = time.monotonic()
            with contextlib.suppress(Exception):
                page.goto(url, timeout=30_000)
            for _ in range(duration):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
        finally:
            browser.close()

    return counts, by_appid, eduassign_paths


def _print_appid_table(label: str, by_appid: dict[str, dict[str, Any]]) -> None:
    print(f"\n[{label}] graph-token appid breakdown ({len(by_appid)} distinct)")
    if not by_appid:
        print("  (no graph.microsoft.com tokens captured)")
        return
    header = (
        f"  {'first_t_ms':>10}  {'appid':<38}  {'app_displayname':<28}  "
        f"{'#req':>5}  {'#tok':>4}  {'#scp':>4}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for appid, b in sorted(by_appid.items(), key=lambda x: x[1]["first_t_ms"]):
        marker = "  <-- TARGET" if appid == TARGET_APPID else ""
        print(
            f"  {b['first_t_ms']:>10}  {appid:<38}  {b['name'][:28]:<28}  "
            f"{b['count']:>5}  {len(b['fp_set']):>4}  {len(b['scope_set']):>4}{marker}"
        )


def main() -> int:
    teams_creds = load_creds()
    _ = teams_creds  # ensure creds load works; fail fast otherwise
    start_url = TEAMS_URL
    cookies = filter_sso(load_cookies())

    print(f"[setup] start_url={start_url} sso_cookies={len(cookies)}")
    print(f"[setup] target_appid={TARGET_APPID} (Microsoft Teams Web Client)")
    print()

    print("[run] control (vanilla headless)...")
    t = time.monotonic()
    control_counts, control_appids, control_eduassign = run_once(
        cookies, start_url, patched=False, duration=30
    )
    print(f"  elapsed={time.monotonic() - t:.1f}s")

    print("[run] patched (anti-detection overrides + launch flag)...")
    t = time.monotonic()
    patched_counts, patched_appids, patched_eduassign = run_once(
        cookies, start_url, patched=True, duration=30
    )
    print(f"  elapsed={time.monotonic() - t:.1f}s")

    print()
    print(f"{'metric':<22} {'control':>10} {'patched':>10}")
    print("-" * 48)
    for key in ("total", "bearer", "graph", "csa"):
        print(f"{key:<22} {control_counts[key]:>10} {patched_counts[key]:>10}")
    print(
        f"{'eduassignmentsui_reqs':<22} "
        f"{len(control_eduassign):>10} {len(patched_eduassign):>10}"
    )

    _print_appid_table("CONTROL", control_appids)
    _print_appid_table("PATCHED", patched_appids)

    print()
    print(f"[appid] target_seen_control = {TARGET_APPID in control_appids}")
    print(f"[appid] target_seen_patched = {TARGET_APPID in patched_appids}")
    print()

    # First few eduassignmentsui paths from each side (signal of feed render)
    if patched_eduassign:
        print(f"[patched] sample eduassignmentsui/* paths ({len(patched_eduassign)} total):")
        for p in patched_eduassign[:6]:
            print(f"  {p}")

    if TARGET_APPID in patched_appids:
        print()
        print("=" * 72)
        print("RESULT: anti-detection patches recover the target appid in headless.")
        print("Next step: port the same overrides into")
        print("  ingestion/daemon/auth/refresh.py PlaywrightTokenRefresher.")
        return 0

    if patched_appids and not control_appids:
        print()
        print("=" * 72)
        print("PARTIAL: patches unlocked some Graph traffic but not the target.")
        print("Need additional tells (CDP overrides, HeadlessChrome UA, etc).")
        return 0

    print()
    print("=" * 72)
    print("RESULT: patches did NOT change behaviour. Either the detection is")
    print("deeper (CDP-level) or the trigger is elsewhere — try probe_l-style")
    print("sidebar walk, or a long-idle (>60s) probe.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
