"""Background tasks that keep observability data fresh.

Phase 4 wiring. Several loops run alongside the tick and refresh loops:

- :func:`run_retention_loop` prunes the ``logs`` table on a coarse schedule.
  Default: once per hour, retention 30 days. Set
  ``ENDPOINT_DAEMON__OBSERVABILITY__RETENTION_DAYS=0`` to disable.
- :func:`run_alert_engine_loop` ticks the :class:`AlertEngine` every 30
  seconds (configurable). Set the interval to 0 to skip. Optionally
  publishes fire/resolve events to the shared EventBus on the ``alerts``
  channel so the dashboard's /ws/alerts subscribers see live updates.
- :func:`run_metrics_publisher_loop` polls the worker pool + dead-letter
  table periodically and publishes a snapshot to the ``metrics`` channel.
- :func:`run_idempotency_prune_loop` prunes expired ``idempotency_log``
  rows hourly so the cache table doesn't grow unbounded under bursty
  retries (slice 5 part B).

None of these loops hold state between ticks; they can be restarted freely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import aiosqlite

from ingestion.daemon.alerts.engine import AlertEngine
from ingestion.daemon.alerts.metrics import SqliteMetricProvider
from ingestion.daemon.api.middleware import prune_idempotency_expired
from ingestion.daemon.logging import get_logger
from ingestion.daemon.store.logs import prune_alert_firings_older_than, prune_logs_older_than

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ingestion.daemon.alerts.engine import AlertNotifier
    from ingestion.daemon.worker import WorkerPool


async def run_retention_loop(
    shutdown: asyncio.Event,
    *,
    db_path: Path,
    retention_days: int,
    interval_seconds: int = 3600,
) -> None:
    """Delete logs and alert firings older than ``retention_days``.

    Runs every ``interval_seconds``. Exits promptly on shutdown. Logs a
    warning on any error and keeps going; a single bad run shouldn't take
    the loop down. Each prune pass is independent so a failure against one
    table does not block the other.
    """
    log = get_logger(__name__)
    if retention_days <= 0:
        log.info("retention_loop.disabled")
        return
    while not shutdown.is_set():
        try:
            deleted = await prune_logs_older_than(db_path, retention_days=retention_days)
            if deleted:
                log.info("retention_loop.pruned", deleted=deleted, retention_days=retention_days)
        except Exception:
            log.exception("retention_loop.errored")
        try:
            deleted_firings = await prune_alert_firings_older_than(
                db_path, retention_days=retention_days
            )
            if deleted_firings:
                log.info(
                    "retention_loop.firings_pruned",
                    deleted=deleted_firings,
                    retention_days=retention_days,
                )
        except Exception:
            log.exception("retention_loop.firings_errored")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)


async def run_alert_engine_loop(
    shutdown: asyncio.Event,
    *,
    db_path: Path,
    interval_seconds: int = 30,
    event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    notifier: AlertNotifier | None = None,
) -> None:
    """Tick the default :class:`AlertEngine` every ``interval_seconds``.

    Uses :class:`SqliteMetricProvider`. ``notifier`` is the external
    delivery sink (e.g. :class:`WebhookAlertNotifier` from slice 13);
    when omitted, the engine falls back to its built-in null notifier
    so the loop still ticks and writes firings to SQLite. When
    ``event_sink`` is passed, fire/resolve events are also published
    to the ``alerts`` channel so /ws/alerts subscribers see a live
    feed.
    """
    log = get_logger(__name__)
    if interval_seconds <= 0:
        log.info("alert_engine_loop.disabled")
        return
    engine = AlertEngine(
        db_path,
        provider=SqliteMetricProvider(db_path),
        notifier=notifier,
    )
    # Track which rules are currently "active" (fired and not yet resolved)
    # so the resolution edge can fire an alerts.resolved event. The engine's
    # _resolve_active_firings updates the DB atomically; we diff our view
    # against the DB each tick.
    active_rule_ids: set[int] = set()
    while not shutdown.is_set():
        try:
            result = await engine.tick()
            if result.fired:
                log.info("alert_engine_loop.fired", rule_ids=result.fired)
            if event_sink is not None:
                await _publish_alert_changes(
                    db_path=db_path,
                    fired_rule_ids=result.fired,
                    active_rule_ids=active_rule_ids,
                    sink=event_sink,
                )
        except Exception:
            log.exception("alert_engine_loop.errored")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)


async def _publish_alert_changes(
    *,
    db_path: Path,
    fired_rule_ids: list[int],
    active_rule_ids: set[int],
    sink: Callable[[str, dict[str, Any]], None],
) -> None:
    """Diff the DB's active firings against our view and publish deltas.

    Newly-fired rows emit ``alert.fired``; rows that flipped to resolved
    since last tick emit ``alert.resolved``. This keeps the /ws/alerts
    subscribers in lockstep without re-reading the whole table each tick.
    """
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                """
                SELECT id, rule_id, fired_at, metric_value, user_email, platform,
                       state, resolved_at, trigger_context_json
                FROM alert_firings
                WHERE state = 'active'
                """
            )
        ).fetchall()
    current_active: set[int] = set()
    row_by_rule: dict[int, dict[str, Any]] = {}
    for row in rows:
        rule_id = int(row["rule_id"])
        current_active.add(rule_id)
        row_by_rule[rule_id] = dict(row)

    for rule_id in fired_rule_ids:
        row = row_by_rule.get(rule_id)
        if row is None:
            continue
        ctx_json = row.pop("trigger_context_json", None)
        row["trigger_context"] = json.loads(ctx_json) if ctx_json else None
        sink("alerts", {"event": "alert.fired", **row})

    resolved = active_rule_ids - current_active
    for rule_id in resolved:
        sink("alerts", {"event": "alert.resolved", "rule_id": rule_id})

    active_rule_ids.clear()
    active_rule_ids.update(current_active)


async def run_metrics_publisher_loop(
    shutdown: asyncio.Event,
    *,
    pool: WorkerPool,
    db_path: Path,
    interval_seconds: int = 5,
    event_sink: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """Publish a rolling metrics snapshot to the ``metrics`` channel.

    Snapshot fields: in_flight task count, counts per-platform in the
    dead-letter table, pending force-sync requests. The dashboard's
    Overview page subscribes to this for live counters. Cheap queries on
    small tables — the interval default (5s) is fine.
    """
    log = get_logger(__name__)
    if interval_seconds <= 0 or event_sink is None:
        log.info("metrics_publisher_loop.disabled")
        return
    while not shutdown.is_set():
        try:
            snapshot = await _metrics_snapshot(pool=pool, db_path=db_path)
            event_sink("metrics", {"event": "metrics.snapshot", **snapshot})
        except Exception:
            log.exception("metrics_publisher_loop.errored")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)


async def run_idempotency_prune_loop(
    shutdown: asyncio.Event,
    *,
    db_path: Path,
    interval_seconds: int = 3600,
) -> None:
    """Hourly prune of expired ``idempotency_log`` rows (slice 5 part B).

    The middleware writes a row per cached mutating response with a
    24h default TTL. Without a prune the table grows linearly with
    request volume; this loop keeps it bounded. A failure inside the
    DELETE is logged and the loop continues — a single bad pass must
    not block the next one.

    Set ``interval_seconds=0`` to disable the loop entirely (useful in
    tests where the prune behaviour is exercised explicitly).
    """
    log = get_logger(__name__)
    if interval_seconds <= 0:
        log.info("idempotency_prune_loop.disabled")
        return
    while not shutdown.is_set():
        try:
            deleted = await prune_idempotency_expired(db_path)
            if deleted:
                log.info("idempotency_prune_loop.pruned", deleted=deleted)
        except Exception:
            log.exception("idempotency_prune_loop.errored")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)


async def _metrics_snapshot(*, pool: WorkerPool, db_path: Path) -> dict[str, Any]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        dl_row = await (
            await conn.execute(
                "SELECT COUNT(*) AS n FROM dead_letter_state WHERE suppressed_until IS NOT NULL"
            )
        ).fetchone()
        fs_row = await (
            await conn.execute("SELECT COUNT(*) AS n FROM force_sync_requests")
        ).fetchone()
        failed_row = await (
            await conn.execute(
                "SELECT COUNT(*) AS n FROM sync_history "
                "WHERE state = 'failed' "
                "AND started_at >= datetime('now', '-1 hour')"
            )
        ).fetchone()
    return {
        "in_flight": pool.in_flight,
        "dead_letter_suppressed": int(dl_row["n"]) if dl_row is not None else 0,
        "force_sync_pending": int(fs_row["n"]) if fs_row is not None else 0,
        "failed_last_hour": int(failed_row["n"]) if failed_row is not None else 0,
    }
