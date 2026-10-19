"""Telegram adapter for the polymorphic :class:`RefreshStrategy` Protocol.

Telegram bot tokens are long-lived but revocable: an admin can disable
or rotate a bot's token via @BotFather at any time. Like the Moodle
flow, this is **probe-not-rotate** — the strategy doesn't mint a new
token, it just probes the current one against ``getMe`` (the cheapest
authenticated read on the Bot API).

Outcomes:

* ``200`` with ``{"ok": true, "result": {...}}`` → :data:`RefreshOutcome.SUCCESS`
  (no payload change).
* ``401`` (or ``200`` with ``{"ok": false, "error_code": 401}``) →
  :data:`RefreshOutcome.EXPIRED`. Telegram returns 401 when a bot
  token is revoked, and the bot-API envelope encodes the same code
  in the JSON body (edge #5 — must detect both surfaces).
* Network error / 5xx → :data:`RefreshOutcome.ERROR`.

The strategy reads ``bot_token`` from the credential payload. Edge #12:
the encrypted-at-rest token is decrypted by :class:`PostgresTokenStore`
before the strategy ever sees it (the token store is the cipher
boundary), so the strategy never touches :class:`CredentialCipher`
directly. The ``api_base_url`` is read from the payload first (for
self-hosted Telegram-API instances) and falls back to the public
:class:`TelegramConfig` default.

Bot tokens are scoped to a single bot — there is no per-user OAuth
flow. The strategy still keys on ``email`` so the bulk loop's
per-user lock and structured-log context line up with every other
platform.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from ingestion.daemon.auth.strategy import RefreshOutcome, RefreshResult
from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from ingestion.config import TelegramConfig


class TelegramRefreshStrategy:
    """Probe-not-rotate strategy for Telegram bot tokens.

    Constructor takes the runtime :class:`TelegramConfig` so the
    default API base URL can be sourced from config when the
    credential payload doesn't carry an explicit ``api_base_url``
    override. ``client_factory`` exists for tests to inject a
    respx-mocked ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        config: TelegramConfig,
        *,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
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
        """Probe Telegram's ``getMe`` and report bot-token health."""
        start = time.perf_counter()
        if not current_payload:
            return _result(RefreshOutcome.NO_TOKENS, start, "empty credential payload")

        bot_token = current_payload.get("bot_token")
        if not isinstance(bot_token, str) or not bot_token:
            return _result(RefreshOutcome.NO_TOKENS, start, "no bot token")

        api_base_raw = current_payload.get("api_base_url") or self._config.api_base_url
        api_base = api_base_raw if isinstance(api_base_raw, str) else self._config.api_base_url
        endpoint = f"{api_base.rstrip('/')}/bot{bot_token}/getMe"

        try:
            async with self._client_factory(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=True,
            ) as client:
                response = await client.get(endpoint)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._log.warning(
                "telegram_strategy.network_error",
                user_email=email,
                error=str(exc),
            )
            return _result(
                RefreshOutcome.ERROR,
                start,
                f"network: {type(exc).__name__}",
            )
        except httpx.HTTPError as exc:
            return _result(RefreshOutcome.ERROR, start, f"httpx: {type(exc).__name__}")

        # Telegram returns 401 on revoked tokens directly. Some proxies
        # short-circuit before the JSON envelope so handle the raw HTTP
        # status first.
        if response.status_code in (401, 403):
            self._log.info(
                "telegram_strategy.http_auth_failure",
                user_email=email,
                status=response.status_code,
            )
            return _result(
                RefreshOutcome.EXPIRED,
                start,
                f"http {response.status_code}",
            )

        if response.status_code != 200:
            return _result(
                RefreshOutcome.ERROR,
                start,
                f"http {response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as exc:
            return _result(RefreshOutcome.ERROR, start, f"invalid json: {exc}")

        if not isinstance(data, dict):
            return _result(RefreshOutcome.ERROR, start, "non-dict response body")

        # Edge #5: Telegram's envelope encodes auth failures as
        # ``{"ok": false, "error_code": 401, "description": "..."}``
        # alongside the raw HTTP status. Treat error_code 401/403 as
        # EXPIRED (matching the http path above) and any other ok=false
        # as a platform-level error.
        if data.get("ok") is True:
            return RefreshResult(
                outcome=RefreshOutcome.SUCCESS,
                new_payload=None,
                error_message=None,
                latency_ms=_elapsed_ms(start),
            )

        error_code_raw = data.get("error_code")
        description_raw = data.get("description", "")
        description = description_raw if isinstance(description_raw, str) else ""
        if isinstance(error_code_raw, int) and error_code_raw in (401, 403):
            self._log.info(
                "telegram_strategy.envelope_auth_failure",
                user_email=email,
                error_code=error_code_raw,
                description=description[:100],
            )
            return _result(
                RefreshOutcome.EXPIRED,
                start,
                f"telegram error_code: {error_code_raw}",
            )

        return _result(
            RefreshOutcome.ERROR,
            start,
            f"ok=false: {description[:100]}" if description else "ok=false",
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
