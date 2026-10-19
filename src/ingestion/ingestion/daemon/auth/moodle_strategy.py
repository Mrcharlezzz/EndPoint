"""Moodle adapter for the polymorphic :class:`RefreshStrategy` Protocol.

Moodle's standard authentication uses a long-lived web-service token
(``wstoken``) issued by an institution administrator and pasted into
the credential UI. There's no OAuth refresh flow we can chain to —
the token is valid until the operator rotates it on the Moodle side.

This strategy is **probe-not-rotate**: instead of minting a new token
the strategy calls Moodle's read-only ``core_webservice_get_site_info``
endpoint and inspects the response. The point isn't to refresh — it's
to give the daemon visibility into a token's revocation so the credential
can be flipped to ``is_expired`` without waiting for the next sync to
fail mid-fetch.

Outcomes:

* HTTP 200 + valid site_info → :data:`RefreshOutcome.SUCCESS` with
  ``new_payload=None`` (no write-back needed; the payload is still
  what it was).
* HTTP 200 + Moodle "200-with-error" body whose ``errorcode`` is in
  the token-error set (``invalidtoken``, ``accessexception``,
  ``invalidlogin``, ``sitemaintenance``) → :data:`RefreshOutcome.EXPIRED`.
* HTTP 401 / 403 → :data:`RefreshOutcome.EXPIRED`.
* HTTP 5xx, network blip, malformed JSON → :data:`RefreshOutcome.ERROR`.

The Moodle base URL is read from the credential payload (forward-compat
for institutions whose URL might drift) and falls back to the
:class:`MoodleConfig` default when not present in the payload.

Future work: the Moodle OAuth2 plugin (LMS-side configuration) would
let us mint refresh-tokens like the Teams flow. When that's in scope
the strategy upgrades to rotate-on-call without changing the
:class:`RefreshStrategy` contract — this is exactly the abstraction
slice 8 introduced.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from ingestion.daemon.auth.strategy import RefreshOutcome, RefreshResult
from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from ingestion.config import MoodleConfig

# Mirror the per-error-code set the adapter's REST client carries.
# Duplicated here (rather than imported from ``ingestion.adapters.moodle``)
# because the strategy module deliberately stays carve-out-clean — it
# must not import anything under ``adapters/``. The set is a Moodle
# protocol detail; if Moodle adds new "token dead" codes both copies
# need updating, and the test for the strategy will pin that.
_MOODLE_TOKEN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalidtoken",
        "accessexception",
        "invalidlogin",
        "sitemaintenance",
    }
)


class MoodleRefreshStrategy:
    """Probe-not-rotate strategy for Moodle webservice tokens.

    Constructor takes the runtime :class:`MoodleConfig` so the default
    base URL can be sourced from config when the credential payload
    doesn't carry an explicit ``base_url`` override. ``client_factory``
    is exposed for tests to inject a respx-mocked ``httpx.AsyncClient``
    without monkeypatching ``httpx`` globals.
    """

    def __init__(
        self,
        config: MoodleConfig,
        *,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds
        self._log = get_logger(__name__)

    async def is_refresh_supported(self) -> bool:
        # Probe-not-rotate still counts as "supported" — the bulk loop
        # surfaces EXPIRED via the same path it does for Teams. False
        # is reserved for platforms whose strategy is scaffolding only.
        return True

    async def refresh(
        self,
        email: str,
        *,
        current_payload: dict[str, Any],
    ) -> RefreshResult:
        """Probe Moodle's ``site_info`` endpoint and report token health.

        ``email`` isn't strictly needed for the probe (Moodle binds the
        identity to the token, not a header) but it's accepted for
        protocol parity and used in the structured-log context.
        """
        start = time.perf_counter()
        if not current_payload:
            return _result(RefreshOutcome.NO_TOKENS, start, "empty credential payload")

        token = current_payload.get("token")
        if not isinstance(token, str) or not token:
            return _result(RefreshOutcome.NO_TOKENS, start, "no webservice token")

        # Edge #11: forward-compat for credentials that carry their own
        # base_url (per-institution). Falls back to the runtime config
        # default when the payload doesn't override.
        base_url_raw = current_payload.get("base_url") or self._config.base_url
        base_url = base_url_raw if isinstance(base_url_raw, str) else self._config.base_url
        endpoint = f"{base_url.rstrip('/')}/webservice/rest/server.php"

        try:
            async with self._client_factory(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    endpoint,
                    data={
                        "wstoken": token,
                        "wsfunction": "core_webservice_get_site_info",
                        "moodlewsrestformat": "json",
                    },
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._log.warning(
                "moodle_strategy.network_error",
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

        # 401/403 directly: Moodle servers behind some proxies short-
        # circuit the JSON envelope and return a raw HTTP error.
        if response.status_code in (401, 403):
            self._log.info(
                "moodle_strategy.http_auth_failure",
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

        # Edge #4: Moodle's "200 with errorcode" pattern. Site-info on a
        # revoked token returns HTTP 200 with a JSON payload like
        # ``{"errorcode": "invalidtoken", "exception": "...", ...}``.
        if isinstance(data, dict):
            error_code = data.get("errorcode")
            if isinstance(error_code, str) and error_code:
                if error_code in _MOODLE_TOKEN_ERROR_CODES:
                    self._log.info(
                        "moodle_strategy.token_error",
                        user_email=email,
                        error_code=error_code,
                    )
                    return _result(
                        RefreshOutcome.EXPIRED,
                        start,
                        f"moodle errorcode: {error_code}",
                    )
                # Other Moodle error codes are platform problems, not
                # auth problems — surface as ERROR so the operator can
                # see the diagnostic without flipping is_expired.
                return _result(
                    RefreshOutcome.ERROR,
                    start,
                    f"moodle errorcode: {error_code}",
                )

        # Healthy site_info responses always include ``sitename`` and
        # ``userid`` — defensive check so a misrouted endpoint (e.g.
        # an HTML login page returning 200 with a JSON parser error)
        # doesn't silently count as success.
        if not isinstance(data, dict) or "userid" not in data:
            return _result(RefreshOutcome.ERROR, start, "site_info missing userid")

        return RefreshResult(
            outcome=RefreshOutcome.SUCCESS,
            new_payload=None,
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
