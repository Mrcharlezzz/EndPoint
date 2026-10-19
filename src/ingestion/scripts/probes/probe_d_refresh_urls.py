"""Probe D — Refresh URL outcome comparison.

Runs the FULL silent-refresh capture cycle (with the same early-exit
once both tokens arrive) against N candidate refresh URLs. Measures
whether the URL variant materially changes capture rate, time-to-full,
and whether a scoped graph_token comes back.

Intended audience: deciding whether the stored refresh_url is optimal
or whether we should re-pick it (e.g. after a team gets archived).

Usage: cd src/ingestion && uv run python scripts/probes/probe_d_refresh_urls.py
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
)


def build_channel_urls(graph_token: str, team_ids: list[str]) -> list[tuple[str, str]]:
    """Build up to 3 different channel URLs by probing Graph."""
    import httpx

    urls: list[tuple[str, str]] = []
    if not graph_token or not team_ids:
        return urls
    try:
        with httpx.Client(timeout=15) as client:
            for team_id in team_ids[:3]:
                r = client.get(
                    f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels",
                    headers={"Authorization": f"Bearer {graph_token}"},
                )
                if r.status_code != 200:
                    continue
                channels = r.json().get("value", [])
                for ch in channels[:2]:
                    ch_id = ch.get("id")
                    name = ch.get("displayName", "General")
                    if not ch_id:
                        continue
                    label = f"chan:{name[:20]}@{team_id[-6:]}"
                    url = (
                        f"https://teams.microsoft.com/l/channel/{ch_id}/{name}?"
                        f"groupId={team_id}"
                    )
                    urls.append((label, url))
                    if len(urls) >= 4:
                        return urls
    except Exception:
        pass
    return urls


def capture_cycle(cookies: list[dict[str, Any]], url: str, timeout: int = 30) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {}
    t_first_csa: int | None = None
    t_first_graph: int | None = None
    t_first_scoped: int | None = None
    t0 = 0.0

    def on_request(request: Any) -> None:
        nonlocal t_first_csa, t_first_graph, t_first_scoped
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return
        token = auth[7:]
        u: str = request.url
        t_ms = int((time.monotonic() - t0) * 1000)
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
                graph = captured.get("graph_token")
                if graph and "csa_token" in captured and token_has_scope(graph, REQUIRED_GRAPH_SCOPE):
                    break
        finally:
            browser.close()
    elapsed = time.monotonic() - t_start

    return {
        "elapsed": elapsed,
        "t_first_csa": t_first_csa,
        "t_first_graph": t_first_graph,
        "t_first_scoped": t_first_scoped,
        "has_graph": "graph_token" in captured,
        "has_csa": "csa_token" in captured,
        "graph_scoped": token_has_scope(captured.get("graph_token", ""), REQUIRED_GRAPH_SCOPE),
        "graph_exp_min": exp_remaining_minutes(captured.get("graph_token", ""))
        if "graph_token" in captured
        else None,
    }


def main() -> int:
    teams_creds = load_creds()
    saved_url = teams_creds.get("refresh_url")
    stored_graph = teams_creds.get("graph_token", "")
    team_ids = teams_creds.get("team_ids", []) or []
    cookies = filter_sso(load_cookies())

    targets: list[tuple[str, str]] = [("homepage", TEAMS_URL)]
    if saved_url:
        targets.append(("saved", saved_url))
    targets.append(("assignments_root", "https://teams.microsoft.com/_#/school/assignments"))
    targets.extend(build_channel_urls(stored_graph, team_ids))

    print(f"[setup] sso_cookies={len(cookies)} candidates={len(targets)}")
    print()
    print(
        f"{'name':<28} {'elapsed':>8} {'csa_ms':>7} {'graph_ms':>9} {'scoped_ms':>10}  "
        f"result"
    )
    print("-" * 110)

    for name, url in targets:
        r = capture_cycle(cookies, url, timeout=35)
        csa = r["t_first_csa"] if r["t_first_csa"] is not None else "-"
        gr = r["t_first_graph"] if r["t_first_graph"] is not None else "-"
        sc = r["t_first_scoped"] if r["t_first_scoped"] is not None else "-"
        tags: list[str] = []
        tags.append("graph:scoped" if r["graph_scoped"] else ("graph:homepage" if r["has_graph"] else "graph:MISS"))
        tags.append("csa:ok" if r["has_csa"] else "csa:MISS")
        if r["graph_exp_min"] is not None:
            tags.append(f"exp:{r['graph_exp_min']}m")
        print(
            f"{name:<28} {r['elapsed']:>7.1f}s {csa!s:>7} {gr!s:>9} {sc!s:>10}  "
            f"{' '.join(tags)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
