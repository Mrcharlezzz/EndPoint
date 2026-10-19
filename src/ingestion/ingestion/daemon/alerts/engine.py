"""Alert engine: evaluate every enabled rule and fire on breach.

The engine is a pure orchestrator: it loads rules, asks the
:class:`MetricProvider` for current values, applies the rule's comparator,
writes firings, and calls the :class:`Notifier`. Cooldown state lives on
the ``alert_firings`` table (most-recent fired_at per rule), so restarts
don't re-fire in a loop.

Cross-process cooldown dedup is enforced at the SQLite layer via the unique
partial index on ``(rule_id, cooldown_bucket)`` added in migration 0007.
The in-process ``_within_cooldown`` check is a fast path; an
``IntegrityError`` on insert is treated as "already fired, skip" so races
between overlapping ticks or a second daemon can't double-fire.

Firing order per tick is rule-id ascending for determinism; tests rely on it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from ingestion.daemon.alerts.rules import list_rules
from ingestion.daemon.logging import get_logger
from ingestion.daemon.store.async_conn import connect

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ingestion.daemon.alerts.metrics import MetricProvider
    from ingestion.daemon.alerts.rules import AlertRule


class AlertNotifier(Protocol):
    """Notification sink invoked on each firing. Must not raise."""

    async def alert_fired(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> None: ...


class NullAlertNotifier:
    async def alert_fired(
        self,
        *,
        rule: AlertRule,
        value: float,
        fired_at: datetime,
    ) -> None:
        return None


@dataclass(frozen=True)
class TickResult:
    evaluated: int
    fired: list[int]  # rule ids that fired this tick


class AlertEngine:
    def __init__(
        self,
        db_path: Path,
        *,
        provider: MetricProvider,
        notifier: AlertNotifier | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._provider = provider
        self._notifier = notifier or NullAlertNotifier()
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(tz=UTC))
        self._log = get_logger(__name__)
        # The cooldown check and firing insert are two separate aiosqlite
        # connections. Two overlapping ticks (or a tick racing a force-fire
        # action once the API lands) could both pass the check. A per-
        # engine lock serialises tick cycles cheaply.
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> TickResult:
        async with self._tick_lock:
            rules = await list_rules(self._db_path, enabled_only=True)
            fired: list[int] = []
            for rule in rules:
                try:
                    if await self._evaluate_and_maybe_fire(rule):
                        fired.append(rule.id)
                except Exception:
                    self._log.exception(
                        "alerts.rule_errored",
                        rule_id=rule.id,
                        rule_name=rule.name,
                    )
            return TickResult(evaluated=len(rules), fired=fired)

    async def _evaluate_and_maybe_fire(self, rule: AlertRule) -> bool:
        value = await self._provider.value_for(rule)
        if value is None:
            # A missing metric can mean the rule is misconfigured (scoped
            # to a user that doesn't exist) or that the metric provider
            # has no data. Log once per tick so the operator notices
            # instead of silently skipping.
            self._log.warning(
                "alerts.metric_unavailable",
                rule_id=rule.id,
                rule_name=rule.name,
                metric=rule.metric,
            )
            return False
        if not rule.evaluate(value):
            # Breach condition no longer holds: mark any active firings
            # for this rule as resolved so the UI clears them.
            await self._resolve_active_firings(rule.id, now=self._now(), metric_value=value)
            return False
        now = self._now()
        if await self._within_cooldown(rule, now=now):
            return False
        if not await self._record_firing(rule, value=value, fired_at=now):
            # Another tick or process beat us to the unique index. Treat as
            # "already fired in this cooldown bucket" and skip the notifier
            # so only one side emits the user-visible alert.
            self._log.info(
                "alerts.race_skipped",
                rule_id=rule.id,
                rule_name=rule.name,
            )
            return False
        self._log.info(
            "alerts.fired",
            rule_id=rule.id,
            rule_name=rule.name,
            value=value,
            threshold=rule.threshold,
        )
        try:
            await self._notifier.alert_fired(rule=rule, value=value, fired_at=now)
        except Exception:
            self._log.exception("alerts.notifier_errored", rule_id=rule.id)
        return True

    async def _within_cooldown(self, rule: AlertRule, *, now: datetime) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        cutoff = (now - timedelta(seconds=rule.cooldown_seconds)).isoformat()
        async with aiosqlite.connect(self._db_path) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT 1 FROM alert_firings
                    WHERE rule_id = ? AND fired_at >= ?
                    LIMIT 1
                    """,
                    (rule.id, cutoff),
                )
            ).fetchone()
        return row is not None

    async def _record_firing(
        self,
        rule: AlertRule,
        *,
        value: float,
        fired_at: datetime,
    ) -> bool:
        """Insert the firing row. Returns True on success, False if the
        unique partial index rejected the row because another writer already
        recorded a firing in the same cooldown bucket.
        """
        import json

        trigger_context = json.dumps(
            {
                "metric": rule.metric,
                "value": value,
                "threshold": rule.threshold,
                "comparator": rule.comparator.value,
                "window_seconds": rule.window_seconds,
            },
            separators=(",", ":"),
        )
        bucket = _cooldown_bucket(fired_at, rule.cooldown_seconds)
        try:
            async with connect(self._db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO alert_firings (
                        rule_id, fired_at, metric_value, user_email, platform,
                        state, resolved_at, trigger_context_json, cooldown_bucket
                    )
                    VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?)
                    """,
                    (
                        rule.id,
                        fired_at.isoformat(),
                        value,
                        rule.user_email,
                        rule.platform,
                        trigger_context,
                        bucket,
                    ),
                )
                await conn.commit()
        except aiosqlite.IntegrityError:
            return False
        return True

    async def _resolve_active_firings(
        self,
        rule_id: int,
        *,
        now: datetime,
        metric_value: float,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                UPDATE alert_firings
                SET state = 'resolved', resolved_at = ?
                WHERE rule_id = ? AND state = 'active'
                """,
                (now.isoformat(), rule_id),
            )
            await conn.commit()
        if cursor.rowcount and cursor.rowcount > 0:
            self._log.info(
                "alerts.resolved",
                rule_id=rule_id,
                resolved_count=cursor.rowcount,
                metric_value=metric_value,
            )


def _cooldown_bucket(fired_at: datetime, cooldown_seconds: int) -> int | None:
    """Compute the cooldown bucket key for a firing.

    Returns ``None`` when the rule has no cooldown; the unique partial index
    excludes NULL buckets, so a zero-cooldown rule can fire freely. For
    positive cooldown windows, two firings for the same rule within the same
    ``cooldown_seconds``-wide window land in the same bucket and the second
    insert is rejected by the index.
    """
    if cooldown_seconds <= 0:
        return None
    return int(fired_at.timestamp() // cooldown_seconds)
