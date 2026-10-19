"""Probe A — Cookie surface.

Inventories the full cookie jar, then runs silent refresh with progressive
cookie subsets to find the minimal set that still yields a scope-qualified
graph_token and a csa_token.

Subsets tested:
  1. none          — baseline (expect no tokens)
  2. sso_only      — current daemon/CLI default
  3. sso_plus_app  — SSO + teams.microsoft.com authtoken / skypetoken
  4. full          — every cookie, unfiltered

Prints a per-subset result row: graph token captured? scoped? csa? elapsed.

Usage: cd src/ingestion && uv run python scripts/probes/probe_a_cookies.py
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    REQUIRED_GRAPH_SCOPE,
    TEAMS_URL,
    exp_remaining_minutes,
    filter_sso,
    format_cookie_row,
    group_by_domain,
    load_cookies,
    load_creds,
    token_has_scope,
)

APP_COOKIE_NAMES: set[str] = {
    "authtoken",
    "skypetoken_asm",
    "SSOAUTHCOOKIE",
    "TSREGIONCOOKIE",
}


def _make_interceptor(captured: dict[str, str]) -> Any:
    def on_request(request: Any) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        token = auth[7:]
        url: str = request.url
        if "graph.microsoft.com" in url:
            existing = captured.get("graph_token")
            if existing is None or (
                token_has_scope(token, REQUIRED_GRAPH_SCOPE)
                and not token_has_scope(existing, REQUIRED_GRAPH_SCOPE)
            ):
                captured["graph_token"] = token
        elif "/api/csa/" in url and "csa_token" not in captured:
            captured["csa_token"] = token

    return on_request


def run_refresh(cookies: list[dict[str, Any]], refresh_url: str) -> tuple[dict[str, str], float]:
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {}
    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            if cookies:
                with contextlib.suppress(Exception):
                    context.add_cookies(cookies)
            page = context.new_page()
            page.on("request", _make_interceptor(captured))
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)
            for _ in range(35):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
                if "graph_token" in captured and "csa_token" in captured:
                    if token_has_scope(captured["graph_token"], REQUIRED_GRAPH_SCOPE):
                        break
        finally:
            browser.close()
    return captured, time.monotonic() - started


def print_inventory(cookies: list[dict[str, Any]]) -> None:
    groups = group_by_domain(cookies)
    print(f"[inventory] {len(cookies)} cookies across {len(groups)} domains")
    for domain in sorted(groups):
        bucket = groups[domain]
        print(f"  {domain}  ({len(bucket)})")
        for c in sorted(bucket, key=lambda x: str(x.get("name", ""))):
            print(format_cookie_row(c))
    print()


def describe(captured: dict[str, str]) -> str:
    parts: list[str] = []
    graph = captured.get("graph_token")
    if graph:
        mins = exp_remaining_minutes(graph)
        scoped = token_has_scope(graph, REQUIRED_GRAPH_SCOPE)
        tag = "scoped" if scoped else "homepage"
        parts.append(f"graph:{tag}({mins}m)")
    else:
        parts.append("graph:MISSING")
    csa = captured.get("csa_token")
    if csa:
        mins = exp_remaining_minutes(csa)
        parts.append(f"csa:ok({mins}m)")
    else:
        parts.append("csa:MISSING")
    return " ".join(parts)


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    all_cookies = load_cookies()

    print_inventory(all_cookies)

    sso = filter_sso(all_cookies)
    app = [c for c in all_cookies if str(c.get("name", "")) in APP_COOKIE_NAMES]

    subsets: list[tuple[str, list[dict[str, Any]]]] = [
        ("none", []),
        ("sso_only", sso),
        ("sso_plus_app", sso + app),
        ("full", all_cookies),
    ]

    print(f"[setup] refresh_url={refresh_url}")
    print(f"[setup] sso_cookies={len(sso)} app_cookies={len(app)} total={len(all_cookies)}")
    print()
    print(f"{'subset':<16} {'cookies':>8} {'elapsed':>8}  result")
    print("-" * 80)

    for name, subset in subsets:
        captured, elapsed = run_refresh(subset, refresh_url)
        print(f"{name:<16} {len(subset):>8} {elapsed:>7.1f}s  {describe(captured)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
