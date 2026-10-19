"""Async silent-refresh for Teams credentials.

Ports :func:`ingestion.cli.auth.teams_login.try_silent_refresh` from sync
Playwright to async, and decouples the "launch browser + capture tokens"
step behind a :class:`TokenRefresher` protocol so tests can inject a fake
driver instead of launching Chromium.

A single refresh:
    1. Load the (encrypted) credential blob via :class:`PostgresTokenStore`.
    2. Pass SSO-domain cookies to the refresher.
    3. Capture fresh ``graph_token`` / ``csa_token`` plus updated cookies.
    4. Merge with the previous blob (preserving ``team_ids``, ``refresh_url``,
       etc.) and write it back encrypted ONLY when a scope-satisfying token
       came back. Partial captures (no graph_token or homepage-scope only)
       don't stamp ``last_refreshed_at``.

A refresh is considered successful when the captured graph_token has the
required channel scope. A definitive-expiry signal (post-nav URL redirected
under login.microsoftonline.com, or every cookie rejected) returns
``RefreshOutcome.EXPIRED`` and the caller marks the row expired.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from ingestion.auth.silent_oidc import (
    InteractionRequiredError,
    SilentOIDCConfig,
    TokenExchangeError,
    refresh_with_token,
    silent_token_refresh,
)
from ingestion.cli.jwt_utils import get_expiry_timestamp
from ingestion.daemon.logging import bind_sync_context, get_logger
from ingestion.daemon.reliability.backoff import with_backoff

if TYPE_CHECKING:
    from ingestion.ports import TokenStore

_TEAMS_HOME_URL = "https://teams.microsoft.com"

# Domain suffixes for the Microsoft SSO surface. Deliberately narrow:
# only login.microsoftonline.* / login.live.com style domains. The Teams
# app cookies (authtoken_asm, skypetoken_asm on teams.microsoft.com /
# .asyncgw.teams.microsoft.com, plus telemetry cookies on .microsoft.com)
# carry cached app tokens that may already be stale; replaying them lets
# Teams JS short-circuit instead of re-asking the SSO session, and the
# fresh graph_token never gets minted. The CLI strips these for the same
# reason — see ``ingestion.cli.auth.teams_login.try_silent_refresh``.
_SSO_DOMAIN_SUFFIXES: tuple[str, ...] = (
    ".login.microsoftonline.com",
    ".login.microsoftonline.us",
    ".login.microsoftonline-p.cn",
    ".login.microsoftonline.de",
    ".login.live.com",
    # Bare-host variants for cookies stored without a leading dot.
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.microsoftonline-p.cn",
    "login.microsoftonline.de",
    "login.live.com",
)


def _is_sso_cookie(cookie: dict[str, Any]) -> bool:
    domain = cookie.get("domain", "")
    if not isinstance(domain, str):
        return False
    return any(domain.endswith(suffix) for suffix in _SSO_DOMAIN_SUFFIXES)


# Slice 8: public alias so the polymorphic TeamsRefreshStrategy can
# reuse this helper without tripping pyright's reportPrivateUsage. The
# leading-underscore name is preserved to keep refresh.py's internal
# call sites unchanged. Same pattern applied to capture_is_fresh and
# merge_creds below.
is_sso_cookie = _is_sso_cookie


class RefreshOutcome(StrEnum):
    SUCCESS = "success"
    NO_COOKIES = "no_cookies"
    NO_TOKENS = "no_tokens"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class CapturedTokens:
    """What a :class:`TokenRefresher` returns after one refresh attempt.

    ``cookies`` is held as a tuple so a frozen record cannot be mutated via
    ``.append`` — the interface documents its immutability.

    ``refresh_token`` was added when we replaced the Playwright-driven
    refresher with the silent-OIDC httpx one (see
    ``ingestion.auth.silent_oidc``). The OIDC ``offline_access`` scope
    yields a refresh_token alongside each access_token; persisting it
    across cycles lets the daemon skip the cookie-bootstrap step on
    subsequent refreshes.
    """

    graph_token: str | None = None
    csa_token: str | None = None
    refresh_token: str | None = None
    cookies: tuple[dict[str, Any], ...] = ()
    # ``True`` when the refresher confirmed the session is dead (post-nav URL
    # bounced to login.microsoftonline.com or similar). The caller uses this
    # to flip is_expired on the credential row.
    expired: bool = False

    @property
    def any_token(self) -> bool:
        return (
            self.graph_token is not None
            or self.csa_token is not None
            or self.refresh_token is not None
        )


class TokenRefresher(Protocol):
    """Capture fresh tokens given SSO cookies and (optionally) a refresh_token.

    ``user_email`` is forwarded as an MSAL-style ``login_hint`` when the
    underlying flow benefits from it. ``refresh_token`` is the previously
    persisted token; implementations that support OAuth2 refresh-token
    grants (``HttpxTokenRefresher``) prefer it as a single-round-trip
    fast path, falling back to cookies only when the refresh_token has
    rolled past its inactivity window.
    """

    async def capture_tokens(
        self,
        *,
        cookies: list[dict[str, Any]],
        refresh_url: str,
        timeout_seconds: int,
        refresh_token: str | None = None,
        user_email: str | None = None,
    ) -> CapturedTokens: ...


@dataclass(frozen=True)
class RefreshResult:
    user_email: str
    outcome: RefreshOutcome
    captured: CapturedTokens | None = None
    error: str | None = None


# Scope the rescoped channel-messages pipeline needs. Exported so the driver
# and orchestration both reference a single name.
REQUIRED_GRAPH_SCOPE = "ChannelMessage.Read.All"


async def refresh_one(
    *,
    user_email: str,
    token_store: TokenStore,
    refresher: TokenRefresher,
    timeout_seconds: int = 30,
    max_retries: int = 0,
    user_lock: asyncio.Lock | None = None,
) -> RefreshResult:
    """Run one silent refresh for a single user. Does not raise.

    ``max_retries`` is the number of additional attempts on transient
    NO_TOKENS (driver timeout, no scope-qualified graph_token captured).
    Defaults to 0 — one attempt total. NO_COOKIES and EXPIRED are
    terminal and never retried.

    ``user_lock`` serialises concurrent refreshes for the same user (bulk
    cycle vs force-refresh action). If omitted the caller trusts the pool
    to dedupe.
    """
    log = get_logger(__name__)
    with bind_sync_context(user_email=user_email, platform="teams"):
        if user_lock is not None:
            async with user_lock:
                return await _refresh_one_inner(
                    user_email=user_email,
                    token_store=token_store,
                    refresher=refresher,
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                    log=log,
                )
        return await _refresh_one_inner(
            user_email=user_email,
            token_store=token_store,
            refresher=refresher,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            log=log,
        )


async def _refresh_one_inner(
    *,
    user_email: str,
    token_store: TokenStore,
    refresher: TokenRefresher,
    timeout_seconds: int,
    max_retries: int,
    log: Any,
) -> RefreshResult:
    try:
        creds = await token_store.get_credentials(user_email, "teams")
        if not creds:
            log.warning("refresh.no_credentials")
            return RefreshResult(user_email, RefreshOutcome.NO_COOKIES)
        cookies = creds.get("cookies", [])
        if not cookies:
            log.warning("refresh.no_session_cookies")
            return RefreshResult(user_email, RefreshOutcome.NO_COOKIES)
        refresh_url = creds.get("refresh_url") or _TEAMS_HOME_URL

        sso_cookies = [c for c in cookies if _is_sso_cookie(c)]
        if not sso_cookies:
            log.warning("refresh.no_sso_cookies")
            return RefreshResult(user_email, RefreshOutcome.NO_COOKIES)

        stored_refresh_token = creds.get("refresh_token")
        if not isinstance(stored_refresh_token, str) or not stored_refresh_token:
            stored_refresh_token = None

        # Wrap capture_tokens with the exponential-backoff helper so
        # transient errors (driver timeouts, network blips raised
        # inside capture_tokens) get jittered retries. Jitter spreads
        # concurrent cycle retries so the auth endpoint doesn't see
        # every caller land at the same millisecond on recovery.
        # 1s base matches the audit recommendation. The legacy
        # ``max_retries`` parameter still drives the budget so
        # callers control attempt count without changing the API.
        attempts = max(1, max_retries + 1)
        last_captured: CapturedTokens | None = None

        async def _capture_once() -> CapturedTokens:
            return await asyncio.wait_for(
                refresher.capture_tokens(
                    cookies=sso_cookies,
                    refresh_url=refresh_url,
                    timeout_seconds=timeout_seconds,
                    refresh_token=stored_refresh_token,
                    user_email=user_email,
                ),
                # Bounded budget: capture_tokens's internal polling +
                # page.goto should finish within timeout_seconds; the
                # +10s slack covers pool queue + cookie readback.
                timeout=timeout_seconds + 10,
            )

        for attempt in range(attempts):
            try:
                captured = await with_backoff(
                    _capture_once,
                    max_attempts=3,
                    base_delay_seconds=1.0,
                    # TimeoutError + transient httpx errors are
                    # retryable; the auth-error subclasses don't
                    # surface here because capture_tokens already
                    # absorbs them into ``CapturedTokens(expired=True)``.
                    retryable=lambda exc: isinstance(exc, TimeoutError | OSError),
                )
            except TimeoutError:
                log.warning("refresh.driver_timeout", attempt=attempt)
                continue
            last_captured = captured
            if captured.expired:
                log.info("refresh.definitive_expiry")
                # Route the EXPIRED signal through the token store so the
                # Notifier fires on the valid-to-expired transition. The
                # store's mark_expired is an atomic CAS, safe against
                # concurrent callers.
                await token_store.mark_expired(user_email, "teams")
                return RefreshResult(user_email, RefreshOutcome.EXPIRED, captured)
            if _capture_is_fresh(captured, existing=creds):
                break

        if last_captured is None or last_captured.expired:
            return RefreshResult(user_email, RefreshOutcome.NO_TOKENS, last_captured)

        if not last_captured.any_token:
            log.warning("refresh.no_tokens_captured")
            return RefreshResult(user_email, RefreshOutcome.NO_TOKENS, last_captured)

        if not _capture_is_fresh(last_captured, existing=creds):
            log.warning("refresh.capture_not_usable")
            return RefreshResult(user_email, RefreshOutcome.NO_TOKENS, last_captured)

        merged = _merge_creds(existing=creds, captured=last_captured)
        await token_store.update_credentials(user_email, "teams", merged)
        log.info(
            "refresh.success",
            has_graph_token=last_captured.graph_token is not None,
            has_csa_token=last_captured.csa_token is not None,
            has_refresh_token=last_captured.refresh_token is not None,
            cookie_count=len(last_captured.cookies),
        )
        return RefreshResult(user_email, RefreshOutcome.SUCCESS, last_captured)
    except Exception as exc:
        log.exception("refresh.errored")
        return RefreshResult(user_email, RefreshOutcome.ERROR, error=str(exc))


def _capture_is_fresh(captured: CapturedTokens, *, existing: dict[str, Any]) -> bool:
    """True when the capture justifies a write-back.

    Accepts:
    - A new graph_token whose expiry isn't earlier than the stored one.
    - A csa_token when none existed before.
    - A refresh_token that differs from the stored one (rolling forward).

    The refresher is responsible for guaranteeing the token shape (right
    appid, right resource); this function only enforces freshness against
    what's already persisted.
    """
    if captured.graph_token is not None:
        stored_graph = existing.get("graph_token")
        if not isinstance(stored_graph, str):
            return True
        new_exp = get_expiry_timestamp(captured.graph_token)
        old_exp = get_expiry_timestamp(stored_graph)
        # get_expiry_timestamp returns 0 on decode failure; treat that as
        # "accept the new token" since we can't compare.
        if new_exp == 0 or old_exp == 0:
            return True
        return new_exp >= old_exp
    if captured.csa_token is not None and not isinstance(existing.get("csa_token"), str):
        return True
    return captured.refresh_token is not None and existing.get("refresh_token") != (
        captured.refresh_token
    )


def _merge_creds(
    *,
    existing: dict[str, Any],
    captured: CapturedTokens,
) -> dict[str, Any]:
    """Overlay freshly-captured values onto the previous credential blob.

    Preserves ``team_ids``, ``refresh_url``, and any field the refresh didn't
    touch. Cookies merge by ``(domain, name)`` so a partial capture doesn't
    drop the SSO cookies we need next cycle; fresh values win on collision.
    """
    merged: dict[str, Any] = dict(existing)
    if captured.graph_token is not None:
        merged["graph_token"] = captured.graph_token
    if captured.csa_token is not None:
        merged["csa_token"] = captured.csa_token
    if captured.refresh_token is not None:
        merged["refresh_token"] = captured.refresh_token
    if captured.cookies:
        existing_cookies = existing.get("cookies", [])
        existing_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing_cookies, list):
            for c in existing_cookies:
                if not isinstance(c, dict):
                    continue
                key = (str(c.get("domain", "")), str(c.get("name", "")))
                existing_by_key[key] = c
        for c in captured.cookies:
            key = (str(c.get("domain", "")), str(c.get("name", "")))
            existing_by_key[key] = dict(c)
        merged["cookies"] = list(existing_by_key.values())
    return merged


# ---- Microsoft Teams Web Client identifiers --------------------------------
# Public values: these appear in every silent-renewal call MSAL.js makes from
# inside the Teams web client (probe M, 2026-04-25). Centralised here so the
# refresher and any future tooling reference one set of constants.

TEAMS_WEB_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
TEAMS_WEB_REDIRECT_URI = "https://teams.microsoft.com/v2/authv2"

# Cookie session on the Constructor U education tenant has freshness for the
# Skype audience but not Graph (interaction_required on direct silent OIDC).
# Bootstrapping a refresh_token via Skype then chaining to Graph via
# grant_type=refresh_token works in both cases. See
# docs/plans/2026-04-25-teams-token-refresh-strategy.md, "Verification".
_BOOTSTRAP_SCOPE: tuple[str, ...] = (
    "https://api.spaces.skype.com/.default",
    "openid",
    "profile",
    "offline_access",
)
_GRAPH_SCOPE: tuple[str, ...] = (
    "https://graph.microsoft.com/.default",
    "openid",
    "profile",
    "offline_access",
)
_CSA_SCOPE: tuple[str, ...] = (
    "https://chatsvcagg.teams.microsoft.com/.default",
    "openid",
    "profile",
    "offline_access",
)


class HttpxTokenRefresher:
    """TokenRefresher that uses Microsoft's OIDC silent flow over httpx.

    Replaces the Playwright-driven path. No browser, no JS, no DOM —
    one or two HTTPS round-trips instead of a 30 s headless Chromium
    session.

    Strategy (per
    docs/plans/2026-04-25-teams-token-refresh-strategy.md):

    1. **Steady state.** If a previously persisted ``refresh_token`` is
       passed in, exchange it for a fresh Graph access_token via
       ``grant_type=refresh_token``. One round-trip, ~250 ms. The
       grant rolls the refresh_token forward so the next cycle uses
       the new value.

    2. **Cookie bootstrap.** When no refresh_token exists or the
       refresh-token grant fails (token revoked, sliding-window
       expired), bootstrap by running silent OIDC against the Skype
       audience using the SSO cookies. The Skype call mints both an
       access_token (for Skype) and a refresh_token. Then we chain
       the refresh_token into a Graph exchange. Two round-trips.

    3. **Definitive expiry.** When even the Skype bootstrap returns
       ``interaction_required`` (cookie session lapsed), set
       ``expired=True`` so the caller marks the credential row.

    CSA tokens are minted on the same chain when ``include_csa=True``
    (default ``True`` to mirror the Playwright path's behaviour). The
    chained CSA exchange is best-effort: a failure does not invalidate
    the Graph capture, since the chat adapter can fall back to
    on-demand refresh of CSA tokens.
    """

    def __init__(self, *, include_csa: bool = True) -> None:
        self._log = get_logger(__name__)
        self._include_csa = include_csa

    async def capture_tokens(
        self,
        *,
        cookies: list[dict[str, Any]],
        refresh_url: str,
        timeout_seconds: int,
        refresh_token: str | None = None,
        user_email: str | None = None,
    ) -> CapturedTokens:
        # ``refresh_url`` belongs to the Playwright path's vocabulary
        # (a channel deeplink that forced the homepage to issue scoped
        # Graph calls). The OIDC flow doesn't navigate anywhere — keep
        # the parameter on the signature for protocol parity, ignore.
        del refresh_url

        graph_config = self._build_config(_GRAPH_SCOPE, user_email)
        bootstrap_config = self._build_config(_BOOTSTRAP_SCOPE, user_email)

        if refresh_token:
            try:
                bundle = await asyncio.to_thread(
                    refresh_with_token, refresh_token, graph_config, timeout=float(timeout_seconds)
                )
            except InteractionRequiredError as exc:
                self._log.info("refresh.refresh_token_invalid", error=str(exc))
                # Fall through to cookie bootstrap.
            except TokenExchangeError as exc:
                self._log.warning("refresh.refresh_token_exchange_error", error=str(exc))
                # Fall through.
            else:
                csa_token = await self._mint_csa(
                    bundle.refresh_token or refresh_token,
                    user_email=user_email,
                    timeout_seconds=timeout_seconds,
                )
                return CapturedTokens(
                    graph_token=bundle.access_token,
                    csa_token=csa_token,
                    refresh_token=bundle.refresh_token or refresh_token,
                    cookies=tuple(cookies),
                    expired=False,
                )

        try:
            bootstrap_bundle = await asyncio.to_thread(
                silent_token_refresh,
                cookies,
                bootstrap_config,
                timeout=float(timeout_seconds),
            )
        except InteractionRequiredError as exc:
            self._log.info("refresh.cookie_session_invalid", error=str(exc))
            return CapturedTokens(expired=True, cookies=tuple(cookies))
        except TokenExchangeError as exc:
            self._log.warning("refresh.bootstrap_failed", error=str(exc))
            return CapturedTokens(cookies=tuple(cookies))

        rolled_refresh = bootstrap_bundle.refresh_token
        if not rolled_refresh:
            self._log.warning("refresh.bootstrap_no_refresh_token")
            return CapturedTokens(cookies=tuple(cookies))

        try:
            graph_bundle = await asyncio.to_thread(
                refresh_with_token,
                rolled_refresh,
                graph_config,
                timeout=float(timeout_seconds),
            )
        except InteractionRequiredError as exc:
            self._log.info("refresh.graph_chain_interaction_required", error=str(exc))
            # Persist the refresh_token anyway so next cycle has it.
            return CapturedTokens(
                refresh_token=rolled_refresh,
                cookies=tuple(cookies),
            )
        except TokenExchangeError as exc:
            self._log.warning("refresh.graph_chain_failed", error=str(exc))
            return CapturedTokens(
                refresh_token=rolled_refresh,
                cookies=tuple(cookies),
            )

        csa_token = await self._mint_csa(
            graph_bundle.refresh_token or rolled_refresh,
            user_email=user_email,
            timeout_seconds=timeout_seconds,
        )
        return CapturedTokens(
            graph_token=graph_bundle.access_token,
            csa_token=csa_token,
            refresh_token=graph_bundle.refresh_token or rolled_refresh,
            cookies=tuple(cookies),
            expired=False,
        )

    def _build_config(self, scope: tuple[str, ...], user_email: str | None) -> SilentOIDCConfig:
        return SilentOIDCConfig(
            client_id=TEAMS_WEB_CLIENT_ID,
            redirect_uri=TEAMS_WEB_REDIRECT_URI,
            scope=list(scope),
            login_hint=user_email,
        )

    async def _mint_csa(
        self,
        refresh_token: str,
        *,
        user_email: str | None,
        timeout_seconds: int,
    ) -> str | None:
        """Best-effort CSA token mint via the same refresh_token.

        CSA failures don't invalidate the Graph capture; the chat adapter
        can refresh CSA on demand.
        """
        if not self._include_csa:
            return None
        csa_config = self._build_config(_CSA_SCOPE, user_email)
        try:
            bundle = await asyncio.to_thread(
                refresh_with_token,
                refresh_token,
                csa_config,
                timeout=float(timeout_seconds),
            )
        except (InteractionRequiredError, TokenExchangeError) as exc:
            self._log.info("refresh.csa_mint_skipped", error=str(exc))
            return None
        return bundle.access_token


# Slice 8: public aliases for the merge / freshness helpers so the
# polymorphic TeamsRefreshStrategy can reuse them without tripping
# pyright's reportPrivateUsage. The leading-underscore names stay as
# the canonical declarations so this module's existing call sites
# remain unchanged; cross-module callers import the public names.
capture_is_fresh = _capture_is_fresh
merge_creds = _merge_creds
