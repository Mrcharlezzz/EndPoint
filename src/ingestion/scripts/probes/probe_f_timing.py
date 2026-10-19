"""Probe F — Timing budget distribution.

Runs N back-to-back silent refreshes and records per-run timings:
  * t_first_request   — first network request after navigation
  * t_first_csa       — first /api/csa/ Bearer request
  * t_first_graph     — first graph.microsoft.com Bearer request (any scope)
  * t_first_scoped    — first graph.microsoft.com Bearer with channel scope
  * t_both            — earliest moment both CSA + scoped graph are present
  * t_total           — wall-clock elapsed (early-exit on success)

Gives a distribution (min/median/max) instead of a single data point so
we can calibrate the 30s + 10s budget in ``_wait_for_csa_token`` and the
2/3-then-1/3 split in ``PlaywrightTokenRefresher``.

Usage:
  cd src/ingestion
  uv run python scripts/probes/probe_f_timing.py        # default N=5
  uv run python scripts/probes/probe_f_timing.py 10     # override N
"""

from __future__ import annotations

import contextlib
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    REQUIRED_GRAPH_SCOPE,
    TEAMS_URL,
    filter_sso,
    load_cookies,
    load_creds,
    token_has_scope,
)


def one_run(cookies: list[dict[str, Any]], url: str, timeout: int = 45) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    t_first_req: int | None = None
    t_first_csa: int | None = None
    t_first_graph: int | None = None
    t_first_scoped: int | None = None
    t_both: int | None = None
    captured: dict[str, str] = {}
    t0 = 0.0

    def on_request(request: Any) -> None:
        nonlocal t_first_req, t_first_csa, t_first_graph, t_first_scoped, t_both
        t_ms = int((time.monotonic() - t0) * 1000)
        if t_first_req is None:
            t_first_req = t_ms
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        token = auth[7:]
        u: str = request.url
        if "graph.microsoft.com" in u:
            if t_first_graph is None:
                t_first_graph = t_ms
            existing = captured.get("graph_token")
            if existing is None or (
                token_has_scope(token, REQUIRED_GRAPH_SCOPE)
                and not token_has_scope(existing, REQUIRED_GRAPH_SCOPE)
            ):
                captured["graph_token"] = token
            if t_first_scoped is None and token_has_scope(token, REQUIRED_GRAPH_SCOPE):
                t_first_scoped = t_ms
        elif "/api/csa/" in u and "csa_token" not in captured:
            captured["csa_token"] = token
            t_first_csa = t_ms
        if (
            t_both is None
            and "csa_token" in captured
            and token_has_scope(captured.get("graph_token", ""), REQUIRED_GRAPH_SCOPE)
        ):
            t_both = t_ms

    t_start = time.monotonic()
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
                page.goto(url, timeout=30_000)
            for _ in range(timeout):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break
                if t_both is not None:
                    break
        finally:
            browser.close()
    total = int((time.monotonic() - t_start) * 1000)
    return {
        "t_first_req": t_first_req,
        "t_first_csa": t_first_csa,
        "t_first_graph": t_first_graph,
        "t_first_scoped": t_first_scoped,
        "t_both": t_both,
        "t_total": total,
        "success": t_both is not None,
    }


def _fmt(v: int | None) -> str:
    return str(v) if v is not None else "-"


def summary(label: str, values: list[int]) -> str:
    if not values:
        return f"{label:<14} n=0"
    return (
        f"{label:<14} n={len(values):<3} "
        f"min={min(values):>5} "
        f"p50={int(statistics.median(values)):>5} "
        f"p90={int(statistics.quantiles(values + values, n=10)[-1]) if len(values) >= 2 else values[0]:>5} "
        f"max={max(values):>5}"
    )


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())

    print(f"[setup] runs={n} sso_cookies={len(cookies)} refresh_url={refresh_url}")
    print()
    print(
        f"{'run':>3}  {'first_req':>9} {'first_csa':>9} {'first_graph':>11} "
        f"{'first_scoped':>12} {'both':>6} {'total':>6}  ok"
    )
    print("-" * 80)

    runs: list[dict[str, Any]] = []
    for i in range(n):
        r = one_run(cookies, refresh_url, timeout=45)
        runs.append(r)
        print(
            f"{i + 1:>3}  {_fmt(r['t_first_req']):>9} {_fmt(r['t_first_csa']):>9} "
            f"{_fmt(r['t_first_graph']):>11} {_fmt(r['t_first_scoped']):>12} "
            f"{_fmt(r['t_both']):>6} {r['t_total']:>6}  {'Y' if r['success'] else 'N'}"
        )

    print()
    print("[distribution]")
    for key in ("t_first_req", "t_first_csa", "t_first_graph", "t_first_scoped", "t_both", "t_total"):
        values = [r[key] for r in runs if r.get(key) is not None]
        print(f"  {summary(key, values)}")
    print(f"[success_rate] {sum(1 for r in runs if r['success'])}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
