"""Probe M - upgraded Graph refresh harness.

This is a production-shaped successor to Probe K for the Teams Graph-token
problem. It keeps Probe K's Assignments/Past-due trigger, but fixes the main
probe limitations:

* async Playwright, closer to ``PlaywrightTokenRefresher`` than sync probes
* context-level request capture, not only ``page.on("request")``
* optional Chromium CDP Network capture for requests Playwright events miss
* adaptive waits instead of fixed final sleeps
* robust Assignments/Past-due locators plus keyboard and deeplink fallbacks
* optional post-capture Graph validation calls, including channel messages
* CSA presence capture, updated-cookie readback, and cookie rotation summary
* multi-run timing/success distribution

No bearer token is printed. Tokens are identified by short SHA-256 fingerprints.

Usage:
  cd src/ingestion
  uv run --extra cli python scripts/probes/probe_m_graph_refresh_harness.py
  uv run --extra cli python scripts/probes/probe_m_graph_refresh_harness.py --runs 5
  uv run --extra cli python scripts/probes/probe_m_graph_refresh_harness.py \
    --url https://teams.cloud.microsoft --timeout 75 --screenshots
  uv run --extra cli python scripts/probes/probe_m_graph_refresh_harness.py --validate-graph
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    LOGIN_REDIRECT_HOSTS,
    REQUIRED_GRAPH_SCOPE,
    TEAMS_URL,
    decode_jwt,
    exp_remaining_minutes,
    filter_sso,
    load_cookies,
    load_creds,
    short_url,
    token_audience,
    token_exp,
    token_fingerprint,
    token_has_scope,
    token_scopes,
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ASSIGNMENTS_BASE = "https://assignments.edu.cloud.microsoft"
NEW_ASSIGNMENTS_SELECTOR = "button[data-tid^='66aeee93']"
OLD_ASSIGNMENTS_SELECTOR = "button[data-tid='app-bar-2a84919f-59d8-4441-a975-2a8c2643b741']"

ASSIGNMENTS_NAME = re.compile(r"assignments?", re.IGNORECASE)
PAST_DUE_NAME = re.compile(r"past\s*due|overdue", re.IGNORECASE)

CLICK_PAST_DUE_JS = r"""
() => {
  const selectors = [
    'button', '[role="button"]', '[role="tab"]',
    '[role="menuitem"]', 'a[href]', '[aria-label]'
  ];
  const seen = new Set();
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const text = (el.innerText || el.textContent || '').trim();
      const aria = el.getAttribute('aria-label') || '';
      const title = el.getAttribute('title') || '';
      const hay = `${text} ${aria} ${title}`.toLowerCase();
      if (!hay.includes('past due') && !hay.includes('overdue')) continue;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const visible = rect.width > 0 && rect.height > 0
        && style.visibility !== 'hidden' && style.display !== 'none';
      if (!visible) continue;
      el.click();
      return {
        tag: el.tagName.toLowerCase(),
        text: text.slice(0, 60),
        aria: aria.slice(0, 60),
        title: title.slice(0, 60),
      };
    }
  }
  return null;
}
"""


def _vlog(verbose: bool, message: str, *, run_index: int | None = None) -> None:
    if not verbose:
        return
    prefix = "[verbose]"
    if run_index is not None:
        prefix = f"[verbose run={run_index}]"
    print(f"{prefix} {message}", flush=True)


@dataclass(frozen=True)
class TokenEvent:
    source: str
    t_ms: int
    stage: str
    method: str
    url: str
    audience: str
    appid: str
    app_name: str
    scope_count: int
    has_required_scope: bool
    exp_min: int | None
    fingerprint: str


@dataclass(frozen=True)
class ValidationResult:
    name: str
    status: int | None
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class CookieRotation:
    before_sso: int
    after_sso: int
    extended: int
    added: int
    max_extension_days: float | None


@dataclass
class CaptureState:
    verbose: bool = False
    run_index: int = 0
    graph_token: str | None = None
    csa_token: str | None = None
    token_events: list[TokenEvent] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=lambda: {"context": 0, "cdp": 0})

    def consider_token(
        self,
        *,
        source: str,
        t_ms: int,
        stage: str,
        method: str,
        url: str,
        token: str,
    ) -> None:
        self.source_counts[source] = self.source_counts.get(source, 0) + 1
        claims = decode_jwt(token) or {}
        event = TokenEvent(
            source=source,
            t_ms=t_ms,
            stage=stage,
            method=method,
            url=url,
            audience=token_audience(token),
            appid=str(claims.get("appid", "")) or "<no-appid>",
            app_name=str(claims.get("app_displayname", "")),
            scope_count=len(token_scopes(token)),
            has_required_scope=token_has_scope(token, REQUIRED_GRAPH_SCOPE),
            exp_min=exp_remaining_minutes(token),
            fingerprint=token_fingerprint(token),
        )
        self.token_events.append(event)

        if "graph.microsoft.com" in url:
            if self.graph_token is None or _graph_token_is_better(token, self.graph_token):
                self.graph_token = token
                if self.verbose:
                    scope_tag = "scoped" if event.has_required_scope else "plain"
                    _vlog(
                        True,
                        (
                            f"captured graph token source={source} stage={stage} "
                            f"t={t_ms}ms fp={event.fingerprint} {scope_tag} "
                            f"scp={event.scope_count} exp={event.exp_min}m "
                            f"appid={event.appid} url={short_url(url, 80)}"
                        ),
                        run_index=self.run_index,
                    )
        elif "/api/csa/" in url and self.csa_token is None:
            self.csa_token = token
            if self.verbose:
                _vlog(
                    True,
                    (
                        f"captured csa token source={source} stage={stage} "
                        f"t={t_ms}ms fp={event.fingerprint} exp={event.exp_min}m "
                        f"url={short_url(url, 80)}"
                    ),
                    run_index=self.run_index,
                )

    @property
    def has_scoped_graph(self) -> bool:
        return bool(self.graph_token) and token_has_scope(self.graph_token, REQUIRED_GRAPH_SCOPE)

    @property
    def has_csa(self) -> bool:
        return self.csa_token is not None

    @property
    def ready(self) -> bool:
        return self.has_csa and self.has_scoped_graph


@dataclass
class RunResult:
    run_index: int
    elapsed_ms: int
    expired_redirect: bool
    final_url: str
    capture: CaptureState
    validations: list[ValidationResult]
    cookie_rotation: CookieRotation
    trigger_steps: list[str]

    @property
    def message_validation_ok(self) -> bool:
        return any(v.name.startswith("graph.messages") and v.ok for v in self.validations)

    @property
    def success(self) -> bool:
        if self.validations:
            return self.capture.ready and self.message_validation_ok
        return self.capture.ready


class StageClock:
    def __init__(self, t0: float, *, verbose: bool = False, run_index: int = 0) -> None:
        self._t0 = t0
        self._stages: list[tuple[int, str]] = [(0, "init")]
        self._verbose = verbose
        self._run_index = run_index

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def mark(self, label: str) -> None:
        t_ms = self.now_ms()
        self._stages.append((t_ms, label))
        _vlog(self._verbose, f"stage={label} t={t_ms}ms", run_index=self._run_index)

    def tag_for(self, t_ms: int) -> str:
        current = "init"
        for stage_ms, label in self._stages:
            if stage_ms <= t_ms:
                current = label
            else:
                break
        return current

    @property
    def current(self) -> str:
        return self._stages[-1][1]


def _graph_token_is_better(candidate: str, existing: str) -> bool:
    candidate_scoped = token_has_scope(candidate, REQUIRED_GRAPH_SCOPE)
    existing_scoped = token_has_scope(existing, REQUIRED_GRAPH_SCOPE)
    if candidate_scoped and not existing_scoped:
        return True
    if not candidate_scoped and existing_scoped:
        return False
    return token_exp(candidate) > token_exp(existing)


def _header(headers: Any, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _is_login_redirect(url: str) -> bool:
    return any(host in url for host in LOGIN_REDIRECT_HOSTS)


async def _wait_until_ready(
    state: CaptureState,
    *,
    deadline: float,
    verbose: bool,
    run_index: int,
) -> None:
    while time.monotonic() < deadline:
        if state.ready:
            _vlog(verbose, "ready: csa + scoped graph captured", run_index=run_index)
            return
        await asyncio.sleep(0.25)
    _vlog(
        verbose,
        f"wait expired ready={state.ready} has_csa={state.has_csa} "
        f"has_scoped_graph={state.has_scoped_graph}",
        run_index=run_index,
    )


async def _click_assignments(
    page: Any,
    *,
    deadline: float,
    verbose: bool,
    run_index: int,
) -> str | None:
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        attempts = (
            ("role-button", lambda: page.get_by_role("button", name=ASSIGNMENTS_NAME)),
            ("role-link", lambda: page.get_by_role("link", name=ASSIGNMENTS_NAME)),
            ("label", lambda: page.get_by_label(ASSIGNMENTS_NAME)),
            ("text", lambda: page.get_by_text(ASSIGNMENTS_NAME)),
            ("tid-new", lambda: page.locator(NEW_ASSIGNMENTS_SELECTOR)),
            ("tid-old", lambda: page.locator(OLD_ASSIGNMENTS_SELECTOR)),
        )
        for label, factory in attempts:
            try:
                locator = factory()
                count = await locator.count()
                if count > 0:
                    _vlog(
                        verbose,
                        f"assignments locator hit label={label} count={count} poll={poll}",
                        run_index=run_index,
                    )
                    await locator.first.click(timeout=3000)
                    _vlog(verbose, f"assignments clicked via {label}", run_index=run_index)
                    return label
            except Exception as exc:
                _vlog(
                    verbose,
                    f"assignments locator miss label={label} error={type(exc).__name__}",
                    run_index=run_index,
                )
                continue
        await asyncio.sleep(0.5)

    _vlog(verbose, "assignments click exhausted all locators", run_index=run_index)
    return None


async def _press_assignments_shortcut(page: Any, *, verbose: bool, run_index: int) -> bool:
    try:
        await page.keyboard.press("Control+Shift+4")
        _vlog(verbose, "sent Control+Shift+4 assignments shortcut", run_index=run_index)
        return True
    except Exception as exc:
        _vlog(
            verbose,
            f"Control+Shift+4 shortcut failed error={type(exc).__name__}",
            run_index=run_index,
        )
        return False


async def _navigate_assignments_deeplink(
    page: Any,
    *,
    refresh_url: str,
    verbose: bool,
    run_index: int,
) -> str | None:
    hosts: list[str] = []
    for candidate in (str(getattr(page, "url", "")), refresh_url, TEAMS_URL):
        parsed = urlparse(candidate)
        if parsed.netloc and "teams" in parsed.netloc and parsed.netloc not in hosts:
            hosts.append(parsed.netloc)
    if "teams.cloud.microsoft" not in hosts:
        hosts.append("teams.cloud.microsoft")

    for host in hosts:
        url = f"https://{host}/_#/school/assignments"
        try:
            _vlog(verbose, f"deeplink attempt {url}", run_index=run_index)
            await page.goto(url, timeout=15_000, wait_until="domcontentloaded")
            _vlog(verbose, f"deeplink succeeded {url}", run_index=run_index)
            return url
        except Exception as exc:
            _vlog(
                verbose,
                f"deeplink failed host={host} error={type(exc).__name__}",
                run_index=run_index,
            )
            continue
    return None


async def _wait_for_assignment_frame(
    page: Any,
    *,
    deadline: float,
    verbose: bool,
    run_index: int,
) -> Any | None:
    last_frame_count = -1
    while time.monotonic() < deadline:
        frames = list(getattr(page, "frames", []))
        if verbose and len(frames) != last_frame_count:
            last_frame_count = len(frames)
            _vlog(verbose, f"frame count={len(frames)}", run_index=run_index)
            for idx, frame in enumerate(frames):
                url = str(getattr(frame, "url", ""))
                _vlog(verbose, f"frame#{idx} {short_url(url, 100)}", run_index=run_index)
        for frame in frames:
            url = str(getattr(frame, "url", "")).lower()
            if "assignment" in url or "edu" in url:
                _vlog(
                    verbose,
                    f"assignment frame found {short_url(str(getattr(frame, 'url', '')), 100)}",
                    run_index=run_index,
                )
                return frame
        await asyncio.sleep(0.25)
    _vlog(verbose, "assignment frame wait timed out", run_index=run_index)
    return None


async def _click_past_due(page: Any, *, verbose: bool, run_index: int) -> str | None:
    targets = [*list(getattr(page, "frames", [])), page]
    for idx, target in enumerate(targets):
        attempts: list[tuple[str, Any]] = []
        with contextlib.suppress(Exception):
            attempts.append(("role-button", target.get_by_role("button", name=PAST_DUE_NAME)))
        with contextlib.suppress(Exception):
            attempts.append(("role-tab", target.get_by_role("tab", name=PAST_DUE_NAME)))
        with contextlib.suppress(Exception):
            attempts.append(("text", target.get_by_text(PAST_DUE_NAME)))
        with contextlib.suppress(Exception):
            attempts.append(("selector-button", target.locator("button:has-text('Past due')")))
        with contextlib.suppress(Exception):
            attempts.append(
                ("selector-role", target.locator("[role='button']:has-text('Past due')"))
            )
        for label, locator in attempts:
            try:
                count = await locator.count()
                if count > 0:
                    _vlog(
                        verbose,
                        f"past-due locator hit label={label} frame={idx} count={count}",
                        run_index=run_index,
                    )
                    await locator.first.click(timeout=3000)
                    _vlog(
                        verbose,
                        f"past-due clicked via {label}@frame{idx}",
                        run_index=run_index,
                    )
                    return f"{label}@frame{idx}"
            except Exception as exc:
                _vlog(
                    verbose,
                    f"past-due locator miss label={label} frame={idx} error={type(exc).__name__}",
                    run_index=run_index,
                )
                continue

    for idx, target in enumerate(targets):
        try:
            clicked = await target.evaluate(CLICK_PAST_DUE_JS)
        except Exception as exc:
            _vlog(
                verbose,
                f"past-due JS fallback failed frame={idx} error={type(exc).__name__}",
                run_index=run_index,
            )
            continue
        if isinstance(clicked, dict):
            text = str(clicked.get("text") or clicked.get("aria") or clicked.get("title") or "")
            _vlog(verbose, f"past-due JS fallback clicked frame={idx}", run_index=run_index)
            return f"js-click@frame{idx}:{text[:40]}"
    _vlog(verbose, "past-due click exhausted all fallbacks", run_index=run_index)
    return None


async def _validate_graph_token(
    token: str | None,
    *,
    team_ids: list[str],
    max_teams: int,
    verbose: bool,
    run_index: int,
) -> list[ValidationResult]:
    if token is None:
        _vlog(verbose, "validation skipped: graph token missing", run_index=run_index)
        return [ValidationResult("graph.token", None, False, "missing")]

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    results: list[ValidationResult] = []
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        me_payload: dict[str, Any] = {}
        me = await _get_json(
            client,
            f"{GRAPH_BASE}/me",
            headers,
            label="graph.me",
            verbose=verbose,
            run_index=run_index,
        )
        results.append(me.result("graph.me"))
        if isinstance(me.payload, dict):
            me_payload = me.payload

        teams = list(team_ids[:max_teams])
        if not teams:
            joined = await _get_json(
                client,
                f"{GRAPH_BASE}/me/joinedTeams?$top={max_teams}",
                headers,
                label="graph.joinedTeams",
                verbose=verbose,
                run_index=run_index,
            )
            results.append(joined.result("graph.joinedTeams"))
            if isinstance(joined.payload, dict):
                teams = [
                    str(t.get("id"))
                    for t in joined.payload.get("value", [])
                    if isinstance(t, dict) and t.get("id")
                ][:max_teams]

        message_ok = False
        for team_id in teams[:max_teams]:
            channels_url = f"{GRAPH_BASE}/teams/{team_id}/channels"
            label = f"graph.channels:{team_id[-6:]}"
            channels = await _get_json(
                client,
                channels_url,
                headers,
                label=label,
                verbose=verbose,
                run_index=run_index,
            )
            results.append(channels.result(f"graph.channels:{team_id[-6:]}"))
            if not isinstance(channels.payload, dict):
                continue
            channel_ids = [
                str(c.get("id"))
                for c in channels.payload.get("value", [])
                if isinstance(c, dict) and c.get("id")
            ]
            if not channel_ids or message_ok:
                continue
            messages_url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_ids[0]}/messages?$top=1"
            label = f"graph.messages:{team_id[-6:]}"
            messages = await _get_json(
                client,
                messages_url,
                headers,
                label=label,
                verbose=verbose,
                run_index=run_index,
            )
            result = messages.result(label)
            results.append(result)
            message_ok = message_ok or result.ok

        # Assignments service uses the Graph-audience token in this codebase.
        for team_id in teams[:max_teams]:
            assignments_url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/{team_id}/assignments"
            label = f"edu.assignments:{team_id[-6:]}"
            assignments = await _get_json(
                client,
                assignments_url,
                headers,
                label=label,
                verbose=verbose,
                run_index=run_index,
            )
            results.append(assignments.result(label))
            if assignments.ok:
                break

        user_id = str(me_payload.get("id", ""))
        if user_id:
            user_assignments_url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/users/{user_id}/assignments"
            user_assignments = await _get_json(
                client,
                user_assignments_url,
                headers,
                label="edu.userAssignments",
                verbose=verbose,
                run_index=run_index,
            )
            results.append(user_assignments.result("edu.userAssignments"))

    return results


@dataclass(frozen=True)
class JsonResponseProbe:
    status: int | None
    payload: Any = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def result(self, name: str) -> ValidationResult:
        return ValidationResult(name, self.status, self.ok, self.detail)


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    label: str,
    verbose: bool,
    run_index: int,
) -> JsonResponseProbe:
    _vlog(verbose, f"validation request {label} {short_url(url, 100)}", run_index=run_index)
    try:
        response = await client.get(url, headers=headers)
    except Exception as exc:
        _vlog(
            verbose,
            f"validation exception {label} error={type(exc).__name__}",
            run_index=run_index,
        )
        return JsonResponseProbe(None, detail=type(exc).__name__)
    detail = ""
    payload: Any = None
    with contextlib.suppress(Exception):
        payload = response.json()
    if response.status_code >= 400:
        detail = _error_detail(payload)
    _vlog(
        verbose,
        f"validation response {label} status={response.status_code} detail={detail or '-'}",
        run_index=run_index,
    )
    return JsonResponseProbe(response.status_code, payload=payload, detail=detail)


def _error_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code", ""))
        message = str(error.get("message", ""))
        return f"{code}:{message[:90]}".strip(":")
    return ""


def _cookie_rotation(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> CookieRotation:
    before_sso = filter_sso(before)
    after_sso = filter_sso(after)
    before_by_key = {(str(c.get("domain", "")), str(c.get("name", ""))): c for c in before_sso}
    extended = 0
    added = 0
    max_extension: float | None = None
    for c in after_sso:
        key = (str(c.get("domain", "")), str(c.get("name", "")))
        old = before_by_key.get(key)
        if old is None:
            added += 1
            continue
        old_exp = old.get("expires")
        new_exp = c.get("expires")
        if isinstance(old_exp, int | float) and isinstance(new_exp, int | float):
            delta = float(new_exp) - float(old_exp)
            if delta > 0:
                extended += 1
                max_extension = delta if max_extension is None else max(max_extension, delta)
    return CookieRotation(
        before_sso=len(before_sso),
        after_sso=len(after_sso),
        extended=extended,
        added=added,
        max_extension_days=(max_extension / 86_400) if max_extension is not None else None,
    )


async def run_once(
    *,
    run_index: int,
    cookies: list[dict[str, Any]],
    refresh_url: str,
    team_ids: list[str],
    timeout_seconds: int,
    cdp_enabled: bool,
    block_service_workers: bool,
    screenshots: bool,
    max_validate_teams: int,
    validate_graph: bool,
    verbose: bool,
) -> RunResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "error: playwright is not installed; run with `uv run --extra cli ...`",
            file=sys.stderr,
        )
        raise

    state = CaptureState(verbose=verbose, run_index=run_index)
    trigger_steps: list[str] = []
    t_start = time.monotonic()
    final_url = ""
    expired_redirect = False
    updated_cookies: list[dict[str, Any]] = []

    async with async_playwright() as p:
        _vlog(verbose, "launching chromium headless=True", run_index=run_index)
        browser = await p.chromium.launch(headless=True)
        try:
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1400, "height": 900},
                "locale": "en-US",
            }
            if block_service_workers:
                context_kwargs["service_workers"] = "block"
            _vlog(verbose, f"new_context kwargs={context_kwargs}", run_index=run_index)
            context = await browser.new_context(**context_kwargs)
            _vlog(verbose, f"adding cookies count={len(cookies)}", run_index=run_index)
            await context.add_cookies(cookies)

            t0 = time.monotonic()
            clock = StageClock(t0, verbose=verbose, run_index=run_index)

            def on_context_request(request: Any) -> None:
                auth = request.headers.get("authorization", "")
                if not auth.startswith("Bearer "):
                    return
                state.consider_token(
                    source="context",
                    t_ms=clock.now_ms(),
                    stage=clock.current,
                    method=str(request.method),
                    url=str(request.url),
                    token=auth[7:],
                )

            context.on("request", on_context_request)

            page = await context.new_page()
            if cdp_enabled:
                await _install_cdp_capture(page, state=state, clock=clock)
                _vlog(verbose, "cdp network capture installed", run_index=run_index)

            clock.mark("goto")
            try:
                _vlog(verbose, f"goto {refresh_url}", run_index=run_index)
                await page.goto(refresh_url, timeout=30_000, wait_until="domcontentloaded")
            except Exception as exc:
                trigger_steps.append(f"goto failed: {type(exc).__name__}")
                _vlog(verbose, f"goto failed error={type(exc).__name__}", run_index=run_index)
            final_url = str(getattr(page, "url", ""))
            _vlog(verbose, f"post-goto url={short_url(final_url, 100)}", run_index=run_index)
            expired_redirect = _is_login_redirect(final_url)
            if expired_redirect:
                trigger_steps.append(f"login redirect: {short_url(final_url, 80)}")

            clock.mark("initial-wait")
            await _wait_until_ready(
                state,
                deadline=time.monotonic() + min(12, timeout_seconds),
                verbose=verbose,
                run_index=run_index,
            )

            if not expired_redirect and not state.has_scoped_graph:
                clock.mark("click-assignments")
                hit = await _click_assignments(
                    page,
                    deadline=time.monotonic() + 15,
                    verbose=verbose,
                    run_index=run_index,
                )
                trigger_steps.append(f"assignments={hit or 'miss'}")
                if hit is None:
                    clock.mark("assignments-keyboard")
                    keyboard_hit = await _press_assignments_shortcut(
                        page,
                        verbose=verbose,
                        run_index=run_index,
                    )
                    trigger_steps.append(
                        f"keyboard_control_shift_4={'sent' if keyboard_hit else 'miss'}"
                    )

                clock.mark("wait-assignment-frame")
                frame = await _wait_for_assignment_frame(
                    page,
                    deadline=time.monotonic() + 8,
                    verbose=verbose,
                    run_index=run_index,
                )
                if frame is None:
                    clock.mark("assignments-deeplink")
                    link = await _navigate_assignments_deeplink(
                        page,
                        refresh_url=refresh_url,
                        verbose=verbose,
                        run_index=run_index,
                    )
                    trigger_steps.append(f"deeplink={short_url(link, 80) if link else 'miss'}")
                    clock.mark("wait-assignment-frame-after-deeplink")
                    frame = await _wait_for_assignment_frame(
                        page,
                        deadline=time.monotonic() + 15,
                        verbose=verbose,
                        run_index=run_index,
                    )
                frame_url = str(getattr(frame, "url", "")) if frame is not None else ""
                trigger_steps.append(
                    f"assignment_frame={short_url(frame_url, 80) if frame_url else 'miss'}"
                )

                clock.mark("click-past-due")
                pd = await _click_past_due(page, verbose=verbose, run_index=run_index)
                trigger_steps.append(f"past_due={pd or 'miss'}")

                clock.mark("post-trigger-wait")
                remaining = max(1.0, (t_start + timeout_seconds) - time.monotonic())
                await _wait_until_ready(
                    state,
                    deadline=time.monotonic() + remaining,
                    verbose=verbose,
                    run_index=run_index,
                )

            if screenshots:
                path = f"/tmp/probe_m_run{run_index:02d}.png"
                with contextlib.suppress(Exception):
                    await page.screenshot(path=path, full_page=False)
                    trigger_steps.append(f"screenshot={path}")

            final_url = str(getattr(page, "url", ""))
            expired_redirect = expired_redirect or _is_login_redirect(final_url)
            updated_cookies = [dict(c) for c in await context.cookies()]
            _vlog(
                verbose,
                f"context cookies after run count={len(updated_cookies)}",
                run_index=run_index,
            )
            await page.close()
            await context.close()
        finally:
            await browser.close()
            _vlog(verbose, "browser closed", run_index=run_index)

    validations = (
        await _validate_graph_token(
            state.graph_token,
            team_ids=team_ids,
            max_teams=max_validate_teams,
            verbose=verbose,
            run_index=run_index,
        )
        if validate_graph
        else []
    )
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return RunResult(
        run_index=run_index,
        elapsed_ms=elapsed_ms,
        expired_redirect=expired_redirect,
        final_url=final_url,
        capture=state,
        validations=validations,
        cookie_rotation=_cookie_rotation(cookies, updated_cookies),
        trigger_steps=trigger_steps,
    )


async def _install_cdp_capture(page: Any, *, state: CaptureState, clock: StageClock) -> None:
    try:
        session = await page.context.new_cdp_session(page)
        await session.send("Network.enable")
    except Exception:
        return

    def on_cdp_request(params: Any) -> None:
        if not isinstance(params, dict):
            return
        req = params.get("request")
        if not isinstance(req, dict):
            return
        auth = _header(req.get("headers"), "authorization")
        if not auth.startswith("Bearer "):
            return
        state.consider_token(
            source="cdp",
            t_ms=clock.now_ms(),
            stage=clock.current,
            method=str(req.get("method", "")),
            url=str(req.get("url", "")),
            token=auth[7:],
        )

    session.on("Network.requestWillBeSent", on_cdp_request)


def _print_run(result: RunResult, *, verbose: bool) -> None:
    graph = result.capture.graph_token
    graph_tag = "scoped" if result.capture.has_scoped_graph else ("plain" if graph else "MISS")
    graph_exp = exp_remaining_minutes(graph) if graph else None
    csa_tag = "ok" if result.capture.has_csa else "MISS"
    cookie = result.cookie_rotation
    validation_tag = (
        "ok" if result.message_validation_ok else ("fail" if result.validations else "skip")
    )
    print(
        f"{result.run_index:>3}  {result.elapsed_ms:>7}  "
        f"graph:{graph_tag:<6} exp={graph_exp if graph_exp is not None else '-':>4}m  "
        f"csa:{csa_tag:<4}  msg_validation:{validation_tag:<4}  "
        f"ctx={result.capture.source_counts.get('context', 0):>3} "
        f"cdp={result.capture.source_counts.get('cdp', 0):>3}  "
        f"cookies:+{cookie.added}/ext{cookie.extended}"
    )
    if result.expired_redirect:
        print(f"     login_redirect final={short_url(result.final_url, 90)}")
    if verbose:
        max_ext = (
            f"{cookie.max_extension_days:.2f}d" if cookie.max_extension_days is not None else "-"
        )
        print(
            "     cookie_rotation: "
            f"sso_before={cookie.before_sso} sso_after={cookie.after_sso} "
            f"added={cookie.added} extended={cookie.extended} "
            f"max_extension={max_ext}"
        )
    for step in result.trigger_steps:
        print(f"     step: {step}")

    graph_events = [e for e in result.capture.token_events if "graph.microsoft.com" in e.url]
    if graph_events:
        print("     graph events:")
        for event in graph_events[:10]:
            marker = "*" if event.has_required_scope else "-"
            name = f" {event.app_name[:24]}" if event.app_name else ""
            exp = event.exp_min if event.exp_min is not None else "-"
            print(
                f"       {event.t_ms:>6}ms {event.source:<7} {marker} "
                f"scp={event.scope_count:<3} exp={exp:>4}m "
                f"fp={event.fingerprint} appid={event.appid}{name} "
                f"{short_url(event.url, 80)}"
            )
        if len(graph_events) > 10:
            print(f"       ... +{len(graph_events) - 10} more graph events")

    if result.validations:
        print("     validation:")
        for validation in result.validations:
            status = validation.status if validation.status is not None else "-"
            ok = "Y" if validation.ok else "N"
            detail = f" {validation.detail}" if validation.detail else ""
            print(f"       {ok} {validation.name:<28} status={status}{detail}")
    else:
        print("     validation: skipped (pass --validate-graph to enable)")


def _print_summary(results: list[RunResult]) -> None:
    elapsed = [r.elapsed_ms for r in results]
    successes = sum(1 for r in results if r.success)
    scoped = sum(1 for r in results if r.capture.has_scoped_graph)
    csa = sum(1 for r in results if r.capture.has_csa)
    messages = sum(1 for r in results if r.message_validation_ok)
    validations_enabled = any(r.validations for r in results)
    print()
    print("[summary]")
    print(f"  success              {successes}/{len(results)}")
    print(f"  scoped_graph_capture {scoped}/{len(results)}")
    print(f"  csa_capture          {csa}/{len(results)}")
    if validations_enabled:
        print(f"  graph_messages_ok    {messages}/{len(results)}")
    else:
        print("  graph_messages_ok    skipped")
    print(
        "  elapsed_ms           "
        f"min={min(elapsed)} p50={int(statistics.median(elapsed))} max={max(elapsed)}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Graph refresh via current Teams UI.")
    parser.add_argument("--runs", type=int, default=1, help="Number of repeated runs.")
    parser.add_argument("--timeout", type=int, default=60, help="Per-run wall-clock budget.")
    parser.add_argument("--url", default=None, help="Override refresh URL.")
    parser.add_argument("--max-validate-teams", type=int, default=3)
    parser.add_argument("--validate-graph", action="store_true")
    parser.add_argument("--strict-exit", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--cdp", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--block-service-workers", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    teams_creds = load_creds()
    refresh_url = str(args.url or teams_creds.get("refresh_url") or TEAMS_URL)
    team_ids_raw = teams_creds.get("team_ids", []) or []
    team_ids = [str(t) for t in team_ids_raw if t]
    all_cookies = load_cookies()
    cookies = filter_sso(all_cookies)

    print(f"[setup] refresh_url={refresh_url}")
    print(f"[setup] sso_cookies={len(cookies)} all_cookies={len(all_cookies)}")
    print(
        f"[setup] team_ids={len(team_ids)} cdp={args.cdp} "
        f"timeout={args.timeout}s verbose={args.verbose}"
    )
    if args.block_service_workers:
        print("[setup] service_workers=block")
    print()
    print(
        f"{'run':>3}  {'elapsed':>7}  {'graph':<24} {'csa':<8} "
        f"{'validation':<19} {'capture':<15} cookies"
    )
    print("-" * 110)

    results: list[RunResult] = []
    for idx in range(1, max(1, int(args.runs)) + 1):
        result = await run_once(
            run_index=idx,
            cookies=cookies,
            refresh_url=refresh_url,
            team_ids=team_ids,
            timeout_seconds=int(args.timeout),
            cdp_enabled=bool(args.cdp),
            block_service_workers=bool(args.block_service_workers),
            screenshots=bool(args.screenshots),
            max_validate_teams=max(1, int(args.max_validate_teams)),
            validate_graph=bool(args.validate_graph),
            verbose=bool(args.verbose),
        )
        results.append(result)
        _print_run(result, verbose=bool(args.verbose))

    _print_summary(results)
    return 0 if not args.strict_exit or all(r.success for r in results) else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
