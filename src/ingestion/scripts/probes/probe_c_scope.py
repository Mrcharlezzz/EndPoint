"""Probe C — Scope elevation under different navigation paths.

For each candidate entry point, opens a FRESH browser context with the
same SSO cookie set and records which Graph scopes appeared (the
"first-seen" scope per request URL) and whether ChannelMessage.Read.All
ever shows up — without any follow-up clicks. Tests the premise that
the URL alone (not a UI interaction) can elevate the scope.

Targets probed:
  * homepage                   https://teams.microsoft.com
  * saved refresh_url          from credentials.json
  * activity feed              /_#/activity
  * chat                       /_#/chats
  * assignments root           /_#/school/assignments
  * calendar                   /_#/calendar

Usage: cd src/ingestion && uv run python scripts/probes/probe_c_scope.py
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
    load_cookies,
    load_creds,
    token_has_scope,
    token_scopes,
)

CANDIDATES: list[tuple[str, str]] = [
    ("homepage", TEAMS_URL),
    ("activity", "https://teams.microsoft.com/_#/activity"),
    ("chat", "https://teams.microsoft.com/_#/chats"),
    ("assignments_root", "https://teams.microsoft.com/_#/school/assignments"),
    ("calendar", "https://teams.microsoft.com/_#/calendar"),
]


def probe_one(cookies: list[dict[str, Any]], target_url: str, duration: int = 25) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    graph_tokens_seen: list[tuple[int, str]] = []  # (t_ms, token)
    t_first_scoped: int | None = None
    t0 = 0.0

    def on_request(request: Any) -> None:
        nonlocal t_first_scoped
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or "graph.microsoft.com" not in request.url:
            return
        token = auth[7:]
        t_ms = int((time.monotonic() - t0) * 1000)
        graph_tokens_seen.append((t_ms, token))
        if t_first_scoped is None and token_has_scope(token, REQUIRED_GRAPH_SCOPE):
            t_first_scoped = t_ms

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()
            page.on("request", on_request)
            t0 = time.monotonic()
            with contextlib.suppress(Exception):
                page.goto(target_url, timeout=30_000)
            for _ in range(duration):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
        finally:
            browser.close()

    all_scopes: set[str] = set()
    for _, tok in graph_tokens_seen:
        all_scopes.update(token_scopes(tok))
    best = graph_tokens_seen[-1][1] if graph_tokens_seen else ""
    return {
        "graph_request_count": len(graph_tokens_seen),
        "first_graph_ms": graph_tokens_seen[0][0] if graph_tokens_seen else None,
        "first_scoped_ms": t_first_scoped,
        "all_scopes_seen": sorted(all_scopes),
        "last_token_exp_min": exp_remaining_minutes(best) if best else None,
        "last_token_scoped": token_has_scope(best, REQUIRED_GRAPH_SCOPE) if best else False,
    }


def main() -> int:
    teams_creds = load_creds()
    saved_url = teams_creds.get("refresh_url")
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)}")
    print()

    targets = list(CANDIDATES)
    if saved_url:
        targets.insert(1, ("saved_refresh_url", saved_url))

    print(f"{'target':<20} {'#graph':>6} {'first_ms':>9} {'scoped_ms':>10}  last  scopes_seen")
    print("-" * 100)

    for name, url in targets:
        r = probe_one(cookies, url, duration=25)
        first = r["first_graph_ms"] if r["first_graph_ms"] is not None else "-"
        scoped = r["first_scoped_ms"] if r["first_scoped_ms"] is not None else "-"
        last_tag = "scoped" if r["last_token_scoped"] else "homepage"
        scopes_preview = ",".join(r["all_scopes_seen"][:6])
        if len(r["all_scopes_seen"]) > 6:
            scopes_preview += f",+{len(r['all_scopes_seen']) - 6}"
        print(
            f"{name:<20} {r['graph_request_count']:>6} {first!s:>9} {scoped!s:>10}  "
            f"{last_tag:<8}  {scopes_preview}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
