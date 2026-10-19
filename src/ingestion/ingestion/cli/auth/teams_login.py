"""Teams login — Playwright browser automation with network interception.

Opens Chromium to teams.microsoft.com, intercepts Bearer tokens from
network requests, decodes JWTs, and auto-discovers joined teams.

Supports two modes:
- Interactive (headed): user logs in manually with MFA
- Silent refresh (headless): replays saved SSO cookies to get fresh tokens
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import httpx
from rich.console import Console

from ingestion.cli.display import (
    SYM_FAIL,
    SYM_OK,
    SYM_PENDING,
    print_error_panel,
    print_status,
)
from ingestion.cli.state import save_credentials

console = Console(stderr=True)

_TEAMS_URL = "https://teams.microsoft.com"

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Browser, Page, Request


def _token_expiry(token: str) -> int:
    """Return the exp claim of a JWT as a Unix timestamp, or 0 on failure."""
    from ingestion.cli.jwt_utils import get_expiry_timestamp

    return get_expiry_timestamp(token)


def _token_has_scope(token: str, scope: str) -> bool:
    """Check if a JWT token contains a specific scope."""
    from ingestion.cli.jwt_utils import has_scope

    return has_scope(token, scope)


def _make_request_interceptor(
    captured_tokens: dict[str, str],
    *,
    silent: bool = False,
) -> Callable[[Request], None]:
    """Create a request interceptor callback for Playwright.

    Keeps upgrading graph_token if a newer one has ChannelMessage.Read.All
    (Teams requests richer scopes when you navigate into a channel).
    """

    def on_request(request: Request) -> None:
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
            elif _token_has_scope(token, "ChannelMessage.Read.All") and not _token_has_scope(
                existing, "ChannelMessage.Read.All"
            ):
                # Upgrade to a richer-scoped token
                should_replace = True
            elif _token_expiry(token) > _token_expiry(existing):
                # Always prefer the token with the later expiry
                should_replace = True

            if should_replace:
                captured_tokens["graph_token"] = token
                if not silent:
                    console.print(f"  {SYM_OK} Captured graph_token")

        # CSA data token: only capture from actual CSA data endpoints,
        # not authsvc or other management endpoints (different tokens).
        if "/api/csa/" in url and "csa_token" not in captured_tokens:
            captured_tokens["csa_token"] = token
            if not silent:
                console.print(f"  {SYM_OK} Captured csa_token")

    return on_request


def _wait_for_tokens(
    page: Page,
    browser: Browser,
    captured_tokens: dict[str, str],
    *,
    timeout_seconds: int = 30,
) -> None:
    """Wait for tokens to appear in intercepted requests."""
    elapsed = 0
    while "graph_token" not in captured_tokens and elapsed < timeout_seconds:
        page.wait_for_timeout(1000)
        elapsed += 1
        if not page.url or browser.is_connected() is False:
            break

    # Give extra time for CSA token
    if "graph_token" in captured_tokens and "csa_token" not in captured_tokens:
        for _ in range(min(10, timeout_seconds - elapsed)):
            page.wait_for_timeout(1000)
            if "csa_token" in captured_tokens:
                break

    # Try to trigger ChannelMessage.Read.All by navigating into a channel
    if "graph_token" in captured_tokens and not _token_has_scope(
        captured_tokens["graph_token"], "ChannelMessage.Read.All"
    ):
        _navigate_to_channel(page, browser, captured_tokens)


def _navigate_to_channel(
    page: Page,
    browser: Browser,
    captured_tokens: dict[str, str],
) -> None:
    """Navigate into a Teams channel to trigger message-scoped token request."""
    graph_token = captured_tokens.get("graph_token", "")
    if not graph_token:
        return

    # Discover first team + channel via Graph API
    try:
        team_ids, _ = _discover_teams(graph_token)
        if not team_ids:
            return

        import httpx

        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"https://graph.microsoft.com/v1.0/teams/{team_ids[0]}/channels",
                headers={"Authorization": f"Bearer {graph_token}"},
            )
            if r.status_code != 200:
                return
            channels = r.json().get("value", [])
            if not channels:
                return

        ch_id = channels[0]["id"]
        ch_name = channels[0].get("displayName", "General")

        # Navigate to the channel in the Teams web client
        channel_url = (
            f"https://teams.microsoft.com/l/channel/{ch_id}/{ch_name}?groupId={team_ids[0]}"
        )
        console.print(f"  {SYM_PENDING} Navigating to #{ch_name} for message scope...")
        page.goto(channel_url)

        # Wait for upgraded token
        for _ in range(15):
            page.wait_for_timeout(1000)
            if not browser.is_connected():
                break
            if _token_has_scope(captured_tokens.get("graph_token", ""), "ChannelMessage.Read.All"):
                break
    except Exception:
        pass


def run_teams_login() -> None:
    """Launch headed Playwright browser for Teams SSO login."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print_error_panel(
            "Playwright not installed",
            "Teams login requires a browser for authentication.\n\n"
            "Install with:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n\n"
            "Then retry: ingest login teams",
        )
        return

    print_status(SYM_PENDING, "Opening browser for Microsoft login...")
    print_status(SYM_PENDING, "Waiting for authentication...")

    captured_tokens: dict[str, str] = {}
    on_request = _make_request_interceptor(captured_tokens)
    saved_cookies: list[dict] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.on("request", on_request)
            page.goto(_TEAMS_URL)

            with contextlib.suppress(Exception):
                _wait_for_tokens(page, browser, captured_tokens, timeout_seconds=300)

            # Save session cookies before closing
            with contextlib.suppress(Exception):
                saved_cookies = [dict(cookie) for cookie in context.cookies()]
            browser.close()

    except Exception as exc:
        print_status(SYM_FAIL, f"Browser error: {exc}")
        return

    if "graph_token" not in captured_tokens:
        print_status(SYM_FAIL, "No tokens captured. Login may have failed.")
        return

    graph_token = captured_tokens["graph_token"]
    csa_token = captured_tokens.get("csa_token")

    _print_token_info(graph_token, "graph_token")
    if csa_token:
        _print_token_info(csa_token, "csa_token")

    # Discover teams
    print_status(SYM_PENDING, "Discovering teams...")
    team_ids, team_names = _discover_teams(graph_token)

    if team_names:
        print_status(SYM_OK, f"Found {len(team_names)} teams:")
        for name in team_names:
            console.print(f"    [dim]•[/dim] {name}")
    else:
        console.print("  [dim]No teams found[/dim]")

    # Build a channel URL for fast silent refresh
    refresh_url = _build_refresh_url(graph_token, team_ids)

    creds: dict = {
        "graph_token": graph_token,
        "team_ids": team_ids,
    }
    if csa_token:
        creds["csa_token"] = csa_token
    if refresh_url:
        creds["refresh_url"] = refresh_url

    save_credentials("teams", creds)

    # Save session cookies for silent refresh
    if saved_cookies:
        from ingestion.cli.state import save_session_cookies

        save_session_cookies("teams", saved_cookies)
        print_status(SYM_OK, "Session cookies saved (enables silent token refresh)")

    console.print("  Saved to ~/.config/ingest/credentials.json")


def try_silent_refresh() -> bool:
    """Attempt silent token refresh using saved SSO cookies + refresh_token.

    Replaces the previous headless-Playwright path. No browser, no DOM:
    one or two HTTPS round-trips against ``login.microsoftonline.com``
    via :mod:`ingestion.auth.silent_oidc`.

    Strategy mirrors :class:`ingestion.daemon.auth.refresh.HttpxTokenRefresher`:

    1. If a ``refresh_token`` is already on disk, redeem it directly for
       a Graph access_token (one round-trip).
    2. If that fails or no refresh_token is persisted, bootstrap by
       running silent OIDC against the Skype audience using the SSO
       cookies, then chain a refresh-token grant for Graph (two
       round-trips).
    3. Best-effort CSA mint via the same refresh_token. Failure here
       does not invalidate the Graph capture.

    Returns ``True`` on success, ``False`` on any failure (caller falls
    back to interactive ``ingest login teams``).
    """
    from ingestion.auth.silent_oidc import (
        InteractionRequiredError,
        TokenBundle,
        TokenExchangeError,
        refresh_with_token,
        silent_token_refresh,
    )
    from ingestion.cli.state import load_credentials, load_session_cookies

    cookies = load_session_cookies("teams")
    if not cookies:
        return False

    sso_domains = {
        ".login.microsoftonline.com",
        "login.microsoftonline.com",
        ".login.live.com",
        ".microsoftonline.com",
    }
    sso_cookies = [c for c in cookies if c.get("domain") in sso_domains]
    if not sso_cookies:
        return False

    existing = load_credentials("teams") or {}
    saved_rt_raw = existing.get("refresh_token")
    saved_rt = saved_rt_raw if isinstance(saved_rt_raw, str) and saved_rt_raw else None

    print_status(SYM_PENDING, "Refreshing Teams tokens (silent OIDC)...")

    graph_config = _SilentOIDCConfigs.graph()
    bootstrap_config = _SilentOIDCConfigs.bootstrap()
    csa_config = _SilentOIDCConfigs.csa()

    # Step 1: try the saved refresh_token (steady-state path)
    graph_bundle: TokenBundle | None = None
    if saved_rt:
        try:
            graph_bundle = refresh_with_token(saved_rt, graph_config)
        except (InteractionRequiredError, TokenExchangeError):
            graph_bundle = None

    # Step 2: cookie bootstrap if step 1 didn't yield a Graph bundle
    if graph_bundle is None:
        try:
            skype_bundle = silent_token_refresh(sso_cookies, bootstrap_config)
        except InteractionRequiredError as exc:
            print_status(SYM_FAIL, f"Session expired: {exc}")
            return False
        except TokenExchangeError as exc:
            print_status(SYM_FAIL, f"Bootstrap failed: {exc}")
            return False
        if not skype_bundle.refresh_token:
            print_status(SYM_FAIL, "Bootstrap returned no refresh_token")
            return False
        try:
            graph_bundle = refresh_with_token(skype_bundle.refresh_token, graph_config)
        except (InteractionRequiredError, TokenExchangeError) as exc:
            print_status(SYM_FAIL, f"Graph chain failed: {exc}")
            return False

    # Step 3: best-effort CSA mint via the (rolled) refresh_token
    csa_token: str | None = (
        existing.get("csa_token") if isinstance(existing.get("csa_token"), str) else None
    )
    chain_rt = graph_bundle.refresh_token or saved_rt
    if chain_rt:
        try:
            csa_bundle = refresh_with_token(chain_rt, csa_config)
            csa_token = csa_bundle.access_token
        except (InteractionRequiredError, TokenExchangeError):
            # Keep whatever CSA token was already on disk; chat adapter
            # can refresh on demand if it actually needs CSA.
            pass

    # Step 4: persist
    team_ids = existing.get("team_ids", [])
    if not team_ids:
        team_ids, _ = _discover_teams(graph_bundle.access_token)

    creds: dict = {
        "graph_token": graph_bundle.access_token,
        "team_ids": team_ids,
    }
    if graph_bundle.refresh_token:
        creds["refresh_token"] = graph_bundle.refresh_token
    elif saved_rt:
        # No rolled value came back; keep the prior refresh_token so the
        # next cycle still has a steady-state path.
        creds["refresh_token"] = saved_rt
    if csa_token:
        creds["csa_token"] = csa_token
    if isinstance(existing.get("refresh_url"), str):
        creds["refresh_url"] = existing["refresh_url"]

    save_credentials("teams", creds)

    _print_token_info(graph_bundle.access_token, "graph_token")
    if csa_token:
        _print_token_info(csa_token, "csa_token")

    print_status(SYM_OK, "Tokens refreshed silently")
    return True


# Microsoft Teams Web Client identifiers — public values, see
# docs/plans/2026-04-25-teams-token-refresh-strategy.md and
# ingestion/daemon/auth/refresh.py for full context.
_TEAMS_WEB_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
_TEAMS_WEB_REDIRECT_URI = "https://teams.microsoft.com/v2/authv2"


class _SilentOIDCConfigs:
    """Cached SilentOIDCConfig builders for the three Teams resources.

    Kept in a class so the import-time cost is paid only when
    ``try_silent_refresh`` is actually called (CLI startup is sensitive
    to import latency).
    """

    @staticmethod
    def graph():  # noqa: ANN205 - return type pulled in lazily
        from ingestion.auth.silent_oidc import SilentOIDCConfig

        return SilentOIDCConfig(
            client_id=_TEAMS_WEB_CLIENT_ID,
            redirect_uri=_TEAMS_WEB_REDIRECT_URI,
            scope=[
                "https://graph.microsoft.com/.default",
                "openid",
                "profile",
                "offline_access",
            ],
        )

    @staticmethod
    def bootstrap():  # noqa: ANN205
        from ingestion.auth.silent_oidc import SilentOIDCConfig

        return SilentOIDCConfig(
            client_id=_TEAMS_WEB_CLIENT_ID,
            redirect_uri=_TEAMS_WEB_REDIRECT_URI,
            scope=[
                "https://api.spaces.skype.com/.default",
                "openid",
                "profile",
                "offline_access",
            ],
        )

    @staticmethod
    def csa():  # noqa: ANN205
        from ingestion.auth.silent_oidc import SilentOIDCConfig

        return SilentOIDCConfig(
            client_id=_TEAMS_WEB_CLIENT_ID,
            redirect_uri=_TEAMS_WEB_REDIRECT_URI,
            scope=[
                "https://chatsvcagg.teams.microsoft.com/.default",
                "openid",
                "profile",
                "offline_access",
            ],
        )


def _print_token_info(token: str, label: str) -> None:
    """Decode and print JWT expiry info."""
    from ingestion.cli.jwt_utils import get_expiry_remaining_minutes

    minutes = get_expiry_remaining_minutes(token)
    if minutes is not None:
        print_status(SYM_OK, f"Captured {label} (expires in {minutes}m)")
    else:
        print_status(SYM_OK, f"Captured {label}")


def _build_refresh_url(graph_token: str, team_ids: list[str]) -> str | None:
    """Find a team with channels and build a direct channel URL for fast refresh.

    Navigating directly to a channel URL forces the Teams web client to issue
    both CSA and Graph API requests, capturing both tokens in ~10s instead of
    never getting the Graph token from the homepage alone.
    """
    if not team_ids or not graph_token:
        return None

    try:
        with httpx.Client(timeout=15) as client:
            for team_id in team_ids[:5]:  # Try first 5 teams
                r = client.get(
                    f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels",
                    headers={"Authorization": f"Bearer {graph_token}"},
                )
                if r.status_code != 200:
                    continue
                channels = r.json().get("value", [])
                # Find General channel (prefer it for reliable CSA access)
                for ch in channels:
                    if ch.get("displayName") == "General":
                        ch_id = ch["id"]
                        return (
                            f"https://teams.microsoft.com/l/channel/{ch_id}/"
                            f"General?groupId={team_id}"
                        )
                # Fallback: use first channel
                if channels:
                    ch = channels[0]
                    ch_id = ch["id"]
                    ch_name = ch.get("displayName", "General")
                    return (
                        f"https://teams.microsoft.com/l/channel/{ch_id}/{ch_name}?groupId={team_id}"
                    )
    except Exception:
        pass
    return None


def _discover_teams(graph_token: str) -> tuple[list[str], list[str]]:
    """Fetch joined teams via Graph /me/joinedTeams."""
    url = "https://graph.microsoft.com/v1.0/me/joinedTeams"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {graph_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return [], []

    teams = data.get("value", [])
    ids = [t.get("id", "") for t in teams if t.get("id")]
    names = [t.get("displayName", t.get("id", "")) for t in teams]
    return ids, names
