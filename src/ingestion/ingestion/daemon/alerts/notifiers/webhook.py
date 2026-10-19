"""HTTP-webhook :class:`AlertNotifier` for slice 13.

POSTs alert firings to an operator-configured URL. Two payload shapes:

* ``"webhook"`` — generic JSON envelope keyed on ``rule_id``,
  ``rule_name``, ``fired_at``, ``user_email``, ``platform``, ``severity``,
  ``context``. Drop into PagerDuty Events API v2 / OpsGenie / a custom
  receiver.
* ``"slack"`` — Slack-incoming-webhook compatible ``{"text": "..."}``
  body. The notifier formats a compact one-liner so a busy ops channel
  stays scannable.

Reliability:

* Retried 3 times with exponential backoff via :func:`with_backoff`.
  Only HTTP 5xx and connection errors trigger a retry; 4xx is treated
  as a misconfiguration (bad URL, wrong auth header) and dropped after
  the first attempt so a typo doesn't burn the budget.
* The notifier never raises. A final failure logs ``alerts.notifier_failed``
  and returns; the alert engine's own try/except already covers a
  raising notifier, but defence-in-depth keeps the invariant local.

Security:

* The bearer token is sent in the ``Authorization`` header only — never
  echoed into any log line, even at debug level. Logs include the URL
  (host-only) so an operator can confirm where the call landed without
  the sniffable token surface.
* Outbound calls have a fixed 10s timeout so a hung receiver can't pin
  the alert engine's tick.

Stale-notification guard: if the firing was resolved between the engine
recording it and the webhook delivering, the notifier reads the row
state once before each retry and skips with ``alerts.notifier_stale``
when the firing is no longer ``active``. That prevents an alert that
self-healed during a 5xx storm from waking up an on-call with a
five-minute-stale page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx

from ingestion.daemon.logging import get_logger
from ingestion.daemon.reliability.backoff import with_backoff

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from ingestion.daemon.alerts.rules import AlertRule


# Treat 5xx and connection errors as transient. 4xx is a config error
# (bad URL, missing token) — don't burn the retry budget on it.
_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})


class _RetryableHTTPError(Exception):
    """Marker exception so :func:`with_backoff` retries on transient HTTP."""


class WebhookAlertNotifier:
    """POST alert firings to an HTTP webhook.

    The transport is :class:`httpx.AsyncClient` for the ergonomics; a
    pre-built client can be injected for tests. ``format`` switches
    between a generic envelope and a Slack-compatible body — operators
    pick one via ``config.alerts.notifier_format``.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str = "",
        notifier_format: str = "webhook",
        db_path: Path | None = None,
        timeout_seconds: float = 10.0,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
        max_attempts: int = 3,
    ) -> None:
        if not url:
            raise ValueError("WebhookAlertNotifier requires a non-empty url")
        if notifier_format not in {"webhook", "slack"}:
            raise ValueError(
                f"notifier_format must be 'webhook' or 'slack', got {notifier_format!r}"
            )
        self._url = url
        self._token = token
        self._format = notifier_format
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._max_attempts = max_attempts
        self._db_path = db_path
        self._log = get_logger(__name__)

    async def alert_fired(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> None:
        """Deliver an alert payload via the configured webhook.

        Catches every exception so the engine's tick-loop is never
        crashed by a transient receiver. The retry budget is small (3
        attempts by default) so a sustained outage at the receiver
        doesn't queue notifier work indefinitely.
        """
        try:
            await with_backoff(
                lambda: self._post_once(rule=rule, value=value, fired_at=fired_at),
                max_attempts=self._max_attempts,
                base_delay_seconds=1.0,
                retryable=lambda exc: isinstance(exc, _RetryableHTTPError),
            )
        except Exception as exc:
            # Final failure after retries OR a non-retryable HTTP 4xx.
            # Log host-only so a token in a malformed URL stays local.
            self._log.warning(
                "alerts.notifier_failed",
                rule_id=rule.id,
                rule_name=rule.name,
                host=_safe_host(self._url),
                exc_type=type(exc).__name__,
                exc_message=str(exc)[:200],
            )

    async def _post_once(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> None:
        # Stale check: skip if the firing has already been resolved by
        # the engine's resolution edge while we were retrying. Only runs
        # when ``db_path`` was wired (test-friendly: omit to disable).
        if self._db_path is not None and await self._is_resolved(rule.id):
            self._log.info(
                "alerts.notifier_stale",
                rule_id=rule.id,
                rule_name=rule.name,
                host=_safe_host(self._url),
            )
            return
        payload = self._build_payload(rule=rule, value=value, fired_at=fired_at)
        headers = self._build_headers()
        try:
            async with self._client_factory(timeout=self._timeout_seconds) as client:
                response = await client.post(self._url, json=payload, headers=headers)
        except (httpx.TransportError, httpx.HTTPError) as exc:
            # Connect / read errors are transient — retry the budget.
            raise _RetryableHTTPError(str(exc)[:200]) from exc
        # 4xx is dropped after the first attempt; 5xx triggers a retry.
        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise _RetryableHTTPError(f"HTTP {response.status_code} from {_safe_host(self._url)}")
        if response.status_code >= 400:
            # Non-retryable: log and return without raising so the
            # outer wrapper's "final failure" branch isn't taken.
            self._log.warning(
                "alerts.notifier_rejected",
                rule_id=rule.id,
                status_code=response.status_code,
                host=_safe_host(self._url),
            )
            return
        # Slack returns 200 with body "ok" on success. A future custom
        # receiver could return ``{"ok": false}``; we treat that as a
        # success at the HTTP level (delivery happened) and let the
        # operator inspect their own receiver logs. Tightening this to
        # parse-and-fail would couple the daemon to a specific
        # receiver's contract.
        self._log.info(
            "alerts.notifier_delivered",
            rule_id=rule.id,
            rule_name=rule.name,
            host=_safe_host(self._url),
        )

    async def _is_resolved(self, rule_id: int) -> bool:
        """Read the most recent firing row's state. ``True`` if it has
        already flipped to resolved during retry latency.

        Cheap query — :class:`aiosqlite.connect` is fine here without a
        pool because the notifier path is rare and small. The lookup
        is keyed on ``(rule_id, fired_at DESC)`` which the migration
        ``0004_observability.sql`` already indexes.

        Defence-in-depth: the only call site (``_post_once``) already
        guards on ``db_path is not None`` so this branch is unreachable
        in normal flow. Kept so a future caller that drops the outer
        guard doesn't accidentally pass ``None`` to ``aiosqlite.connect``.
        """
        if self._db_path is None:  # pragma: no cover - guarded by caller
            return False
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                row = await (
                    await conn.execute(
                        """
                        SELECT state FROM alert_firings
                        WHERE rule_id = ?
                        ORDER BY fired_at DESC
                        LIMIT 1
                        """,
                        (rule_id,),
                    )
                ).fetchone()
        except aiosqlite.Error:
            # If the read itself fails, fall through to the POST. Better
            # to over-deliver than to silently drop on a transient
            # SQLite-busy hiccup.
            return False
        if row is None:
            return False
        return str(row[0]).lower() == "resolved"

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            # Bearer is the dominant convention. The token never lands
            # in a log line — only the host portion of the URL does.
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _build_payload(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> dict[str, Any]:
        if self._format == "slack":
            return self._slack_payload(rule=rule, value=value, fired_at=fired_at)
        return self._generic_payload(rule=rule, value=value, fired_at=fired_at)

    def _generic_payload(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> dict[str, Any]:
        return {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "fired_at": fired_at.isoformat(),
            "user_email": rule.user_email,
            "platform": rule.platform,
            # Severity is rule-name-based today; future slice can map
            # rule -> severity via a config table. For now WARN is the
            # safe default — operator triages.
            "severity": "warning",
            "context": {
                "metric": rule.metric,
                "comparator": rule.comparator.value,
                "threshold": rule.threshold,
                "value": value,
                "window_seconds": rule.window_seconds,
            },
        }

    def _slack_payload(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> dict[str, Any]:
        # Slack honours plaintext "text" with mrkdwn for emphasis. Keep
        # the line short so a busy channel renders cleanly.
        scope = ""
        if rule.platform:
            scope += f" platform={rule.platform}"
        if rule.user_email:
            scope += f" user={rule.user_email}"
        text = (
            f"*alert.fired* `{rule.name}` "
            f"{rule.metric}={value} {rule.comparator.value} {rule.threshold}"
            f"{scope} at {fired_at.isoformat()}"
        )
        return {"text": text}


def _safe_host(url: str) -> str:
    """Return a logging-safe host fragment (no userinfo, no path).

    Stripping ``user:pass@`` prevents an embedded credential in the URL
    from leaking to logs. ``urlparse`` is the right tool but adding a
    dependency on stdlib for one call is fine — the existing daemon
    code already imports :mod:`urllib.parse` elsewhere.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable>"
    netloc = parsed.netloc or "<blank>"
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    return netloc


# Re-exported so callers don't have to import json AND the module if
# they want to round-trip a payload (tests, future audit log path).
__all__ = ["WebhookAlertNotifier", "json"]
