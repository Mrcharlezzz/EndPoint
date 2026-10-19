"""Probe B — Token emission inventory.

Logs EVERY Bearer-bearing request captured during a single silent refresh:
timestamp (ms since navigation), method, host+path, aud, scp count,
scp flags for key scopes, exp remaining, fingerprint.

No filtering on URL. Reveals tokens we currently discard and whether
authsvc / loki / other endpoints carry distinct tokens from graph + csa.

Output:
  * Chronological line per Bearer request.
  * Summary grouped by audience with unique fingerprint counts.

Usage: cd src/ingestion && uv run python scripts/probes/probe_b_tokens.py
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections import defaultdict
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
    short_url,
    token_audience,
    token_fingerprint,
    token_has_scope,
    token_scopes,
)

WATCH_SCOPES: tuple[str, ...] = (
    REQUIRED_GRAPH_SCOPE,
    "User.Read",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "EduAssignments.Read",
    "EduAssignments.ReadBasic",
    "Chat.Read",
    "Presence.Read.All",
)


def _scope_flags(token: str) -> str:
    scopes = set(token_scopes(token))
    return "".join("+" if s in scopes else "-" for s in WATCH_SCOPES)


def run_probe(cookies: list[dict[str, Any]], refresh_url: str, duration: int = 30) -> int:
    from playwright.sync_api import sync_playwright

    events: list[dict[str, Any]] = []
    fingerprints_by_aud: dict[str, set[str]] = defaultdict(set)
    request_count = {"total": 0, "bearer": 0}
    t0 = 0.0

    def on_request(request: Any) -> None:
        request_count["total"] += 1
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        request_count["bearer"] += 1
        token = auth[7:]
        aud = token_audience(token)
        fp = token_fingerprint(token)
        fingerprints_by_aud[aud].add(fp)
        events.append(
            {
                "t_ms": int((time.monotonic() - t0) * 1000),
                "method": request.method,
                "url": request.url,
                "aud": aud,
                "scp_count": len(token_scopes(token)),
                "scp_flags": _scope_flags(token),
                "fp": fp,
                "exp_min": exp_remaining_minutes(token),
                "has_required": token_has_scope(token, REQUIRED_GRAPH_SCOPE),
            }
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            if cookies:
                with contextlib.suppress(Exception):
                    context.add_cookies(cookies)
            page = context.new_page()
            page.on("request", on_request)
            t0 = time.monotonic()
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)
            for _ in range(duration):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
        finally:
            browser.close()

    print(f"[summary] total_requests={request_count['total']} bearer={request_count['bearer']}")
    print(f"[summary] unique_tokens_by_aud:")
    for aud, fps in sorted(fingerprints_by_aud.items(), key=lambda kv: -len(kv[1])):
        print(f"  {aud:<60}  {len(fps)} unique")
    print()
    print(f"[watch_scopes] {' '.join(WATCH_SCOPES)}")
    print(f"{'t_ms':>6}  {'meth':<5} {'aud':<45} {'#':>3} flags     exp   fp            url")
    print("-" * 140)
    for e in events:
        print(
            f"{e['t_ms']:>6}  {e['method']:<5} {e['aud'][:45]:<45} "
            f"{e['scp_count']:>3} {e['scp_flags']:<8} "
            f"{(e['exp_min'] if e['exp_min'] is not None else '?'):>4}m  "
            f"{e['fp']}  {short_url(e['url'], 70)}"
        )
    return 0


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] refresh_url={refresh_url}")
    print(f"[setup] sso_cookies={len(cookies)}")
    print()
    return run_probe(cookies, refresh_url, duration=35)


if __name__ == "__main__":
    sys.exit(main())
