"""Probe E — Session state / degraded cookie scenarios.

Runs silent refresh under synthetic cookie degradation and records
what signal the refresh receives. The goal is to understand the spectrum
between "healthy session" and "expired session" — and whether the
login-redirect detection in PlaywrightTokenRefresher reliably fires.

Scenarios:
  * healthy         — SSO cookies as stored
  * drop_each_sso   — remove one SSO cookie at a time (report which cookie
                      is load-bearing)
  * expired_all     — rewrite every SSO cookie's `expires` to past (mimics
                      a stale session)
  * empty           — no cookies (cold browser)

For each, records: final URL, whether it redirected to a login host,
which tokens were captured, elapsed.

Usage: cd src/ingestion && uv run python scripts/probes/probe_e_session.py
"""

from __future__ import annotations

import contextlib
import copy
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    LOGIN_REDIRECT_HOSTS,
    REQUIRED_GRAPH_SCOPE,
    TEAMS_URL,
    filter_sso,
    load_cookies,
    load_creds,
    token_has_scope,
)


def run_scenario(
    cookies: list[dict[str, Any]], refresh_url: str, duration: int = 20
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {}
    final_url = ""
    t_start = time.monotonic()

    def on_request(request: Any) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        token = auth[7:]
        u: str = request.url
        if "graph.microsoft.com" in u:
            existing = captured.get("graph_token")
            if existing is None or (
                token_has_scope(token, REQUIRED_GRAPH_SCOPE)
                and not token_has_scope(existing, REQUIRED_GRAPH_SCOPE)
            ):
                captured["graph_token"] = token
        elif "/api/csa/" in u and "csa_token" not in captured:
            captured["csa_token"] = token

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            if cookies:
                with contextlib.suppress(Exception):
                    context.add_cookies(cookies)
            page = context.new_page()
            page.on("request", on_request)
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)
            for _ in range(duration):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
                if "graph_token" in captured and token_has_scope(
                    captured["graph_token"], REQUIRED_GRAPH_SCOPE
                ):
                    break
            final_url = str(getattr(page, "url", ""))
        finally:
            browser.close()

    elapsed = time.monotonic() - t_start
    login_redirect = any(host in final_url for host in LOGIN_REDIRECT_HOSTS)
    return {
        "elapsed": elapsed,
        "final_url": final_url,
        "login_redirect": login_redirect,
        "has_graph": "graph_token" in captured,
        "has_csa": "csa_token" in captured,
        "graph_scoped": token_has_scope(
            captured.get("graph_token", ""), REQUIRED_GRAPH_SCOPE
        ),
    }


def _expire_all(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    past = int(time.time()) - 3600
    out: list[dict[str, Any]] = []
    for c in cookies:
        c2 = copy.deepcopy(c)
        c2["expires"] = past
        out.append(c2)
    return out


def print_row(label: str, r: dict[str, Any]) -> None:
    tags: list[str] = []
    tags.append("graph:scoped" if r["graph_scoped"] else ("graph:homepage" if r["has_graph"] else "graph:MISS"))
    tags.append("csa:ok" if r["has_csa"] else "csa:MISS")
    if r["login_redirect"]:
        tags.append("login_redirect")
    final = r["final_url"][:70] if r["final_url"] else "-"
    print(f"{label:<36} {r['elapsed']:>6.1f}s  {' '.join(tags):<30}  {final}")


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    sso = filter_sso(load_cookies())

    print(f"[setup] sso_cookies={len(sso)} refresh_url={refresh_url}")
    print()
    print(f"{'scenario':<36} {'elapsed':>7}  {'tags':<30}  final_url")
    print("-" * 130)

    # healthy
    print_row("healthy", run_scenario(sso, refresh_url, duration=25))

    # drop each SSO cookie one at a time
    for i in range(len(sso)):
        subset = [c for j, c in enumerate(sso) if j != i]
        name = str(sso[i].get("name", "?"))[:20]
        domain = str(sso[i].get("domain", "?"))[:20]
        label = f"drop:{domain}/{name}"[:36]
        print_row(label, run_scenario(subset, refresh_url, duration=20))

    # expired_all
    print_row("expired_all", run_scenario(_expire_all(sso), refresh_url, duration=20))

    # empty
    print_row("empty", run_scenario([], refresh_url, duration=15))

    return 0


if __name__ == "__main__":
    sys.exit(main())
