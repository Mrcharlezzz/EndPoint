"""Sync pipeline — orchestrates fetch, upsert, and logging for one user+platform.

This is the server-side entry point used by Django/Celery.
The CLI has its own orchestration in ``cli._run_impl``.

Slice 8: the pipeline now detects 401/403 mid-sync and raises a
typed :class:`AuthenticationFailedError` after marking the credential
expired and logging the failure. The detection covers every adapter
because the shared :class:`ingestion.clients.base.BaseHTTPClient`
already converts HTTP 401 to :class:`TokenExpiredError` and HTTP 403
to :class:`InsufficientScopeError` — the pipeline catches at that
seam without reaching into adapter code (carve-out: never edit
``ingestion/adapters/``).

The pipeline does NOT retry on 401: once the platform has rejected
the token, replaying the same sync would just storm the platform.
The next bulk-refresh cycle will attempt to refresh the credential;
if that also returns ``EXPIRED`` the credential.expired event fires
and the operator is notified.

Trade-off — no in-line refresh trigger on 401. The pipeline could
theoretically attempt one mid-sync silent refresh before raising
:class:`AuthenticationFailedError`, then re-fetch with the new
token. We deliberately do NOT do that because:

* The 401 is detected at the adapter seam, not at the auth seam —
  the pipeline would have to reach back into the strategy registry
  to know which platform's strategy to invoke. That couples the
  pipeline to the daemon's auth surface, which the CLI doesn't have.
* A failed in-line refresh would leave the credential in an
  ambiguous state during the same sync (was it the original token
  or the refreshed one that got rejected?). The bulk-refresh loop's
  next cycle gives the auth path a clean separate observation point.
* Latency-wise it doesn't help: by the time the adapter returns 401
  the user has already waited for the failed fetch. The next sync
  (after the bulk loop refreshes) is when the user actually sees
  recovered functionality.

If a future slice wants in-line refresh, the seam is here — catch
``TokenExpiredError``, call ``coordinator.refresh_credential``, and
on ``RefreshOutcome.SUCCESS`` re-issue ``adapter.fetch`` once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.daemon.logging import get_logger
from ingestion.daemon.reliability.backoff import with_backoff
from ingestion.daemon.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
)
from ingestion.exceptions import (
    IngestionError,
    InsufficientScopeError,
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)
from ingestion.schemas import (
    FetchResult,
    FetchScope,
    SyncStats,
)

if TYPE_CHECKING:
    from ingestion.ports import Adapter, ContentSink, SyncLogger, SyncStateStore, TokenStore

logger = logging.getLogger(__name__)
# structlog channel for per-sync telemetry events (slice 13). The legacy
# stdlib ``logger`` above stays for the human-readable info lines; the
# structured events below ride on structlog so they pick up the
# ``sync_id`` / ``user_email`` / ``platform`` contextvars bound by
# :func:`run_sync` and land in the ``logs`` SQLite table for the
# dashboard's per-sync drill-down.
_telemetry = get_logger(__name__)


# Module-level breaker for the fetch path. Keyed ``fetch:<platform>``
# so each adapter has its own isolated breaker — a Teams Graph outage
# won't trip the Moodle or Telegram breakers. State is process-local;
# a restart resets every key to CLOSED, which is acceptable because
# the dead-letter tracker also persists per (user, platform) failure
# streaks and provides a complementary backstop.
#
# TESTING HAZARD: this singleton retains state across tests in the
# same process. A test that drives the breaker to OPEN leaves it OPEN
# for every later test that calls ``run_sync`` until the state is
# explicitly reset. Use
# :func:`ingestion.daemon.reliability.circuit_breaker.reset_breaker_state_for_tests`
# between tests, or construct a fresh ``CircuitBreaker`` and inject it
# past the module-level default. See the circuit_breaker module
# docstring for the full pattern.
_FETCH_BREAKER = CircuitBreaker()


def get_fetch_breaker() -> CircuitBreaker:
    """Expose the module-level fetch breaker for tests and metrics.

    Tests that trip this breaker MUST reset its state via
    :func:`ingestion.daemon.reliability.circuit_breaker.reset_breaker_state_for_tests`
    or the OPEN state leaks into later tests in the same process.
    """
    return _FETCH_BREAKER


class _AuthHolder:
    """Carrier for an auth exception that escaped a fetch.

    Used so the breaker call sees a "successful" return when the
    adapter raised :class:`TokenExpiredError` or
    :class:`InsufficientScopeError` — those are credential failures,
    not upstream outages, and shouldn't count toward the breaker's
    trip threshold. The outer code re-raises the held exception.
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


def _is_retryable_fetch_error(exc: BaseException) -> bool:
    """Predicate for the fetch backoff helper.

    Retry only the upstream-transient subset — :class:`RateLimitError`
    (the adapter exhausted its own internal retries against a 429) and
    :class:`PlatformUnavailableError` (5xx after retries). Everything
    else is treated as terminal so generic bugs (RuntimeError,
    NormalizationError) surface immediately without burning the budget,
    and auth errors propagate directly to the auth-handling branch.
    """
    return isinstance(exc, RateLimitError | PlatformUnavailableError)


async def run_sync(
    adapter: Adapter,
    credentials: dict,
    content_sink: ContentSink,
    sync_state: SyncStateStore,
    sync_logger: SyncLogger,
    user_id: str,
    scope: frozenset[FetchScope] | None = None,
    token_store: TokenStore | None = None,
) -> tuple[SyncStats, str]:
    """Full sync cycle for one user + one platform.

    1. Log sync start
    2. Load cursor from sync_state
    3. Call adapter.fetch(credentials, cursor, scope)
    4. For each normalized item: content_sink.upsert()
    5. Save new cursor to sync_state (only on full completion)
    6. Log result to sync_logger
    7. Return (stats, sync_id)

    The pipeline does NOT:
    - Schedule itself (Django/Celery does that)
    - Download files (returns FileRef URLs)
    - Run LLM extraction (Telegram items stay as raw_message)
    - Handle token refresh (raises, caller handles)

    Returns a ``(stats, sync_id)`` tuple. Callers that want to correlate a
    run with its sync_history row (e.g. the scheduler's failure detection)
    should use the ``sync_id`` to query by primary key rather than racing
    against ``ORDER BY started_at DESC``, which can pick up a stale row
    under clock skew or overlapping retries.
    """
    platform = adapter.platform
    sync_id = await sync_logger.log_start(user_id, platform)
    start_time = time.monotonic()

    # Bind sync_id so every structlog record emitted inside the pipeline
    # lands in the logs table with the sync_id column populated. The UI
    # "logs for this sync" drill-down depends on this.
    from ingestion.daemon.logging import bind_sync_context
    from ingestion.daemon.observability.tracing import traced_span

    with bind_sync_context(sync_id=sync_id, user_email=user_id, platform=platform):
        # Slice 13: structured ``sync.started`` event so dashboards and
        # alert rules can pivot on (sync_id, user_email, platform, scope,
        # sync_kind, started_at). The contextvars bound above auto-merge
        # so the explicit kwargs below are for fields that aren't already
        # in the context (scope/started_at/sync_kind).
        scope_repr = sorted(s.value for s in scope) if scope is not None else None
        _telemetry.info(
            "sync.started",
            started_at=datetime.now(tz=UTC).isoformat(),
            scope=scope_repr,
            sync_kind="content_sync",
        )
        # Slice 13: optional OpenTelemetry span. No-op when the SDK is
        # not installed; otherwise the span carries the sync_id /
        # user_email / platform attributes for trace correlation in
        # tools like Jaeger / Honeycomb / Tempo.
        with traced_span(
            "sync.run",
            sync_id=sync_id,
            user_email=user_id,
            platform=platform,
        ):
            stats = await _run_sync_inner(
                adapter=adapter,
                credentials=credentials,
                content_sink=content_sink,
                sync_state=sync_state,
                sync_logger=sync_logger,
                user_id=user_id,
                scope=scope,
                token_store=token_store,
                sync_id=sync_id,
                platform=platform,
                start_time=start_time,
            )
    return stats, sync_id


async def _run_sync_inner(
    *,
    adapter: Adapter,
    credentials: dict,
    content_sink: ContentSink,
    sync_state: SyncStateStore,
    sync_logger: SyncLogger,
    user_id: str,
    scope: frozenset[FetchScope] | None,
    token_store: TokenStore | None,
    sync_id: str,
    platform: str,
    start_time: float,
) -> SyncStats:
    try:
        cursor = await sync_state.get_cursor(user_id, platform, "default")

        logger.info("Starting sync for user=%s platform=%s", user_id, platform)
        # Per-platform fetch breaker. Counts only adapter-internal
        # transient errors as failures: 401/403 (TokenExpiredError /
        # InsufficientScopeError) are credential issues, not upstream
        # outages, so we wrap them in a holder that returns from the
        # breaker as a "success" then re-raise outside. The breaker
        # therefore measures upstream availability, not credential
        # validity. CircuitOpenError surfaces here as a typed return.
        #
        # The retry loop sits *inside* the breaker callable so the
        # breaker observes one outcome per fetch attempt as a whole
        # (3 retries that all fail count as one breaker failure;
        # retry that succeeds on attempt 2 counts as one breaker
        # success). 2s base delay matches the audit's recommended
        # value for the more-expensive pipeline path.
        fetch_breaker_key = f"fetch:{platform}"

        async def _fetch_once() -> FetchResult:
            return await adapter.fetch(credentials, cursor, scope)

        async def _do_fetch() -> FetchResult | _AuthHolder:
            try:
                return await with_backoff(
                    _fetch_once,
                    max_attempts=3,
                    base_delay_seconds=2.0,
                    retryable=_is_retryable_fetch_error,
                )
            except (TokenExpiredError, InsufficientScopeError) as auth_exc:
                # Bypass the breaker counter: pass the auth exception
                # through as a "successful observation" so the outer
                # block can re-raise it without tripping the breaker.
                return _AuthHolder(auth_exc)

        try:
            fetch_result = await _FETCH_BREAKER.call(fetch_breaker_key, _do_fetch)
        except CircuitOpenError as exc:
            duration = time.monotonic() - start_time
            logger.warning(
                "Fetch circuit breaker open for user=%s platform=%s next_attempt_at=%s",
                user_id,
                platform,
                exc.next_attempt_at,
            )
            await sync_logger.log_failure(sync_id, f"circuit_open: {exc}")
            # Slice 13: terminal ``sync.finished`` for the breaker-open
            # path. Total items zero because the fetch never ran.
            _telemetry.info(
                "sync.finished",
                total_items_fetched=0,
                total_items_ingested=0,
                duration_seconds=round(duration, 3),
                outcome="failed",
                failure_reason="circuit_open",
            )
            return SyncStats(errors=1, duration_seconds=duration)
        if isinstance(fetch_result, _AuthHolder):
            # Propagate the auth error to the existing handler below.
            raise fetch_result.exc
        result: FetchResult = fetch_result

        # Slice 13: emit one ``sync.scope_progress`` event per scope so a
        # sync that fetched 3 scopes ends up with 3 rows in the logs table
        # tagged by scope. ``per_scope_stats`` is keyed by the adapter's
        # scope label (string) and counts items_fetched at the scope
        # boundary. We log only scopes the adapter actually exercised
        # (empty dict means the adapter fetched everything in one call,
        # documented in the schemas module).
        for scope_name, scope_stats in result.stats.per_scope_stats.items():
            _telemetry.info(
                "sync.scope_progress",
                scope=scope_name,
                items_fetched_in_scope=scope_stats.items_fetched,
            )

        # Attribute every item to the credential that fetched it. Powers the
        # dashboard's per-user Contributions view.
        for item in result.items:
            item.fetched_by_user_email = user_id

        # Batch dedup check: one round-trip on the full id list instead of
        # the historical N round-trips. Every conformant ContentSink now
        # implements ``exists_batch`` per the Protocol, so we call the method
        # directly. Items in a fetch share one platform but we don't assume
        # that — group by platform defensively so a future multi-platform
        # fetcher still gets the right "exists" answer.
        existing_ids: set[str] = set()
        if result.items:
            ids_by_platform: dict[str, list[str]] = {}
            for item in result.items:
                ids_by_platform.setdefault(item.platform, []).append(item.external_id)
            for plat, plat_ids in ids_by_platform.items():
                found = await content_sink.exists_batch(plat, plat_ids)
                existing_ids.update(found)

        # Slice 13: ``sync.upserting`` straddles the upsert boundary — emit
        # BEFORE so a long upsert (large batch + slow Postgres) is visible
        # to the dashboard with items_to_upsert > 0 even if the call hangs.
        # An empty list is allowed and the matching ``sync.upserted`` event
        # still fires below so dashboards can pair the two events 1:1.
        upsert_started = time.monotonic()
        _telemetry.info(
            "sync.upserting",
            items_to_upsert=len(result.items),
        )
        upserted_ids = await content_sink.upsert_batch(result.items)
        items_updated = sum(1 for item in result.items if item.external_id in existing_ids)
        items_new = len(result.items) - items_updated
        upsert_duration = time.monotonic() - upsert_started
        _telemetry.info(
            "sync.upserted",
            items_inserted=items_new,
            items_skipped=items_updated,
            duration_seconds=round(upsert_duration, 3),
        )

        for item_id in upserted_ids:
            logger.debug("Upserted item %s", item_id)

        if result.cursor is not None:
            # Shield the cursor write from cancellation: once we've fetched
            # and upserted the items, we MUST land the cursor or the next
            # run will refetch the same range. Cancellation (e.g. from
            # emergency_stop) can land mid-set_cursor; shield lets the
            # sqlite commit finish before the cancel is re-raised upstream.
            await asyncio.shield(sync_state.set_cursor(user_id, platform, "default", result.cursor))
            logger.info("Cursor saved for user=%s platform=%s", user_id, platform)

        duration = time.monotonic() - start_time

        stats = SyncStats(
            items_fetched=len(result.items),
            items_new=items_new,
            items_updated=items_updated,
            items_skipped=result.stats.items_skipped,
            errors=len(result.errors),
            duration_seconds=duration,
            api_call_duration_seconds=result.stats.api_call_duration_seconds,
            normalization_duration_seconds=result.stats.normalization_duration_seconds,
            per_scope_stats=result.stats.per_scope_stats,
        )

        await sync_logger.log_complete(sync_id, stats)

        if result.errors:
            for error in result.errors:
                logger.warning(
                    "Fetch error: code=%s scope=%s message=%s recoverable=%s",
                    error.code,
                    error.scope,
                    error.message,
                    error.recoverable,
                )

        logger.info(
            "Sync complete for user=%s platform=%s: "
            "fetched=%d new=%d updated=%d errors=%d duration=%.1fs",
            user_id,
            platform,
            stats.items_fetched,
            stats.items_new,
            stats.items_updated,
            stats.errors,
            stats.duration_seconds,
        )
        # Slice 13: ``sync.finished`` is the terminal event for a happy
        # path. ``outcome="success"`` even when the FetchResult carried
        # per-scope errors — a partial success is not a failed sync (the
        # scheduler's dead-letter logic agrees: it only escalates when
        # the sync_history row state == 'failed'). The failure-path
        # branches below emit their own ``sync.finished`` with
        # ``outcome="failed"`` so dashboards can pair the two reliably.
        _telemetry.info(
            "sync.finished",
            total_items_fetched=stats.items_fetched,
            total_items_ingested=stats.items_new + stats.items_updated,
            duration_seconds=round(stats.duration_seconds, 3),
            outcome="success",
        )

        return stats

    except (TokenExpiredError, InsufficientScopeError) as exc:
        # 401/403 mid-sync: mark the credential expired, log the
        # failure, and raise a typed AuthenticationFailedError so the
        # scheduler's worker pool sees a structured exception (not a
        # generic RuntimeError on "state == failed"). Atomic CAS in
        # the token store guards against two concurrent fetches both
        # racing to mark expired — only the false-to-true transition
        # fires the notifier, so a re-mark is idempotent.
        http_status = 401 if isinstance(exc, TokenExpiredError) else 403
        logger.error(
            "Authentication failed for user=%s platform=%s status=%d",
            user_id,
            platform,
            http_status,
        )
        if token_store is not None:
            await token_store.mark_expired(user_id, platform)
        await sync_logger.log_failure(sync_id, f"Authentication failed (http {http_status})")
        # Slice 13: terminal ``sync.finished`` for the auth-failure path.
        duration = time.monotonic() - start_time
        _telemetry.info(
            "sync.finished",
            total_items_fetched=0,
            total_items_ingested=0,
            duration_seconds=round(duration, 3),
            outcome="failed",
            failure_reason=f"auth_{http_status}",
        )
        # Lazy import: services.exceptions pulls in the daemon-side
        # service tree which the CLI doesn't need at runtime.
        from ingestion.daemon.services.exceptions import (  # noqa: PLC0415
            AuthenticationFailedError,
        )

        raise AuthenticationFailedError(
            platform=platform,
            user_email=user_id,
            http_status=http_status,
            cause=str(exc)[:200],
        ) from exc

    except IngestionError as exc:
        duration = time.monotonic() - start_time
        logger.error("Sync failed for user=%s platform=%s: %s", user_id, platform, exc)
        await sync_logger.log_failure(sync_id, str(exc))
        _telemetry.info(
            "sync.finished",
            total_items_fetched=0,
            total_items_ingested=0,
            duration_seconds=round(duration, 3),
            outcome="failed",
            failure_reason=type(exc).__name__,
        )
        return SyncStats(errors=1, duration_seconds=duration)

    except Exception as exc:
        duration = time.monotonic() - start_time
        logger.exception("Unexpected error in sync for user=%s platform=%s", user_id, platform)
        await sync_logger.log_failure(sync_id, f"Unexpected: {exc}")
        _telemetry.info(
            "sync.finished",
            total_items_fetched=0,
            total_items_ingested=0,
            duration_seconds=round(duration, 3),
            outcome="failed",
            failure_reason="unexpected",
        )
        return SyncStats(errors=1, duration_seconds=duration)
