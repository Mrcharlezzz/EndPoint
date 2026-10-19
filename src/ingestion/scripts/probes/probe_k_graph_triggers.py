"""Probe K — Graph anatomy with corrected selectors.

Probes I/J found:
  * Teams app host migrated to ``teams.cloud.microsoft``.
  * Sidebar is always expanded now — #menurb is gone.
  * Assignments button is ``data-tid=66aeee93-...`` with aria-label
    ``Assignments (Ctrl+Shift+4)``. The daemon's 4 stale selectors
    all miss.

This probe uses ``page.get_by_role("button", name="Assignments")``
(robust against keyboard-shortcut suffixes) to drive the flow, waits
for the Assignments iframe to appear, enumerates Past-due candidates
inside it, and clicks the best one. Full graph.microsoft.com traffic
captured with stage markers.

Output tells us exactly which stage produces the first scoped token.

Usage: cd src/ingestion && uv run python scripts/probes/probe_k_graph_triggers.py
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

ENUMERATE_PAST_DUE_JS = r"""
() => {
  const out = [];
  const all = document.querySelectorAll(
    'button, [role="button"], [role="tab"], [role="menuitem"], a[href]'
  );
  for (const el of all) {
    const text = (el.innerText || el.textContent || '').trim();
    const aria = el.getAttribute('aria-label') || '';
    const title = el.getAttribute('title') || '';
    const tid = el.getAttribute('data-tid') || '';
    const hay = [text, aria, title, tid].join(' ').toLowerCase();
    if (hay.includes('past') || hay.includes('due') || hay.includes('overdue') ||
        hay.includes('assignment')) {
      const rect = el.getBoundingClientRect();
      out.push({
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role'),
        aria, title, tid,
        text: text.slice(0, 60),
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        w: Math.round(rect.width),
        h: Math.round(rect.height),
        visible: rect.width > 0 && rect.height > 0,
      });
    }
  }
  return out;
}
"""


class StageClock:
    def __init__(self, t0: float) -> None:
        self._t0 = t0
        self._stages: list[tuple[int, str]] = []

    def mark(self, label: str) -> None:
        t_ms = int((time.monotonic() - self._t0) * 1000)
        self._stages.append((t_ms, label))
        print(f"  t={t_ms:>6}ms  stage: {label}")

    def tag_for(self, t_ms: int) -> str:
        current = "pre"
        for stage_ms, label in self._stages:
            if stage_ms <= t_ms:
                current = label
            else:
                break
        return current


def _wait_for_assignment_frame(
    page: Any, *, deadline: float, clock: StageClock
) -> Any | None:
    """Poll frames until one whose URL references assignments appears."""
    last_count = 0
    while time.monotonic() < deadline:
        frames = list(page.frames)
        if len(frames) != last_count:
            last_count = len(frames)
            print(f"    frames={len(frames)}:")
            for i, f in enumerate(frames):
                print(f"      #{i} {str(getattr(f, 'url', '?'))[:100]}")
        for f in frames:
            url = str(getattr(f, "url", ""))
            low = url.lower()
            if "assignment" in low or "edu" in low:
                clock.mark(f"assignment_frame_found({url[:60]})")
                return f
        page.wait_for_timeout(500)
    return None


def run(cookies: list[dict[str, Any]], refresh_url: str) -> int:
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
                post_data = ""
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
                    entry["body_sample"] = response.text()[:600]
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

            page.wait_for_timeout(6000)
            clock.mark("02_settled_6s")

            # Click Assignments via accessible name (daemon's selectors are stale).
            # Poll-wait up to 20s — button renders ~8-10s post-goto on first load.
            print("\n[click Assignments]")
            assignments_loc = page.get_by_role("button", name="Assignments")
            deadline = time.monotonic() + 20
            a_count = 0
            while time.monotonic() < deadline:
                a_count = assignments_loc.count()
                if a_count > 0:
                    break
                page.wait_for_timeout(500)
            clock.mark(f"02b_assignments_visible(count={a_count})")
            if a_count == 0:
                print("  Assignments button never rendered — aborting.")
                return 1
            try:
                assignments_loc.first.click(timeout=5000)
                clock.mark("03_assignments_clicked")
            except Exception as exc:
                clock.mark(f"03_assignments_click_FAIL({type(exc).__name__})")

            # Wait for the Assignments iframe to load
            print("\n[wait for assignments frame]")
            deadline = time.monotonic() + 15
            assignment_frame = _wait_for_assignment_frame(
                page, deadline=deadline, clock=clock
            )
            if assignment_frame is None:
                clock.mark("04_no_assignment_frame")
            else:
                clock.mark("04_assignment_frame_ready")

            page.wait_for_timeout(3000)
            clock.mark("05_post_settle_3s")

            # Enumerate Past-due candidates in every frame
            print("\n[enumerate past-due candidates]")
            past_due_candidates: list[tuple[int, dict[str, Any], Any]] = []
            for i, frame in enumerate(page.frames):
                try:
                    raw = frame.evaluate(ENUMERATE_PAST_DUE_JS)
                except Exception as exc:
                    print(f"  frame#{i} evaluate failed: {exc!s:.80}")
                    continue
                entries = list(raw) if isinstance(raw, list) else []
                if not entries:
                    continue
                print(
                    f"  frame#{i} ({str(getattr(frame, 'url', '?'))[:80]}): "
                    f"{len(entries)} candidates"
                )
                for e in entries:
                    text = (e.get("text") or "").replace("\n", " ")[:40]
                    aria = (e.get("aria") or "")[:30]
                    tid = (e.get("tid") or "")[:30]
                    print(
                        f"    tag={e.get('tag')} aria={aria} tid={tid} "
                        f"text={text!r} box=({e.get('x')},{e.get('y')} "
                        f"{e.get('w')}x{e.get('h')}) vis={e.get('visible')}"
                    )
                    past_due_candidates.append((i, e, frame))

            # Find a Past-due candidate and click it
            print("\n[click Past-due]")
            pd_hit_tag: str | None = None
            for idx, entry, frame in past_due_candidates:
                text = str(entry.get("text") or "").lower()
                aria = str(entry.get("aria") or "").lower()
                if "past due" not in text and "past due" not in aria and "overdue" not in text:
                    continue
                # Try role-based selector in this frame
                try:
                    loc = frame.get_by_role("button", name="Past due")
                    if loc.count() > 0:
                        loc.first.click(timeout=3000)
                        pd_hit_tag = f"get_by_role@frame#{idx}"
                        break
                except Exception:
                    pass
                try:
                    loc = frame.get_by_text("Past due")
                    if loc.count() > 0:
                        loc.first.click(timeout=3000)
                        pd_hit_tag = f"get_by_text@frame#{idx}"
                        break
                except Exception:
                    pass

            clock.mark(f"06_past_due_clicked({pd_hit_tag or 'NOT_FOUND'})")

            page.wait_for_timeout(12000)
            clock.mark("07_final_settle_12s")
        finally:
            browser.close()

    # ---------- Report ----------
    print()
    print(f"[graph_requests] total={len(graph_reqs)}")
    if not graph_reqs:
        print("  NONE — still no Graph traffic after the corrected flow.")
        return 0

    print()
    header = f"{'t_ms':>6} {'stage':<45} {'meth':<5} {'scp#':>4} sc {'fp':<14} url"
    print(header)
    print("-" * 160)
    for r in graph_reqs:
        stage = clock.tag_for(r["t_ms"])[:45]
        url = r["url"][:90]
        sc = "Y" if r["scoped"] else "-"
        print(
            f"{r['t_ms']:>6} {stage:<45} {r['method']:<5} "
            f"{len(r['scopes']):>4} {sc:<2} {r['fp']:<14} {url}"
        )

    # Scope evolution — show which stage first brought which new scope
    print()
    print("[scope evolution]")
    seen_scopes: set[str] = set()
    for r in graph_reqs:
        new = set(r["scopes"]) - seen_scopes
        if new:
            print(
                f"  t={r['t_ms']:>6}ms [{clock.tag_for(r['t_ms'])[:35]}] "
                f"+{len(new)} new: {sorted(new)}"
            )
            seen_scopes |= new

    first = graph_reqs[0]
    first_scoped = next((r for r in graph_reqs if r["scoped"]), None)

    print()
    print("=" * 80)
    print("[first graph request]")
    _dump_request(first, graph_responses)

    print()
    print("=" * 80)
    if first_scoped is None:
        print("[first SCOPED graph request] NONE")
    elif first_scoped is first:
        print("[first SCOPED graph request] same as first (above)")
    else:
        print("[first SCOPED graph request]")
        _dump_request(first_scoped, graph_responses)

    print()
    by_stage: dict[str, dict[str, int]] = {}
    for r in graph_reqs:
        stage = clock.tag_for(r["t_ms"])
        bucket = by_stage.setdefault(stage, {"total": 0, "scoped": 0})
        bucket["total"] += 1
        if r["scoped"]:
            bucket["scoped"] += 1
    print("[graph requests by stage]")
    for stage, bucket in by_stage.items():
        print(f"  total={bucket['total']:>3} scoped={bucket['scoped']:>3}  {stage}")

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
        print(f"  resp_body: {body[:500]}")


def main() -> int:
    teams_creds = load_creds()
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} refresh_url={refresh_url}")
    print()
    return run(cookies, refresh_url)


if __name__ == "__main__":
    sys.exit(main())
