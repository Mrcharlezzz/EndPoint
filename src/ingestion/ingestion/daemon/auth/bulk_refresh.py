"""Scheduled bulk-refresh cycle for Teams tokens.

Every ``interval_seconds`` the cycle:
    1. Enumerates non-expired Teams users.
    2. Distributes their refresh starts uniformly across the interval so N
       concurrent refreshes don't all fire at the same second.
    3. Runs each refresh via :func:`refresh_one`, bounded globally by a
       semaphore (``concurrency``) and serialised per-user via a shared
       lock dict so a force-refresh action can't race the cycle.

Per-user failures are logged but don't stop the cycle. A definitive auth
rejection (``RefreshOutcome.EXPIRED``) causes :func:`refresh_one` to mark
the credential row expired via the token store. Retries on transient
NO_TOKENS are bounded by ``max_retries_per_user``.

When a :class:`SyncLogger` is supplied the cycle also emits one
``sync_history`` row per user with ``sync_kind='credential_refresh'`` so the
V6 Credentials page's "Refresh activity" tab can reuse the V2 Syncs drawer
against the same table. ``SUCCESS`` closes the row via :meth:`log_complete`
with a minimal ``SyncStats``; every other outcome closes via
:meth:`log_failure` with the outcome code (plus the driver's error string
on ``ERROR``).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingestion.daemon.auth.coordinator import (
    CredentialRefreshOutcome,
    refresh_credential,
)
from ingestion.daemon.auth.refresh import RefreshOutcome, refresh_one
from ingestion.daemon.logging import get_logger
from ingestion.daemon.ports.sync_logger import KIND_CREDENTIAL_REFRESH, SqliteSyncLogger
from ingestion.schemas import SyncStats

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from structlog.stdlib import BoundLogger

    from ingestion.daemon.auth.refresh import RefreshResult, TokenRefresher
    from ingestion.daemon.auth.strategy import RefreshStrategy
    from ingestion.ports import TokenStore


@dataclass(frozen=True)
class BulkRefreshStats:
    attempted: int = 0
    succeeded: int = 0
    no_cookies: int = 0
    no_tokens: int = 0
    expired: int = 0
    errored: int = 0
    duration_seconds: float = 0.0
    results: tuple[RefreshResult, ...] = ()


def plan_jittered_starts(
    count: int,
    *,
    interval_seconds: float,
    rng: random.Random | None = None,
) -> list[float]:
    """Return per-user start offsets within the half-open ``[0, interval_seconds)``.

    The cycle partitions the window into ``count`` equal slots and picks a
    uniformly-random point inside each slot. ``random.random()`` is
    half-open on the upper end, so the returned offsets never equal
    ``interval_seconds`` exactly.

    With ``count=0`` returns an empty list. With ``interval_seconds=0``
    returns ``count`` zeros (everything starts immediately, useful in tests).
    """
    if count <= 0:
        return []
    if interval_seconds <= 0:
        return [0.0] * count
    r = rng or random.Random()
    slot = interval_seconds / count
    # random.random() is [0, 1); multiplying keeps the upper bound exclusive.
    return [slot * i + r.random() * slot for i in range(count)]


async def run_bulk_refresh_cycle(
    *,
    user_emails: list[str],
    token_store: TokenStore,
    refresher: TokenRefresher,
    interval_seconds: int,
    timeout_seconds: int = 30,
    max_retries_per_user: int = 0,
    concurrency: int = 8,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    shutdown: asyncio.Event | None = None,
    sync_logger: SqliteSyncLogger | None = None,
    user_locks: dict[str, asyncio.Lock] | None = None,
) -> BulkRefreshStats:
    """Run one full refresh cycle over the supplied user list.

    ``concurrency`` caps how many refresh_one calls are in flight at once
    via a queue + bounded worker pool. Default 8 matches a pool size of 4
    × 2 drainers and keeps memory bounded regardless of user count.

    ``shutdown`` lets the outer loop abort mid-cycle: pending offsets are
    abandoned, in-flight refreshes complete, and the partial stats are
    returned.

    ``sync_logger`` is optional; when supplied the cycle writes a
    ``sync_history`` row per user with ``sync_kind='credential_refresh'``.
    A logger failure does not abort the refresh — the credential rotation
    is more important than the audit row.
    """
    log = get_logger(__name__)
    if not user_emails:
        log.info("bulk_refresh.empty_cycle")
        return BulkRefreshStats()

    starts = plan_jittered_starts(
        len(user_emails),
        interval_seconds=float(interval_seconds),
        rng=rng,
    )
    log.info(
        "bulk_refresh.starting",
        user_count=len(user_emails),
        interval_seconds=interval_seconds,
        concurrency=concurrency,
    )
    cycle_start = time.perf_counter()

    # Caller-supplied dict lets the action endpoint share the same lock as
    # the cycle for a given user, so a manual force-refresh and the bulk
    # cycle can't pull on the same Playwright context concurrently and
    # crash with TargetClosedError. Without a shared dict we fall back to a
    # cycle-local one (the historical behavior, fine for tests).
    if user_locks is None:
        user_locks = {}
    results: list[RefreshResult] = []
    results_lock = asyncio.Lock()

    queue: asyncio.Queue[tuple[float, str] | None] = asyncio.Queue()
    for offset, user_email in zip(starts, user_emails, strict=True):
        queue.put_nowait((offset, user_email))
    for _ in range(max(1, concurrency)):
        queue.put_nowait(None)

    async def _worker() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            offset, user_email = item
            if shutdown is not None and shutdown.is_set():
                continue
            if offset > 0:
                try:
                    await sleep(offset)
                except asyncio.CancelledError:
                    raise
            if shutdown is not None and shutdown.is_set():
                continue
            lock = user_locks.setdefault(user_email, asyncio.Lock())
            sync_id = await _maybe_log_start(sync_logger, user_email, log)
            result = await refresh_one(
                user_email=user_email,
                token_store=token_store,
                refresher=refresher,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries_per_user,
                user_lock=lock,
            )
            await _maybe_log_finish(sync_logger, sync_id, result, log)
            async with results_lock:
                results.append(result)

    workers = [
        asyncio.create_task(_worker(), name=f"bulk-refresh-{i}") for i in range(max(1, concurrency))
    ]
    try:
        await asyncio.gather(*workers, return_exceptions=False)
    except asyncio.CancelledError:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    stats = _summarize(results, duration_seconds=time.perf_counter() - cycle_start)
    log.info(
        "bulk_refresh.finished",
        attempted=stats.attempted,
        succeeded=stats.succeeded,
        no_cookies=stats.no_cookies,
        no_tokens=stats.no_tokens,
        expired=stats.expired,
        errored=stats.errored,
        cycle_duration_seconds=round(stats.duration_seconds, 3),
    )
    return stats


async def _maybe_log_start(
    sync_logger: SqliteSyncLogger | None,
    user_email: str,
    log: BoundLogger,
) -> str | None:
    """Open a ``sync_history`` row for this refresh or return ``None``.

    Swallows and logs any exception — the audit row is best-effort; failing
    to write it must not abort the refresh itself.
    """
    if sync_logger is None:
        return None
    try:
        return await sync_logger.log_start(
            user_email,
            "teams",
            sync_kind=KIND_CREDENTIAL_REFRESH,
        )
    except Exception:
        log.exception("bulk_refresh.sync_logger_start_failed")
        return None


async def _maybe_log_finish(
    sync_logger: SqliteSyncLogger | None,
    sync_id: str | None,
    result: RefreshResult,
    log: BoundLogger,
) -> None:
    """Close the ``sync_history`` row opened by :func:`_maybe_log_start`.

    SUCCESS → log_complete with minimal ``SyncStats`` (the cycle doesn't
    carry content-sync counters); everything else → log_failure with the
    outcome enum value plus any driver error string.
    """
    if sync_logger is None or sync_id is None:
        return
    try:
        if result.outcome is RefreshOutcome.SUCCESS:
            await sync_logger.log_complete(sync_id, SyncStats())
        else:
            parts: list[str] = [result.outcome.value]
            if result.error:
                parts.append(result.error[:200])
            await sync_logger.log_failure(sync_id, ": ".join(parts))
    except Exception:
        log.exception("bulk_refresh.sync_logger_finish_failed")


def _summarize(results: list[RefreshResult], *, duration_seconds: float) -> BulkRefreshStats:
    counts = Counter(r.outcome for r in results)
    return BulkRefreshStats(
        attempted=len(results),
        succeeded=counts[RefreshOutcome.SUCCESS],
        no_cookies=counts[RefreshOutcome.NO_COOKIES],
        no_tokens=counts[RefreshOutcome.NO_TOKENS],
        expired=counts[RefreshOutcome.EXPIRED],
        errored=counts[RefreshOutcome.ERROR],
        duration_seconds=duration_seconds,
        # Hold captured tokens off the stats tuple: they're raw bearers,
        # and operators who want per-user debug should use the audit log
        # or /api/syncs drill-downs rather than scraping this aggregate.
        results=tuple(
            type(r)(
                user_email=r.user_email,
                outcome=r.outcome,
                captured=None,
                error=r.error,
            )
            for r in results
        ),
    )


# ---- Polymorphic strategy-driven cycle (slice 8) ---------------------------
# Mirrors :func:`run_bulk_refresh_cycle` but takes ``(email, platform)``
# pairs and a per-platform strategy resolver. Used by the refresh loop
# to fan out across Teams + Moodle + Telegram with one cycle structure
# instead of three. The legacy Teams cycle stays for the maintenance
# service's force-refresh path.


@dataclass(frozen=True)
class PolymorphicBulkStats:
    """Aggregate counters across all platforms touched in one cycle.

    Distinct from :class:`BulkRefreshStats` (Teams-shaped legacy) so a
    test reading the polymorphic cycle's output never accidentally
    treats it as Teams-only. ``per_platform`` is keyed on platform
    name so the dashboard can render one row per platform.
    """

    attempted: int = 0
    succeeded: int = 0
    expired: int = 0
    no_tokens: int = 0
    no_cookies: int = 0
    errored: int = 0
    duration_seconds: float = 0.0
    per_platform: dict[str, dict[str, int]] | None = None
    results: tuple[CredentialRefreshOutcome, ...] = ()


async def run_polymorphic_refresh_cycle(
    *,
    candidates: list[tuple[str, str]],
    token_store: TokenStore,
    strategy_for: Callable[[str], RefreshStrategy | None],
    interval_seconds: int,
    concurrency: int = 8,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    shutdown: asyncio.Event | None = None,
    sync_logger: SqliteSyncLogger | None = None,
    user_locks: dict[str, asyncio.Lock] | None = None,
) -> PolymorphicBulkStats:
    """Run one polymorphic refresh cycle across mixed-platform candidates.

    ``candidates`` is a list of ``(user_email, platform)`` tuples.
    ``strategy_for(platform)`` returns the registered strategy or
    ``None`` — when ``None`` the candidate is skipped silently
    (the refresh loop has already pre-filtered, this is a defense
    in depth).

    Same jitter-then-fan-out shape as :func:`run_bulk_refresh_cycle`:
    each candidate gets a uniformly-random offset within
    ``interval_seconds`` and a worker pool processes the queue.
    Per-user locks come from the shared ``user_locks`` dict so a
    Teams force-refresh and a Telegram cycle don't double up on the
    same email's lock.
    """
    log = get_logger(__name__)
    if not candidates:
        log.info("bulk_refresh.polymorphic_empty_cycle")
        return PolymorphicBulkStats()

    starts = plan_jittered_starts(
        len(candidates),
        interval_seconds=float(interval_seconds),
        rng=rng,
    )
    log.info(
        "bulk_refresh.polymorphic_starting",
        candidate_count=len(candidates),
        interval_seconds=interval_seconds,
        concurrency=concurrency,
    )
    cycle_start = time.perf_counter()

    if user_locks is None:
        user_locks = {}
    results: list[CredentialRefreshOutcome] = []
    results_lock = asyncio.Lock()

    queue: asyncio.Queue[tuple[float, str, str] | None] = asyncio.Queue()
    for offset, (email, platform) in zip(starts, candidates, strict=True):
        queue.put_nowait((offset, email, platform))
    for _ in range(max(1, concurrency)):
        queue.put_nowait(None)

    async def _worker() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            offset, user_email, platform = item
            if shutdown is not None and shutdown.is_set():
                continue
            if offset > 0:
                try:
                    await sleep(offset)
                except asyncio.CancelledError:
                    raise
            if shutdown is not None and shutdown.is_set():
                continue
            strategy = strategy_for(platform)
            if strategy is None:
                # Defense in depth — refresh_loop pre-filters but a
                # registry edit between filter and dispatch could let
                # an unsupported platform through. Drop quietly.
                continue
            lock = user_locks.setdefault(user_email, asyncio.Lock())
            sync_id = await _maybe_log_start_polymorphic(sync_logger, user_email, platform, log)
            outcome = await refresh_credential(
                user_email=user_email,
                platform=platform,
                token_store=token_store,
                strategy=strategy,
                user_lock=lock,
            )
            await _maybe_log_finish_polymorphic(sync_logger, sync_id, outcome, log)
            async with results_lock:
                results.append(outcome)

    workers = [
        asyncio.create_task(_worker(), name=f"polymorphic-refresh-{i}")
        for i in range(max(1, concurrency))
    ]
    try:
        await asyncio.gather(*workers, return_exceptions=False)
    except asyncio.CancelledError:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    stats = _summarize_polymorphic(results, duration_seconds=time.perf_counter() - cycle_start)
    log.info(
        "bulk_refresh.polymorphic_finished",
        attempted=stats.attempted,
        succeeded=stats.succeeded,
        expired=stats.expired,
        no_tokens=stats.no_tokens,
        errored=stats.errored,
        cycle_duration_seconds=round(stats.duration_seconds, 3),
    )
    return stats


async def _maybe_log_start_polymorphic(
    sync_logger: SqliteSyncLogger | None,
    user_email: str,
    platform: str,
    log: BoundLogger,
) -> str | None:
    if sync_logger is None:
        return None
    try:
        return await sync_logger.log_start(
            user_email,
            platform,
            sync_kind=KIND_CREDENTIAL_REFRESH,
        )
    except Exception:
        log.exception("bulk_refresh.polymorphic_sync_logger_start_failed")
        return None


async def _maybe_log_finish_polymorphic(
    sync_logger: SqliteSyncLogger | None,
    sync_id: str | None,
    outcome: CredentialRefreshOutcome,
    log: BoundLogger,
) -> None:
    if sync_logger is None or sync_id is None:
        return
    try:
        if outcome.outcome is RefreshOutcome.SUCCESS:
            await sync_logger.log_complete(sync_id, SyncStats())
        else:
            parts: list[str] = [outcome.outcome.value]
            if outcome.error:
                parts.append(outcome.error[:200])
            await sync_logger.log_failure(sync_id, ": ".join(parts))
    except Exception:
        log.exception("bulk_refresh.polymorphic_sync_logger_finish_failed")


def _summarize_polymorphic(
    results: list[CredentialRefreshOutcome],
    *,
    duration_seconds: float,
) -> PolymorphicBulkStats:
    counts = Counter(r.outcome for r in results)
    per_platform: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = per_platform.setdefault(
            r.platform,
            {"attempted": 0, "succeeded": 0, "expired": 0, "no_tokens": 0, "errored": 0},
        )
        bucket["attempted"] += 1
        if r.outcome is RefreshOutcome.SUCCESS:
            bucket["succeeded"] += 1
        elif r.outcome is RefreshOutcome.EXPIRED:
            bucket["expired"] += 1
        elif r.outcome is RefreshOutcome.ERROR:
            bucket["errored"] += 1
        else:
            bucket["no_tokens"] += 1
    return PolymorphicBulkStats(
        attempted=len(results),
        succeeded=counts[RefreshOutcome.SUCCESS],
        expired=counts[RefreshOutcome.EXPIRED],
        no_tokens=counts[RefreshOutcome.NO_TOKENS],
        no_cookies=counts[RefreshOutcome.NO_COOKIES],
        errored=counts[RefreshOutcome.ERROR],
        duration_seconds=duration_seconds,
        per_platform=per_platform,
        results=tuple(results),
    )
