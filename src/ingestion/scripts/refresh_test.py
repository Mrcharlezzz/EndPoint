"""Standalone Teams token refresh testbed.

Self-contained copy of the CLI's silent-refresh path
(``ingestion.cli.auth.teams_login.try_silent_refresh``) so we can iterate
on the Playwright bits without touching the CLI or the daemon.

Reads cookies + refresh_url from the CLI's local files; runs ONE headless
Playwright refresh; prints what got captured + how long it took. No
write-back, no daemon, no postgres.

Usage:
    cd src/ingestion
    uv run python scripts/refresh_test.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CREDENTIALS = Path.home() / ".config" / "ingest" / "credentials.json"
DEFAULT_COOKIES = Path.home() / ".config" / "ingest" / "sessions" / "teams_cookies.json"
TEAMS_URL = "https://teams.microsoft.com"
REQUIRED_GRAPH_SCOPE = "ChannelMessage.Read.All"

# Same set as ``ingestion.cli.auth.teams_login.try_silent_refresh``: only
# the Microsoft SSO domains. Stripping the Teams app cookies (authtoken,
# skypetoken_asm) forces Teams JS to re-auth against the SSO session
# instead of replaying cached app tokens.
SSO_DOMAINS = {
    ".login.microsoftonline.com",
    "login.microsoftonline.com",
    ".login.live.com",
    ".microsoftonline.com",
}


def _decode_jwt(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        import jwt  # PyJWT, already a dep

        return jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        return None


def _token_has_scope(token: str, scope: str) -> bool:
    decoded = _decode_jwt(token)
    if decoded is None:
        return False
    return scope in str(decoded.get("scp", "")).split()


def _token_expiry(token: str) -> int:
    decoded = _decode_jwt(token)
    if decoded is None:
        return 0
    return int(decoded.get("exp", 0))


def _exp_remaining_minutes(token: str) -> int | None:
    exp = _token_expiry(token)
    if exp == 0:
        return None
    return int((datetime.fromtimestamp(exp, tz=UTC) - datetime.now(tz=UTC)).total_seconds() / 60)


def _make_request_interceptor(captured_tokens: dict[str, str]):
    """Same logic as CLI's ``_make_request_interceptor`` (silent variant)."""

    def on_request(request) -> None:  # noqa: ANN001
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return
        token = auth_header[7:]
        url = request.url
        if "graph.microsoft.com" in url:
            existing = captured_tokens.get("graph_token")
            should_replace = False
            if existing is None:
                should_replace = True
            elif _token_has_scope(token, REQUIRED_GRAPH_SCOPE) and not _token_has_scope(
                existing, REQUIRED_GRAPH_SCOPE
            ):
                should_replace = True
            elif _token_expiry(token) > _token_expiry(existing):
                should_replace = True
            if should_replace:
                captured_tokens["graph_token"] = token
        elif "/api/csa/" in url and "csa_token" not in captured_tokens:
            captured_tokens["csa_token"] = token

    return on_request


def _wait_for_csa_token(
    page,  # noqa: ANN001
    browser,  # noqa: ANN001
    captured_tokens: dict[str, str],
    *,
    timeout_seconds: int = 30,
) -> None:
    """Same logic as CLI's ``_wait_for_csa_token``."""
    for _ in range(timeout_seconds):
        page.wait_for_timeout(1000)
        if not browser.is_connected():
            break
        if "csa_token" in captured_tokens and "graph_token" in captured_tokens:
            break
    if "csa_token" in captured_tokens and "graph_token" not in captured_tokens:
        for _ in range(10):
            page.wait_for_timeout(1000)
            if "graph_token" in captured_tokens or not browser.is_connected():
                break


def silent_refresh(refresh_url: str, sso_cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Run one silent refresh. Returns whatever was captured (may be empty)."""
    from playwright.sync_api import sync_playwright

    captured: dict[str, str] = {}
    on_request = _make_request_interceptor(captured)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_cookies(sso_cookies)
            page = context.new_page()
            page.on("request", on_request)
            page.goto(refresh_url)
            with contextlib.suppress(Exception):
                _wait_for_csa_token(page, browser, captured, timeout_seconds=30)
        finally:
            browser.close()

    return captured


def main() -> int:
    if not DEFAULT_CREDENTIALS.exists():
        print(f"error: credentials.json not found at {DEFAULT_CREDENTIALS}", file=sys.stderr)
        return 2
    if not DEFAULT_COOKIES.exists():
        print(f"error: cookies file not found at {DEFAULT_COOKIES}", file=sys.stderr)
        return 2

    teams_creds = json.loads(DEFAULT_CREDENTIALS.read_text()).get("teams", {})
    refresh_url = teams_creds.get("refresh_url") or TEAMS_URL
    all_cookies = json.loads(DEFAULT_COOKIES.read_text())
    sso_cookies = [c for c in all_cookies if c.get("domain") in SSO_DOMAINS]

    print(
        f"[setup]   {len(sso_cookies)}/{len(all_cookies)} cookies kept "
        f"({len({c.get('domain', '') for c in sso_cookies})} domains)"
    )
    print(f"[setup]   refresh_url={refresh_url}")

    started = time.monotonic()
    captured = silent_refresh(refresh_url, sso_cookies)
    elapsed = time.monotonic() - started

    print(f"[done]    elapsed={elapsed:.2f}s")
    rc = 0
    for key in ("graph_token", "csa_token"):
        token = captured.get(key)
        if token is None:
            print(f"[result]  {key}: MISSING")
            rc = 1
            continue
        mins = _exp_remaining_minutes(token)
        scope_note = ""
        if key == "graph_token":
            scoped = _token_has_scope(token, REQUIRED_GRAPH_SCOPE)
            scope_note = " (channel-scoped)" if scoped else " (homepage-scoped)"
        suffix = f"~{mins}m" if mins is not None else "no exp"
        print(f"[result]  {key}: captured, expires in {suffix}{scope_note}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
