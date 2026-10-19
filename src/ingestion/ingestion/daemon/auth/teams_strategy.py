"""Teams adapter for the polymorphic :class:`RefreshStrategy` Protocol.

Migrates the existing :class:`HttpxTokenRefresher` (silent OIDC over
httpx, see ``docs/plans/2026-04-25-teams-token-refresh-strategy.md``)
behind the strategy Protocol so the bulk-refresh loop and platform
registry can dispatch polymorphically.

This module is a *thin* adapter — the actual refresh logic stays in
:mod:`ingestion.daemon.auth.refresh`. No behaviour change vs the
pre-strategy code path: the same SSO-cookie filter, the same OIDC
exchange, the same fresh-token persistence. The strategy hands back a
:class:`RefreshResult` whose ``new_payload`` is the merged credential
blob the bulk loop should persist, and the bulk loop is responsible
for the actual write.

Why thin: the Teams flow already runs through
:func:`ingestion.daemon.auth.refresh._refresh_one_inner` which writes
to the token store inline. The strategy reuses the existing
:class:`HttpxTokenRefresher` to capture tokens but then returns the
new payload to its caller rather than persisting it itself —
delegating persistence keeps the strategy contract uniform across
rotate-on-call (Teams) and probe-not-rotate (Moodle, Telegram) flows.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ingestion.daemon.auth.refresh import (
    HttpxTokenRefresher,
    RefreshOutcome,
    capture_is_fresh,
    is_sso_cookie,
    merge_creds,
)
from ingestion.daemon.auth.strategy import RefreshResult
from ingestion.daemon.logging import bind_sync_context, get_logger

if TYPE_CHECKING:
    from ingestion.daemon.auth.refresh import TokenRefresher

_TEAMS_HOME_URL = "https://teams.microsoft.com"


class TeamsRefreshStrategy:
    """Wrap :class:`HttpxTokenRefresher` behind :class:`RefreshStrategy`.

    Constructor takes an optional refresher so tests can inject a fake
    instead of letting the strategy default-construct one. In production
    the daemon composes a single :class:`HttpxTokenRefresher` and the
    strategy reuses it across users — the refresher itself is stateless.
    """

    def __init__(
        self,
        *,
        refresher: TokenRefresher | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self._refresher = refresher or HttpxTokenRefresher()
        self._timeout_seconds = timeout_seconds
        self._log = get_logger(__name__)

    async def is_refresh_supported(self) -> bool:
        return True

    async def refresh(
        self,
        email: str,
        *,
        current_payload: dict[str, Any],
    ) -> RefreshResult:
        """Run one silent OIDC refresh and return the rotated payload.

        Mirrors :func:`ingestion.daemon.auth.refresh._refresh_one_inner`
        but returns the result instead of writing to the token store —
        the bulk-refresh loop owns the write. Outcomes:

        * :data:`RefreshOutcome.SUCCESS` — fresh tokens, payload
          merged with the previous blob (cookies de-duped, refresh
          token rolled forward).
        * :data:`RefreshOutcome.NO_TOKENS` — the credential row exists
          but has no SSO cookies / refresh_token to bootstrap from, OR
          the OIDC flow returned without a usable graph_token.
        * :data:`RefreshOutcome.EXPIRED` — the OIDC flow confirmed the
          cookie session is dead. Caller flips ``is_expired``.
        * :data:`RefreshOutcome.ERROR` — driver raised. Wrapped via
          :func:`ingestion.daemon.auth.strategy.wrap_unexpected` at
          the call site.

        ``current_payload == {}`` short-circuits to NO_TOKENS without
        any network I/O. This matches the freshly-provisioned-row
        case where the operator hasn't pasted cookies yet.
        """
        start = time.perf_counter()
        with bind_sync_context(user_email=email, platform="teams"):
            return await self._refresh_inner(email, current_payload, start)

    async def _refresh_inner(
        self,
        email: str,
        current_payload: dict[str, Any],
        start: float,
    ) -> RefreshResult:
        if not current_payload:
            return _result(RefreshOutcome.NO_TOKENS, start, "empty credential payload")

        cookies_raw = current_payload.get("cookies", [])
        cookies = cookies_raw if isinstance(cookies_raw, list) else []
        if not cookies:
            return _result(RefreshOutcome.NO_TOKENS, start, "no session cookies")

        sso_cookies = [c for c in cookies if isinstance(c, dict) and is_sso_cookie(c)]
        if not sso_cookies:
            return _result(RefreshOutcome.NO_TOKENS, start, "no sso cookies")

        refresh_url_raw = current_payload.get("refresh_url") or _TEAMS_HOME_URL
        refresh_url = refresh_url_raw if isinstance(refresh_url_raw, str) else _TEAMS_HOME_URL
        stored_refresh_token = current_payload.get("refresh_token")
        if not isinstance(stored_refresh_token, str) or not stored_refresh_token:
            stored_refresh_token = None

        try:
            captured = await asyncio.wait_for(
                self._refresher.capture_tokens(
                    cookies=sso_cookies,
                    refresh_url=refresh_url,
                    timeout_seconds=self._timeout_seconds,
                    refresh_token=stored_refresh_token,
                    user_email=email,
                ),
                # Same headroom the bulk path uses — wait_for + capture's
                # internal timeout cooperate, the slack covers queue waits.
                timeout=self._timeout_seconds + 10,
            )
        except TimeoutError:
            self._log.warning("teams_strategy.driver_timeout")
            return _result(RefreshOutcome.NO_TOKENS, start, "driver timeout")

        if captured.expired:
            return _result(RefreshOutcome.EXPIRED, start, "cookie session lapsed")

        if not captured.any_token or not capture_is_fresh(captured, existing=current_payload):
            return _result(RefreshOutcome.NO_TOKENS, start, "no usable tokens captured")

        merged = merge_creds(existing=current_payload, captured=captured)
        return RefreshResult(
            outcome=RefreshOutcome.SUCCESS,
            new_payload=merged,
            error_message=None,
            latency_ms=_elapsed_ms(start),
        )


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _result(
    outcome: RefreshOutcome,
    start: float,
    error_message: str | None,
) -> RefreshResult:
    return RefreshResult(
        outcome=outcome,
        new_payload=None,
        error_message=error_message,
        latency_ms=_elapsed_ms(start),
    )
