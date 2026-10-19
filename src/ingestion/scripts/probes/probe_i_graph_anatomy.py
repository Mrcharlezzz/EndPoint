"""Probe I — Graph request anatomy + trigger dynamics.

Walks through the Assignments → Past-due flow while capturing full
detail on every ``graph.microsoft.com`` request. Answers:

  1. What does a Graph request carrying the refresh token actually
     look like — URL, method, path, query, body, headers, response?
  2. Which stage of the flow fires the first scoped graph call —
     ``page.goto``, sidebar toggle, Assignments click, iframe load,
     or the Past-due click?

Stage markers instrument the timeline so every captured request is
tagged with the stage it fired during. Full detail dumped for the
first scoped-graph hit. Also records the full scope set observed
across all graph tokens.

Usage: cd src/ingestion && uv run python scripts/probes/probe_i_graph_anatomy.py
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
    decode_jwt,
    exp_remaining_minutes,
    filter_sso,
    load_cookies,
    load_creds,
    token_audience,
    token_fingerprint,
    token_has_scope,
    token_scopes,
)

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


class StageClock:
    """Stage timeline; tags events with the active stage by wall-clock."""

    def __init__(self, t0: float) -> None:
        self._t0 = t0
        self._stages: list[tuple[int, str]] = []

    def mark(self, label: str) -> None:
        t_ms = int((time.monotonic() - self._t0) * 1000)
        self._stages.append((t_ms, label))
        print(f"  t={t_ms:>6}ms  stage: {label}")

    def tag_for(self, t_ms: int) -> str:
        current = "pre_mark"
        for stage_ms, label in self._stages:
            if stage_ms <= t_ms:
                current = label
            else:
                break
        return current

    @property
    def stages(self) -> list[tuple[int, str]]:
        return list(self._stages)


def _safe_click(target: Any, selector: str, timeout: int = 3000) -> bool:
    try:
        target.locator(selector).first.click(timeout=timeout)
        return True
    except Exception:
        return False


def _click_assignments(page: Any) -> str | None:
    for selector in ASSIGNMENTS_SELECTORS:
        if _safe_click(page, selector):
            return selector
    for selector in ASSIGNMENTS_SELECTORS:
        try:
            locator = page.locator(selector).first
            box = locator.bounding_box()
            if box is None:
                continue
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page.mouse.click(x, y)
            return f"{selector} (box)"
        except Exception:
            continue
    return None


def _click_past_due(page: Any) -> tuple[str | None, int | None]:
    for idx, frame in enumerate([*page.frames, page]):
        for selector in PAST_DUE_SELECTORS:
            try:
                frame.locator(selector).first.click(timeout=3000)
                return (selector, idx)
            except Exception:
                continue
    return (None, None)


def run_probe(cookies: list[dict[str, Any]], refresh_url: str) -> int:
    from playwright.sync_api import sync_playwright

    graph_reqs: list[dict[str, Any]] = []
    graph_responses: dict[str, dict[str, Any]] = {}
    t0 = 0.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()

            def on_request(request: Any) -> None:
                if "graph.microsoft.com" not in request.url:
                    return
                t_ms = int((time.monotonic() - t0) * 1000)
                auth = request.headers.get("authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                post_data: str = ""
                try:
                    raw = request.post_data
                    if raw is not None:
                        post_data = str(raw)[:300]
                except Exception:
                    post_data = ""
                graph_reqs.append(
                    {
                        "t_ms": t_ms,
                        "method": request.method,
                        "url": request.url,
                        "post_data": post_data,
                        "token": token,
                        "aud": token_audience(token),
                        "scopes": token_scopes(token),
                        "exp_min": exp_remaining_minutes(token) if token else None,
                        "fp": token_fingerprint(token) if token else "-",
                        "scoped": (
                            token_has_scope(token, REQUIRED_GRAPH_SCOPE) if token else False
                        ),
                    }
                )

            def on_response(response: Any) -> None:
                if "graph.microsoft.com" not in response.url:
                    return
                entry: dict[str, Any] = {"status": response.status}
                try:
                    entry["body_sample"] = response.text()[:500]
                except Exception:
                    entry["body_sample"] = "<read failed>"
                graph_responses[response.url] = entry

            page.on("request", on_request)
            page.on("response", on_response)

            t0 = time.monotonic()
            clock = StageClock(t0)

            clock.mark("00_before_goto")
            with contextlib.suppress(Exception):
                page.goto(refresh_url, timeout=30_000)
            clock.mark("01_after_goto")

            page.wait_for_timeout(8000)
            clock.mark("02_settled_8s")

            _safe_click(page, SIDEBAR_TOGGLE)
            clock.mark("03_sidebar_toggled")
            page.wait_for_timeout(1000)

            hit = _click_assignments(page)
            clock.mark(f"04_assignments_clicked({hit or 'FAIL'})")
            page.wait_for_timeout(3000)
            clock.mark("05_assignments_settled_3s")

            frame_urls = [str(getattr(f, "url", "?"))[:80] for f in page.frames]
            clock.mark(f"06_frames={len(frame_urls)}")
            for u in frame_urls:
                print(f"    frame: {u}")

            pd_hit, pd_frame = _click_past_due(page)
            clock.mark(f"07_past_due_clicked({pd_hit or 'FAIL'}@frame{pd_frame})")
            page.wait_for_timeout(15000)
            clock.mark("08_final_settle_15s")
        finally:
            browser.close()

    # ---------- Report ----------
    print()
    print(f"[graph_requests] total={len(graph_reqs)}")
    if not graph_reqs:
        print("  NONE — no requests to graph.microsoft.com during the full flow.")
        return 0

    print()
    header = f"{'t_ms':>6} {'stage':<45} {'meth':<5} {'scp#':>4} sc {'fp':<14} url"
    print(header)
    print("-" * len(header) * 2)
    for r in graph_reqs:
        stage = clock.tag_for(r["t_ms"])[:45]
        url = r["url"][:90]
        sc = "Y" if r["scoped"] else "-"
        print(
            f"{r['t_ms']:>6} {stage:<45} {r['method']:<5} "
            f"{len(r['scopes']):>4} {sc:<2} {r['fp']:<14} {url}"
        )

    first = graph_reqs[0]
    first_scoped = next((r for r in graph_reqs if r["scoped"]), None)

    print()
    print("=" * 80)
    print("[first graph request]")
    _dump_request(first, graph_responses)

    print()
    print("=" * 80)
    if first_scoped is None:
        print("[first SCOPED graph request] NONE — no token carried ChannelMessage.Read.All")
    elif first_scoped is first:
        print("[first SCOPED graph request] same as first request (above)")
    else:
        print("[first SCOPED graph request]")
        _dump_request(first_scoped, graph_responses)

    print()
    all_scopes: set[str] = set()
    for r in graph_reqs:
        all_scopes.update(r["scopes"])
    print(f"[unique scopes seen in graph tokens] ({len(all_scopes)})")
    for s in sorted(all_scopes):
        tag = "  <-- REQUIRED" if s == REQUIRED_GRAPH_SCOPE else ""
        print(f"  {s}{tag}")

    print()
    by_stage: dict[str, int] = {}
    for r in graph_reqs:
        stage = clock.tag_for(r["t_ms"])
        by_stage[stage] = by_stage.get(stage, 0) + 1
    print("[graph requests by stage]")
    for stage, count in by_stage.items():
        print(f"  {count:>3}  {stage}")

    return 0


def _dump_request(r: dict[str, Any], responses: dict[str, dict[str, Any]]) -> None:
    print(f"  t_ms     : {r['t_ms']}")
    print(f"  method   : {r['method']}")
    print(f"  url      : {r['url']}")
    if r["post_data"]:
        print(f"  body     : {r['post_data']}")
    print(f"  aud      : {r['aud']}")
    print(f"  exp_min  : {r['exp_min']}")
    print(f"  fp       : {r['fp']}")
    print(f"  scopes   ({len(r['scopes'])}):")
    for s in r["scopes"]:
        tag = "  <-- REQUIRED" if s == REQUIRED_GRAPH_SCOPE else ""
        print(f"    {s}{tag}")
    claims = decode_jwt(r["token"]) or {}
    interesting: dict[str, Any] = {}
    for k in ("iss", "tid", "appid", "idtyp", "app_displayname", "oid", "upn"):
        if k in claims:
            interesting[k] = claims[k]
    print(f"  claims   : {interesting}")
    resp = responses.get(r["url"])
    if resp:
        print(f"  status   : {resp['status']}")
        body = resp.get("body_sample", "")
        print(f"  resp_body: {body[:400]}")


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} refresh_url={refresh_url}")
    print()
    return run_probe(cookies, refresh_url)


if __name__ == "__main__":
    sys.exit(main())
