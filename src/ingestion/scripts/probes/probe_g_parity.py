"""Probe G — Headed vs headless parity.

Runs the same refresh (same URL, same cookies) twice — once headed,
once headless — and diffs the request streams. If Teams detects headless
mode and omits certain requests (or asks for narrower scopes), the diff
will surface it.

Produces two sets of (method, host, path, aud, scope_flag) tuples and
reports which requests were unique to each mode, plus a side-by-side
token-emission summary.

NOTE: the headed run pops a real Chromium window. It auto-closes after
the capture window; don't close it yourself or you'll cancel the run.

Usage: cd src/ingestion && uv run python scripts/probes/probe_g_parity.py
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    REQUIRED_GRAPH_SCOPE,
    TEAMS_URL,
    decode_jwt,
    filter_sso,
    load_cookies,
    load_creds,
    token_audience,
    token_fingerprint,
    token_has_scope,
    token_scopes,
)

# Microsoft Teams Web Client — the appid the headed CLI captures.
# Headless currently captures Assignments UI (ccb65bcd-...) instead.
TARGET_APPID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"


def _normalized_key(request: Any, token: str | None) -> tuple[str, str, str, str, str]:
    """(method, host, path_prefix, aud, scoped?) — stable key for diffing."""
    host: str = "?"
    path: str = "?"
    try:
        parsed = urlparse(str(request.url))
        host = str(parsed.netloc)
        path = str(parsed.path)[:60]
    except Exception:
        pass
    method = str(request.method)
    if token is None:
        return (method, host, path, "-", "-")
    aud = token_audience(token) or "-"
    scoped = "scoped" if token_has_scope(token, REQUIRED_GRAPH_SCOPE) else "plain"
    return (method, host, path, aud[:40], scoped)


def run_once(
    cookies: list[dict[str, Any]],
    url: str,
    *,
    headless: bool,
    duration: int = 25,
) -> tuple[
    set[tuple[str, str, str, str, str]],
    dict[str, int],
    dict[str, dict[str, Any]],
]:
    from playwright.sync_api import sync_playwright

    keys: set[tuple[str, str, str, str, str]] = set()
    counts = {"total": 0, "bearer": 0, "graph": 0, "scoped_graph": 0, "csa": 0}
    # appid -> {count, name, first_t_ms, scope_set, fp_set}
    by_appid: dict[str, dict[str, Any]] = {}
    t0 = 0.0

    def on_request(request: Any) -> None:
        counts["total"] += 1
        auth = request.headers.get("authorization", "")
        token: str | None = None
        if auth.startswith("Bearer "):
            counts["bearer"] += 1
            token = auth[7:]
            assert token is not None
            if "graph.microsoft.com" in request.url:
                counts["graph"] += 1
                if token_has_scope(token, REQUIRED_GRAPH_SCOPE):
                    counts["scoped_graph"] += 1
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
            elif "/api/csa/" in request.url:
                counts["csa"] += 1
        keys.add(_normalized_key(request, token))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
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

    return keys, counts, by_appid


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
    # Use Teams homepage (not channel refresh_url) to mirror the headed CLI flow,
    # which successfully captures the Microsoft Teams Web Client appid passively.
    start_url = TEAMS_URL
    _ = teams_creds  # kept loaded so missing-creds fails fast
    cookies = filter_sso(load_cookies())

    print(f"[setup] start_url={start_url} sso_cookies={len(cookies)}")
    print(f"[setup] target_appid={TARGET_APPID} (Microsoft Teams Web Client)")
    print()

    print("[run] headless=True...")
    t = time.monotonic()
    headless_keys, headless_counts, headless_appids = run_once(
        cookies, start_url, headless=True, duration=30
    )
    print(f"  elapsed={time.monotonic() - t:.1f}s")

    print("[run] headless=False (browser window will open — don't close it)...")
    t = time.monotonic()
    headed_keys, headed_counts, headed_appids = run_once(
        cookies, start_url, headless=False, duration=30
    )
    print(f"  elapsed={time.monotonic() - t:.1f}s")

    print()
    print(f"{'metric':<18} {'headless':>10} {'headed':>10}")
    print("-" * 45)
    for key in ("total", "bearer", "graph", "scoped_graph", "csa"):
        print(f"{key:<18} {headless_counts[key]:>10} {headed_counts[key]:>10}")

    _print_appid_table("HEADLESS", headless_appids)
    _print_appid_table("HEADED", headed_appids)

    only_appids_headed = set(headed_appids) - set(headless_appids)
    only_appids_headless = set(headless_appids) - set(headed_appids)
    print()
    print(f"[appid diff] only_headed={sorted(only_appids_headed)}")
    print(f"[appid diff] only_headless={sorted(only_appids_headless)}")
    print(f"[appid diff] target_seen_headed   = {TARGET_APPID in headed_appids}")
    print(f"[appid diff] target_seen_headless = {TARGET_APPID in headless_appids}")
    print()

    only_headless = headless_keys - headed_keys
    only_headed = headed_keys - headless_keys
    shared = headless_keys & headed_keys
    print(
        f"[diff] shared={len(shared)} only_headless={len(only_headless)} "
        f"only_headed={len(only_headed)}"
    )

    if only_headless:
        print("\n  only in HEADLESS:")
        for k in sorted(only_headless)[:40]:
            print(f"    {k[0]:<5} {k[1]:<35} {k[2]:<45} aud={k[3]:<30} {k[4]}")
        if len(only_headless) > 40:
            print(f"    ...+{len(only_headless) - 40} more")

    if only_headed:
        print("\n  only in HEADED:")
        for k in sorted(only_headed)[:40]:
            print(f"    {k[0]:<5} {k[1]:<35} {k[2]:<45} aud={k[3]:<30} {k[4]}")
        if len(only_headed) > 40:
            print(f"    ...+{len(only_headed) - 40} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
