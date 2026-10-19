"""Login session — headless Playwright with screenshot streaming.

Hosts the interactive Microsoft sign-in (email + password + MFA + KMSI)
inside a headless Chromium and streams JPEG frames over WebSocket to
the user's browser. As soon as the SSO cookie session is established,
the Playwright page is closed and tokens are minted entirely via
:mod:`ingestion.auth.silent_oidc` — no UI walking, no
``Assignments → Past due`` clicks, no per-team channel visits.

The token-mint chain mirrors the daemon's ``HttpxTokenRefresher`` and
the CLI's ``try_silent_refresh``:

1. ``silent_token_refresh(sso_cookies, bootstrap_config)`` — bootstrap
   a ``refresh_token`` via the Skype audience.
2. ``refresh_with_token(rt, graph_config)`` — Graph access token.
3. ``refresh_with_token(rt, csa_config)`` — CSA access token
   (best-effort: failure does not invalidate the Graph capture).
4. One ``GET /me/joinedTeams`` Graph call to discover ``team_ids``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import tempfile
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_TEAMS_URL = "https://teams.cloud.microsoft"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = 300
_FRAME_INTERVAL_MS = 66  # ~15 fps
_COOKIE_POLL_EVERY_TICKS = 8  # ~530 ms between context.cookies() calls
_LOGIN_DETECT_REQUIRED_HITS = 2  # consecutive positive polls before acting

# SSO cookie domains used by the silent-OIDC bootstrap. Mirrors the
# allowlist in ``ingestion.cli.auth.teams_login.try_silent_refresh`` —
# keep these in sync if Microsoft adds (or drops) a login host.
SSO_COOKIE_DOMAINS: frozenset[str] = frozenset(
    {
        ".login.microsoftonline.com",
        "login.microsoftonline.com",
        ".login.live.com",
        ".microsoftonline.com",
    }
)

# Cookies that prove a successful sign-in. ``ESTSAUTH`` is session-only;
# ``ESTSAUTHPERSISTENT`` is set when the user picks "Stay signed in".
# Either one combined with a redirect off the Entra login hosts is a
# reliable signal that the Entra leg of the auth flow finished.
_AUTH_PROOF_COOKIES: frozenset[str] = frozenset({"ESTSAUTH", "ESTSAUTHPERSISTENT"})

# Hosts on which the user is *still* in the middle of the Entra flow.
# Once the page leaves these, the redirect back to Teams has happened.
_AUTH_HOSTS: frozenset[str] = frozenset(
    {
        "login.microsoftonline.com",
        "login.live.com",
        "login.microsoft.com",
        "login.windows.net",
    }
)

_LOG_DIR = Path(tempfile.gettempdir()) / "endpoint-headless-logs"

# JS snippet: identify the element at given coordinates. Used purely
# for click-input audit logging — the algorithm itself never inspects
# the DOM.
_ELEMENT_AT_POINT_JS = """
([x, y]) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return {tag: '(none)', text: '', aria: '', id: '', cls: '', href: ''};
    return {
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || '').trim().slice(0, 80),
        aria: el.getAttribute('aria-label') || '',
        id: el.id || '',
        cls: el.className ? String(el.className).slice(0, 100) : '',
        href: el.getAttribute('href') || '',
    };
}
"""


class SessionLog:
    """Structured session log — writes every browser event to a timestamped file."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._t0 = _time.monotonic()
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%S")
        self.path = _LOG_DIR / f"endpoint-headless-{ts}-{session_id}.log"
        self._f = self.path.open("a", encoding="utf-8")
        self.write("SESSION", f"started  id={session_id}")
        logger.info("[session %s] log file → %s", session_id, self.path)

    def write(self, tag: str, msg: str) -> None:
        elapsed = _time.monotonic() - self._t0
        wall = _time.strftime("%H:%M:%S")
        line = f"{wall} +{elapsed:>7.2f}s  {tag:<14} {msg}\n"
        self._f.write(line)
        self._f.flush()

    def close(self) -> None:
        self.write("SESSION", "closed")
        self._f.close()

    # ── Playwright event handlers ─────────────────────────────────────

    def on_request(self, request: Any) -> None:
        method: str = request.method
        url: str = request.url
        res_type: str = request.resource_type
        if res_type in ("document", "xhr", "fetch"):
            self.write("REQ", f"{method} {res_type} {url[:200]}")

    def on_response(self, response: Any) -> None:
        status: int = response.status
        url: str = response.url
        if status >= 400:
            self.write("RESP", f"{status} {url[:200]}")

    def on_console(self, msg: Any) -> None:
        level: str = msg.type
        text: str = msg.text
        if level in ("error", "warning"):
            self.write(f"CONSOLE:{level}", text[:300])

    def on_page_error(self, error: Any) -> None:
        self.write("JS_ERROR", str(error)[:400])

    def on_frame_navigated(self, frame: Any) -> None:
        url: str = "(detached)"
        with contextlib.suppress(Exception):
            url = frame.url
        self.write("FRAME_NAV", url[:200])

    def on_load(self, page: Any) -> None:
        url = "(unknown)"
        with contextlib.suppress(Exception):
            url = page.url
        self.write("PAGE_LOAD", url[:200])

    def on_close(self, _page: Any) -> None:
        self.write("PAGE_CLOSE", "(page closed)")

    def on_popup(self, page: Any) -> None:
        url = "(unknown)"
        with contextlib.suppress(Exception):
            url = page.url
        self.write("POPUP", url[:200])

    def on_download(self, download: Any) -> None:
        self.write("DOWNLOAD", f"{download.url} → {download.suggested_filename}")

    def on_worker(self, worker: Any) -> None:
        self.write("WORKER", f"new web worker: {worker.url[:200]}")

    def on_dialog(self, dialog: Any) -> None:
        self.write("DIALOG", f"type={dialog.type} msg={dialog.message[:200]}")
        with contextlib.suppress(Exception):
            dialog.dismiss()

    def on_new_page(self, page: Any) -> None:
        self.write("NEW_PAGE", "tab opened")
        self._attach_page_hooks(page)

    def _attach_page_hooks(self, page: Any) -> None:
        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("console", self.on_console)
        page.on("pageerror", self.on_page_error)
        page.on("framenavigated", self.on_frame_navigated)
        page.on("load", self.on_load)
        page.on("close", self.on_close)
        page.on("popup", self.on_popup)
        page.on("download", self.on_download)
        page.on("worker", self.on_worker)
        page.on("dialog", self.on_dialog)

    def attach(self, context: Any, page: Any) -> None:
        """Attach logging hooks to a browser context and its first page."""
        self._attach_page_hooks(page)
        context.on("page", self.on_new_page)

    # ── User input logging ────────────────────────────────────────────

    def log_click(self, page: Any, x: float, y: float) -> None:
        info = _element_at_point(page, x, y)
        self.write("INPUT:click", f"({x:.0f},{y:.0f}) → {info}")

    def log_type(self, text: str) -> None:
        self.write("INPUT:type", repr(text))

    def log_press(self, key: str) -> None:
        self.write("INPUT:press", key)

    def log_scroll(self, dx: float, dy: float) -> None:
        self.write("INPUT:scroll", f"dx={dx:.0f} dy={dy:.0f}")

    # ── Status / phase logging ────────────────────────────────────────

    def log_status(self, old: str, new: str) -> None:
        self.write("STATUS", f"{old} → {new}")

    def log_token(self, name: str) -> None:
        self.write("TOKEN", f"captured: {name}")

    def log_phase(self, msg: str) -> None:
        self.write("PHASE", msg)


# ── Helper functions ──────────────────────────────────────────────────


def _element_at_point(page: Any, x: float, y: float) -> str:
    try:
        info: dict[str, str] = page.evaluate(_ELEMENT_AT_POINT_JS, [x, y])
        parts = [info.get("tag", "?")]
        if info.get("id"):
            parts.append(f"#{info['id']}")
        if info.get("aria"):
            parts.append(f'aria="{info["aria"]}"')
        if info.get("href"):
            parts.append(f'href="{info["href"][:60]}"')
        text = info.get("text", "")
        if text:
            parts.append(f'"{text[:50]}"')
        return " ".join(parts)
    except Exception:
        return "(evaluate failed)"


def _auto_click_kmsi(frame: Any, slog: SessionLog) -> None:
    """Auto-click 'Yes' on the 'Stay signed in?' (KMSI) prompt.

    Choosing "Yes" sets ``ESTSAUTHPERSISTENT`` on the SSO cookie domain,
    which is what later silent-OIDC ``prompt=none`` calls rely on.
    """
    try:
        url = frame.url
        if "login.microsoftonline.com" not in url:
            return
        kmsi_title = frame.query_selector("#kmsiTitle")
        if not kmsi_title:
            return
        btn = frame.query_selector("#idSIButton9")
        if btn and btn.is_visible():
            slog.write("KMSI", "clicking 'Stay signed in? Yes'")
            btn.click()
    except Exception:
        pass


def filter_sso_cookies(cookies: list[Any]) -> list[dict[str, Any]]:
    """Return the SSO subset of a Playwright cookie jar.

    Only cookies whose ``domain`` matches :data:`SSO_COOKIE_DOMAINS` are
    included. The result has the same shape as the input items (Playwright
    cookie dicts with at least ``name``, ``value``, ``domain``, ``path``)
    so it can be fed straight into
    :func:`ingestion.auth.silent_oidc.silent_token_refresh`.
    """
    out: list[dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        cookie = cast("dict[str, Any]", c)
        domain = cookie.get("domain")
        if isinstance(domain, str) and domain in SSO_COOKIE_DOMAINS:
            out.append(cookie)
    return out


def _login_completed(context: Any, page: Any) -> bool:
    """Heuristic: the Entra leg of the auth flow has finished.

    Two conditions, both required:

    * The current page URL is no longer on an Entra login host (so the
      browser has been redirected back to Teams).
    * The cookie jar carries ``ESTSAUTH`` or ``ESTSAUTHPERSISTENT`` on
      one of the SSO domains (so a session was actually established —
      not just a back navigation off the login page).

    Either condition alone is unreliable: a stale tab can already be on
    teams.microsoft.com without any auth cookies, and Entra also sets
    transient cookies during the credential-entry steps before sign-in
    actually succeeds.
    """
    try:
        url = page.url or ""
    except Exception:
        return False
    host = urlparse(url).netloc
    if not host or host in _AUTH_HOSTS:
        return False
    try:
        cookies = context.cookies()
    except Exception:
        return False
    for c in cookies:
        if not isinstance(c, dict):
            continue
        cookie = cast("dict[str, Any]", c)
        if cookie.get("domain") not in SSO_COOKIE_DOMAINS:
            continue
        if cookie.get("name") in _AUTH_PROOF_COOKIES:
            return True
    return False


def _mint_refresh_token(sso_cookies: list[dict[str, Any]], slog: SessionLog) -> str | None:
    """Bootstrap a ``refresh_token`` via silent OIDC against Skype.

    Mirrors the cookie-bootstrap step in
    :func:`ingestion.cli.auth.teams_login.try_silent_refresh`: one
    ``/authorize?prompt=none`` round-trip yields an auth code, which the
    ``/token`` exchange swaps for a bundle that includes a
    ``refresh_token`` covering all scopes the SSO session consented to.

    Returns ``None`` (and logs the failure) on any error so callers can
    decide whether to mark the session FAILED or fall back.
    """
    if not sso_cookies:
        slog.write("REFRESH_TOKEN", "skip — no SSO cookies in jar")
        return None

    try:
        from ingestion.auth.silent_oidc import (
            InteractionRequiredError,
            TokenExchangeError,
            silent_token_refresh,
        )
        from ingestion.auth.teams_oidc_configs import bootstrap_config
    except Exception as exc:
        slog.write("REFRESH_TOKEN", f"skip — import failed: {exc}")
        return None

    try:
        bundle = silent_token_refresh(sso_cookies, bootstrap_config())
    except InteractionRequiredError as exc:
        slog.write("REFRESH_TOKEN", f"skip — interaction required: {exc}")
        return None
    except TokenExchangeError as exc:
        slog.write("REFRESH_TOKEN", f"skip — exchange failed: {exc}")
        return None
    except Exception as exc:
        slog.write("REFRESH_TOKEN", f"skip — unexpected error: {exc}")
        return None

    if not bundle.refresh_token:
        slog.write("REFRESH_TOKEN", "skip — bootstrap returned no refresh_token")
        return None

    slog.write("REFRESH_TOKEN", "minted via Skype bootstrap")
    return bundle.refresh_token


def _chain_graph_token(refresh_token: str, slog: SessionLog) -> str | None:
    """Exchange ``refresh_token`` for a Graph access_token. ``None`` on failure."""
    try:
        from ingestion.auth.silent_oidc import (
            InteractionRequiredError,
            TokenExchangeError,
            refresh_with_token,
        )
        from ingestion.auth.teams_oidc_configs import graph_config
    except Exception as exc:
        slog.write("GRAPH_TOKEN", f"skip — import failed: {exc}")
        return None

    try:
        bundle = refresh_with_token(refresh_token, graph_config())
    except InteractionRequiredError as exc:
        slog.write("GRAPH_TOKEN", f"interaction required: {exc}")
        return None
    except TokenExchangeError as exc:
        slog.write("GRAPH_TOKEN", f"exchange failed: {exc}")
        return None
    except Exception as exc:
        slog.write("GRAPH_TOKEN", f"unexpected error: {exc}")
        return None

    slog.log_token("graph_token")
    return bundle.access_token


def _chain_csa_token(refresh_token: str, slog: SessionLog) -> str | None:
    """Exchange ``refresh_token`` for a CSA access_token. Best-effort.

    Failure is logged but never escalated — the chat adapter can re-mint
    CSA on demand later, so the Graph capture stands on its own.
    """
    try:
        from ingestion.auth.silent_oidc import (
            InteractionRequiredError,
            TokenExchangeError,
            refresh_with_token,
        )
        from ingestion.auth.teams_oidc_configs import csa_config
    except Exception as exc:
        slog.write("CSA_TOKEN", f"skip — import failed: {exc}")
        return None

    try:
        bundle = refresh_with_token(refresh_token, csa_config())
    except (InteractionRequiredError, TokenExchangeError) as exc:
        slog.write("CSA_TOKEN", f"skip — {exc}")
        return None
    except Exception as exc:
        slog.write("CSA_TOKEN", f"skip — unexpected error: {exc}")
        return None

    slog.log_token("csa_token")
    return bundle.access_token


def _discover_team_ids(graph_token: str, slog: SessionLog) -> list[str]:
    """Return the user's joined-team IDs via one Graph call. ``[]`` on failure."""
    import httpx

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{_GRAPH_BASE}/me/joinedTeams",
                headers={"Authorization": f"Bearer {graph_token}"},
            )
    except Exception as exc:
        slog.write("TEAMS", f"discovery failed: {exc}")
        return []

    if resp.status_code != 200:
        slog.write("TEAMS", f"discovery {resp.status_code}: {resp.text[:200]}")
        return []

    raw_data: Any = resp.json()
    if not isinstance(raw_data, dict):
        return []
    raw = cast("dict[str, Any]", raw_data)
    teams_value = raw.get("value")
    if not isinstance(teams_value, list):
        return []
    teams_list = cast("list[Any]", teams_value)
    team_ids: list[str] = []
    for team in teams_list:
        if not isinstance(team, dict):
            continue
        team_dict = cast("dict[str, Any]", team)
        tid = team_dict.get("id")
        if isinstance(tid, str) and tid:
            team_ids.append(tid)
    slog.write("TEAMS", f"discovered {len(team_ids)} teams")
    return team_ids


# ── Session types ────────────────────────────────────────────────────


class SessionStatus(StrEnum):
    STARTING = "starting"
    WAITING = "waiting"
    COLLECTING = "collecting"
    CAPTURED = "captured"
    FAILED = "failed"
    CLOSED = "closed"


def _frame_q() -> queue.Queue[bytes]:
    return queue.Queue(maxsize=2)


def _input_q() -> queue.Queue[dict[str, Any]]:
    return queue.Queue()


def _str_dict() -> dict[str, str]:
    return {}


def _obj_list() -> list[Any]:
    return []


def _str_list() -> list[str]:
    return []


@dataclass
class LoginSession:
    """Headless Playwright session with screenshot streaming.

    Playwright is used purely as an interactive sign-in host (MFA, KMSI).
    Once the SSO cookies are established, all token minting is done over
    plain HTTPS via :mod:`ingestion.auth.silent_oidc`.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: SessionStatus = SessionStatus.STARTING
    tokens: dict[str, str] = field(default_factory=_str_dict)
    cookies: list[Any] = field(default_factory=_obj_list)
    team_ids: list[str] = field(default_factory=_str_list)
    refresh_token: str | None = None
    error: str | None = None

    frames: queue.Queue[bytes] = field(default_factory=_frame_q, repr=False)
    inputs: queue.Queue[dict[str, Any]] = field(default_factory=_input_q, repr=False)

    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    async def start(self, timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        self._task = asyncio.create_task(
            asyncio.to_thread(self._run, timeout_seconds),
        )

    # ------------------------------------------------------------------
    # Playwright thread
    # ------------------------------------------------------------------

    def _run(self, timeout_seconds: int) -> None:
        try:
            self._run_inner(timeout_seconds)
        except Exception as exc:
            logger.error("Session %s failed: %s", self.id, exc, exc_info=True)
            self.status = SessionStatus.FAILED
            self.error = str(exc)

    def _run_inner(self, timeout_seconds: int) -> None:
        slog = SessionLog(self.id)
        try:
            self._run_browser(timeout_seconds, slog)
        finally:
            slog.close()

    def _run_browser(self, timeout_seconds: int, slog: SessionLog) -> None:
        from playwright.sync_api import sync_playwright

        with (
            tempfile.TemporaryDirectory(prefix="pw-hl-") as user_data_dir,
            sync_playwright() as p,
        ):
            slog.write(
                "BROWSER",
                f"launching Chromium (headless, user_data={user_data_dir})",
            )
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=True,
                viewport={"width": 460, "height": 640},
                args=["--disable-infobars", "--no-first-run"],
            )

            page = context.pages[0] if context.pages else context.new_page()
            slog.attach(context, page)
            page.on(
                "framenavigated",
                lambda frame: _auto_click_kmsi(frame, slog),
            )

            self._set_status(SessionStatus.WAITING, slog)
            slog.write("NAV", f"goto {_TEAMS_URL}")
            page.goto(_TEAMS_URL, wait_until="domcontentloaded")

            self._main_loop(page, context, timeout_seconds, slog)

    def _set_status(self, new: SessionStatus, slog: SessionLog) -> None:
        old = self.status
        self.status = new
        slog.log_status(old.value, new.value)

    # ------------------------------------------------------------------
    # Main loop — stream frames, replay input, watch for SSO cookies
    # ------------------------------------------------------------------

    def _main_loop(
        self,
        page: Any,
        context: Any,
        timeout_seconds: int,
        slog: SessionLog,
    ) -> None:
        ticks = 0
        max_ticks = int(timeout_seconds * 1000 / _FRAME_INTERVAL_MS)
        last_url = ""
        positive_polls = 0
        login_detected = False

        while not self._cancel.is_set() and ticks < max_ticks and not login_detected:
            self._process_inputs(page, slog)

            frame_bytes: bytes | None = None
            with contextlib.suppress(Exception):
                frame_bytes = page.screenshot(type="jpeg", quality=85)
            if frame_bytes is not None:
                while not self.frames.empty():
                    with contextlib.suppress(queue.Empty):
                        self.frames.get_nowait()
                with contextlib.suppress(queue.Full):
                    self.frames.put_nowait(frame_bytes)

            with contextlib.suppress(Exception):
                cur_url = page.url
                if cur_url != last_url:
                    slog.write("URL", cur_url)
                    last_url = cur_url

            if ticks % _COOKIE_POLL_EVERY_TICKS == 0:
                if _login_completed(context, page):
                    positive_polls += 1
                    if positive_polls >= _LOGIN_DETECT_REQUIRED_HITS:
                        login_detected = True
                        slog.log_phase(f"login detected at tick {ticks} (URL={last_url[:120]})")
                else:
                    positive_polls = 0

            try:
                page.wait_for_timeout(_FRAME_INTERVAL_MS)
            except Exception:
                break
            ticks += 1

        if login_detected:
            self._set_status(SessionStatus.COLLECTING, slog)
            with contextlib.suppress(Exception):
                self.cookies = context.cookies()
            self._close_context(context)
            self._mint_and_finalize(slog)
        else:
            with contextlib.suppress(Exception):
                self.cookies = context.cookies()
            self._close_context(context)
            if self._cancel.is_set():
                self._set_status(SessionStatus.CLOSED, slog)
            else:
                self._set_status(SessionStatus.FAILED, slog)
                self.error = "Login timed out"
                slog.log_phase("login not detected before timeout")

    def _mint_and_finalize(self, slog: SessionLog) -> None:
        """Run the silent-OIDC chain over the captured SSO cookies.

        Failure paths:

        * No SSO cookies, bootstrap fails, or chain fails for Graph →
          status=FAILED. The user can retry interactively.
        * Bootstrap succeeds but CSA chain fails → status=CAPTURED with
          ``csa_token`` absent. The chat adapter can mint CSA on demand.
        """
        sso_cookies = filter_sso_cookies(self.cookies)
        slog.log_phase(f"silent OIDC chain begins (sso_cookies={len(sso_cookies)})")

        refresh_token = _mint_refresh_token(sso_cookies, slog)
        if not refresh_token:
            self._set_status(SessionStatus.FAILED, slog)
            self.error = "Could not bootstrap refresh_token"
            return

        graph_token = _chain_graph_token(refresh_token, slog)
        if not graph_token:
            self._set_status(SessionStatus.FAILED, slog)
            self.error = "Could not mint graph_token"
            return

        csa_token = _chain_csa_token(refresh_token, slog)
        team_ids = _discover_team_ids(graph_token, slog)

        tokens: dict[str, str] = {"graph_token": graph_token}
        if csa_token:
            tokens["csa_token"] = csa_token
        self.tokens = tokens
        self.refresh_token = refresh_token
        self.team_ids = team_ids

        slog.log_phase(
            f"CAPTURED: tokens={list(tokens.keys())} teams={len(team_ids)} refresh_token=yes"
        )
        self._set_status(SessionStatus.CAPTURED, slog)

    @staticmethod
    def _close_context(context: Any) -> None:
        with contextlib.suppress(Exception):
            context.close()

    def _process_inputs(self, page: Any, slog: SessionLog) -> None:
        """Replay all queued input events on the Playwright page."""
        while not self.inputs.empty():
            with contextlib.suppress(queue.Empty):
                evt = self.inputs.get_nowait()
                kind = evt.get("type", "")
                if kind == "click":
                    x, y = float(evt["x"]), float(evt["y"])
                    slog.log_click(page, x, y)
                    with contextlib.suppress(Exception):
                        page.mouse.click(x, y)
                elif kind == "type":
                    text = str(evt["text"])
                    slog.log_type(text)
                    with contextlib.suppress(Exception):
                        page.keyboard.type(text)
                elif kind == "press":
                    key = str(evt["key"])
                    slog.log_press(key)
                    with contextlib.suppress(Exception):
                        page.keyboard.press(key)
                elif kind == "scroll":
                    dx = float(evt.get("dx", 0))
                    dy = float(evt.get("dy", 0))
                    slog.log_scroll(dx, dy)
                    with contextlib.suppress(Exception):
                        page.mouse.wheel(dx, dy)

    async def cleanup(self) -> None:
        self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.status = SessionStatus.CLOSED
