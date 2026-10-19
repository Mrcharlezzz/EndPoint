"""Metric providers for the alert engine.

A :class:`MetricProvider` answers "what's the current value for rule R?".
The Phase 4 built-in provider ships three metrics out of the box, plus
per-sync telemetry metrics added in slice 13:

- ``failure_count``: total failed syncs in ``rule.window_seconds`` (default
  300). Scope: user and platform if set on the rule.
- ``last_success_age_seconds``: elapsed seconds since the most recent
  completed sync. Requires ``user_email`` and ``platform`` on the rule.
- ``dead_letter_count``: number of suppressed rows in
  ``dead_letter_state``. Optionally scoped to a platform.
- ``sync_duration_seconds_p95``: 95th-percentile total_duration_seconds
  from ``sync_profiles`` over the rule window. Approximates p95 with a
  rank query (no extension needed). Optional platform/user scope.
- ``sync_duration_seconds_max``: max sync duration in window. Easier to
  alert on than p95 for sub-N populations where the percentile collapses.
- ``items_fetched_max``: max items_fetched in the window. Catches a
  runaway sync pulling too much data in one cycle.
- ``refresh_failures_total``: count of failed credential-refresh syncs
  (sync_kind='credential_refresh') in the window. Scope: platform if set.

Metric names are strings so future providers can slot in without touching
the engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.daemon.alerts.rules import AlertRule


class MetricProvider(Protocol):
    async def value_for(self, rule: AlertRule) -> float | None: ...


class SqliteMetricProvider:
    """Reads from the same SQLite file the rest of the daemon uses."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def value_for(self, rule: AlertRule) -> float | None:
        if rule.metric == "failure_count":
            return await self._failure_count(rule)
        if rule.metric == "last_success_age_seconds":
            return await self._last_success_age_seconds(rule)
        if rule.metric == "dead_letter_count":
            return await self._dead_letter_count(rule)
        if rule.metric == "sync_duration_seconds_p95":
            return await self._sync_duration_p95(rule)
        if rule.metric == "sync_duration_seconds_max":
            return await self._sync_duration_max(rule)
        if rule.metric == "items_fetched_max":
            return await self._items_fetched_max(rule)
        if rule.metric == "refresh_failures_total":
            return await self._refresh_failures_total(rule)
        return None

    async def _failure_count(self, rule: AlertRule) -> float:
        # Filter the window by finished_at so a long-running sync that
        # started before the window and failed inside it still counts.
        # Covers the "5 failures in the last 5 minutes" case intuitively.
        clauses: list[str] = ["state = 'failed'", "finished_at IS NOT NULL"]
        params: list[object] = []
        if rule.window_seconds is not None:
            since = (datetime.now(tz=UTC) - timedelta(seconds=rule.window_seconds)).isoformat()
            clauses.append("finished_at >= ?")
            params.append(since)
        if rule.user_email is not None:
            clauses.append("user_email = ?")
            params.append(rule.user_email)
        if rule.platform is not None:
            clauses.append("platform = ?")
            params.append(rule.platform)
        sql = f"SELECT COUNT(*) FROM sync_history WHERE {' AND '.join(clauses)}"
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        return float(row[0] if row is not None else 0)

    async def _last_success_age_seconds(self, rule: AlertRule) -> float | None:
        if rule.user_email is None or rule.platform is None:
            return None
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT finished_at FROM sync_history
                    WHERE user_email = ? AND platform = ? AND state = 'complete'
                    ORDER BY finished_at DESC LIMIT 1
                    """,
                    (rule.user_email, rule.platform),
                )
            ).fetchone()
        if row is None or row[0] is None:
            return None
        last = datetime.fromisoformat(str(row[0]))
        return (datetime.now(tz=UTC) - last).total_seconds()

    async def _dead_letter_count(self, rule: AlertRule) -> float:
        now = datetime.now(tz=UTC).isoformat()
        clauses: list[str] = ["suppressed_until IS NOT NULL", "suppressed_until > ?"]
        params: list[object] = [now]
        if rule.platform is not None:
            clauses.append("platform = ?")
            params.append(rule.platform)
        if rule.user_email is not None:
            clauses.append("user_email = ?")
            params.append(rule.user_email)
        sql = f"SELECT COUNT(*) FROM dead_letter_state WHERE {' AND '.join(clauses)}"
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        return float(row[0] if row is not None else 0)

    async def _sync_duration_p95(self, rule: AlertRule) -> float | None:
        """Approximate p95 of ``total_duration_seconds`` from ``sync_profiles``.

        Uses rank-from-the-top in a sub-query (avoids requiring an
        aggregate-percentile extension). With N rows in window, rank 0 is the
        slowest and we pick the value at index ``floor(N * 0.05)``. For very
        small windows (N < 20) this collapses to the slowest row, which is
        the conservative behaviour we want for alerting.
        """
        rows = await self._duration_window_rows(rule)
        if not rows:
            return None
        # Already DESC; index ``floor(N*0.05)`` is the p95 cutoff.
        idx = int(len(rows) * 0.05)
        idx = min(idx, len(rows) - 1)
        return float(rows[idx])

    async def _sync_duration_max(self, rule: AlertRule) -> float | None:
        rows = await self._duration_window_rows(rule, limit=1)
        if not rows:
            return None
        return float(rows[0])

    async def _duration_window_rows(
        self, rule: AlertRule, *, limit: int | None = None
    ) -> list[float]:
        """Pull ``total_duration_seconds`` for the rule window in DESC order.

        ``limit`` keeps the read cheap when the caller only needs the tail
        (max). p95 needs the full window. ``sync_profiles.created_at`` is the
        completion timestamp so the window aligns with the completion
        boundary regardless of how long the sync actually ran.
        """
        clauses: list[str] = ["total_duration_seconds IS NOT NULL"]
        params: list[object] = []
        if rule.window_seconds is not None:
            since = (datetime.now(tz=UTC) - timedelta(seconds=rule.window_seconds)).isoformat()
            clauses.append("created_at >= ?")
            params.append(since)
        if rule.user_email is not None:
            clauses.append("user_email = ?")
            params.append(rule.user_email)
        if rule.platform is not None:
            clauses.append("platform = ?")
            params.append(rule.platform)
        sql = (
            "SELECT total_duration_seconds FROM sync_profiles "
            f"WHERE {' AND '.join(clauses)} ORDER BY total_duration_seconds DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        async with aiosqlite.connect(self._db_path) as conn:
            rows = await (await conn.execute(sql, params)).fetchall()
        return [float(r[0]) for r in rows]

    async def _items_fetched_max(self, rule: AlertRule) -> float | None:
        clauses: list[str] = ["items_fetched IS NOT NULL"]
        params: list[object] = []
        if rule.window_seconds is not None:
            since = (datetime.now(tz=UTC) - timedelta(seconds=rule.window_seconds)).isoformat()
            clauses.append("created_at >= ?")
            params.append(since)
        if rule.user_email is not None:
            clauses.append("user_email = ?")
            params.append(rule.user_email)
        if rule.platform is not None:
            clauses.append("platform = ?")
            params.append(rule.platform)
        sql = f"SELECT MAX(items_fetched) FROM sync_profiles WHERE {' AND '.join(clauses)}"
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    async def _refresh_failures_total(self, rule: AlertRule) -> float:
        """Count failed credential-refresh syncs in the window.

        ``sync_kind='credential_refresh'`` is the bucket the Teams refresh
        loop writes to (see :class:`SqliteSyncLogger`). Distinct from
        ``failure_count`` so an operator can wire one rule to content-sync
        regressions and another to credential-refresh storms without
        cross-contamination.
        """
        clauses: list[str] = [
            "state = 'failed'",
            "sync_kind = 'credential_refresh'",
            "finished_at IS NOT NULL",
        ]
        params: list[object] = []
        if rule.window_seconds is not None:
            since = (datetime.now(tz=UTC) - timedelta(seconds=rule.window_seconds)).isoformat()
            clauses.append("finished_at >= ?")
            params.append(since)
        if rule.platform is not None:
            clauses.append("platform = ?")
            params.append(rule.platform)
        if rule.user_email is not None:
            clauses.append("user_email = ?")
            params.append(rule.user_email)
        sql = f"SELECT COUNT(*) FROM sync_history WHERE {' AND '.join(clauses)}"
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        return float(row[0] if row is not None else 0)
