"""AlertEngine: rule CRUD, metric evaluation, cooldown."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ingestion.daemon.alerts.engine import AlertEngine, _cooldown_bucket
from ingestion.daemon.alerts.metrics import SqliteMetricProvider
from ingestion.daemon.alerts.rules import (
    AlertRule,
    Comparator,
    create_rule,
    delete_rule,
    get_rule,
    list_rules,
    set_enabled,
)
from ingestion.daemon.store.local import apply_migrations
from ingestion.daemon.store.logs import prune_alert_firings_older_than


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


# --- Rule CRUD ---


async def test_create_and_get_rule(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path,
        name="too-many-failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5.0,
        window_seconds=300,
        cooldown_seconds=60,
    )
    rule = await get_rule(db_path, rule_id)
    assert rule is not None
    assert rule.name == "too-many-failures"
    assert rule.comparator is Comparator.GT
    assert rule.threshold == 5.0
    assert rule.enabled is True


async def test_list_rules_filter_enabled(db_path: Path) -> None:
    await create_rule(db_path, name="a", metric="m", comparator=Comparator.GT, threshold=1)
    b_id = await create_rule(db_path, name="b", metric="m", comparator=Comparator.GT, threshold=1)
    await set_enabled(db_path, b_id, enabled=False)

    all_rules = await list_rules(db_path)
    enabled = await list_rules(db_path, enabled_only=True)
    assert len(all_rules) == 2
    assert len(enabled) == 1
    assert enabled[0].name == "a"


async def test_delete_rule(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path, name="gone", metric="m", comparator=Comparator.GT, threshold=1
    )
    await delete_rule(db_path, rule_id)
    assert await get_rule(db_path, rule_id) is None


# --- Comparator semantics ---


@pytest.mark.parametrize(
    ("comparator", "value", "threshold", "expected"),
    [
        (Comparator.GT, 6, 5, True),
        (Comparator.GT, 5, 5, False),
        (Comparator.GTE, 5, 5, True),
        (Comparator.LT, 4, 5, True),
        (Comparator.LTE, 5, 5, True),
        (Comparator.EQ, 5, 5, True),
        (Comparator.EQ, 6, 5, False),
    ],
)
def test_rule_evaluate(
    comparator: Comparator, value: float, threshold: float, expected: bool
) -> None:
    rule = AlertRule(
        id=1,
        name="x",
        enabled=True,
        metric="m",
        comparator=comparator,
        threshold=threshold,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=None,
    )
    assert rule.evaluate(value) is expected


# --- Engine tick ---


class StaticProvider:
    """Deterministic metric values keyed by rule name."""

    def __init__(self, values: dict[str, float]) -> None:
        self.values = values

    async def value_for(self, rule: AlertRule) -> float | None:
        return self.values.get(rule.name)


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float]] = []

    async def alert_fired(self, *, rule: AlertRule, value: float, fired_at: datetime) -> None:
        self.calls.append((rule.id, value))


async def test_tick_fires_on_breach_and_records_firing(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path,
        name="failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5,
        cooldown_seconds=0,
    )
    notifier = RecordingNotifier()
    engine = AlertEngine(
        db_path,
        provider=StaticProvider({"failures": 7.0}),
        notifier=notifier,
    )

    result = await engine.tick()

    assert result.fired == [rule_id]
    assert notifier.calls == [(rule_id, 7.0)]
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_firings WHERE rule_id = ?", (rule_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


async def test_tick_does_not_fire_when_under_threshold(db_path: Path) -> None:
    await create_rule(
        db_path,
        name="failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5,
    )
    engine = AlertEngine(db_path, provider=StaticProvider({"failures": 4.0}))
    result = await engine.tick()
    assert result.fired == []


async def test_tick_cooldown_prevents_re_fire(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path,
        name="failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5,
        cooldown_seconds=3600,
    )
    provider = StaticProvider({"failures": 10.0})
    engine = AlertEngine(db_path, provider=provider)

    first = await engine.tick()
    assert first.fired == [rule_id]
    second = await engine.tick()
    assert second.fired == []  # cooldown blocks


async def test_tick_fires_again_after_cooldown(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path,
        name="failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5,
        cooldown_seconds=60,
    )
    provider = StaticProvider({"failures": 10.0})
    clock = {"t": datetime.now(tz=UTC)}

    def _now() -> datetime:
        return clock["t"]

    engine = AlertEngine(db_path, provider=provider, now=_now)

    first = await engine.tick()
    assert first.fired == [rule_id]
    clock["t"] = clock["t"] + timedelta(seconds=120)
    second = await engine.tick()
    assert second.fired == [rule_id]


async def test_tick_skips_disabled_rules(db_path: Path) -> None:
    rule_id = await create_rule(
        db_path,
        name="failures",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5,
    )
    await set_enabled(db_path, rule_id, enabled=False)
    engine = AlertEngine(db_path, provider=StaticProvider({"failures": 100.0}))

    result = await engine.tick()
    assert result.evaluated == 0
    assert result.fired == []


async def test_tick_skips_rule_with_missing_metric(db_path: Path) -> None:
    await create_rule(
        db_path,
        name="mystery",
        metric="unknown_metric",
        comparator=Comparator.GT,
        threshold=1,
    )
    engine = AlertEngine(db_path, provider=StaticProvider({}))  # no value for rule
    result = await engine.tick()
    assert result.evaluated == 1
    assert result.fired == []


# --- SqliteMetricProvider ---


async def test_sqlite_provider_failure_count(db_path: Path) -> None:
    now = datetime.now(tz=UTC)
    conn = sqlite3.connect(db_path)
    try:
        for i in range(3):
            conn.execute(
                """
                INSERT INTO sync_history (
                    id, user_email, platform, started_at, finished_at, state, error
                )
                VALUES (?, 'a@x', 'moodle', ?, ?, 'failed', 'boom')
                """,
                (f"s{i}", now.isoformat(), now.isoformat()),
            )
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state)
            VALUES ('ok', 'a@x', 'moodle', ?, ?, 'complete')
            """,
            (now.isoformat(), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    provider = SqliteMetricProvider(db_path)
    rule = AlertRule(
        id=1,
        name="x",
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
        window_seconds=300,
        cooldown_seconds=0,
        user_email="a@x",
        platform="moodle",
    )
    assert await provider.value_for(rule) == 3.0


async def test_sqlite_provider_unknown_metric_returns_none(db_path: Path) -> None:
    provider = SqliteMetricProvider(db_path)
    rule = AlertRule(
        id=1,
        name="x",
        enabled=True,
        metric="something_else",
        comparator=Comparator.GT,
        threshold=1,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=None,
    )
    assert await provider.value_for(rule) is None


# --- Cooldown bucket / cross-process dedup ---


def test_cooldown_bucket_groups_same_window() -> None:
    """Two firings within one cooldown_seconds window land in the same bucket."""
    t = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    assert _cooldown_bucket(t, 300) == _cooldown_bucket(t + timedelta(seconds=120), 300)


def test_cooldown_bucket_separates_adjacent_windows() -> None:
    t = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    first = _cooldown_bucket(t, 300)
    second = _cooldown_bucket(t + timedelta(seconds=301), 300)
    assert first != second


def test_cooldown_bucket_is_none_when_zero() -> None:
    """A zero-cooldown rule opts out of the unique index by storing NULL."""
    t = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    assert _cooldown_bucket(t, 0) is None
    assert _cooldown_bucket(t, -1) is None


async def test_record_firing_rejects_duplicate_bucket(db_path: Path) -> None:
    """Two engine instances sharing a clock can't both insert the same bucket.

    Simulates the cross-process race: two engine objects against the same
    DB file, each with the in-process cooldown check bypassed (we insert
    the second directly via ``_record_firing``). The unique partial index
    on ``(rule_id, cooldown_bucket)`` must reject the second insert.
    """
    rule_id = await create_rule(
        db_path,
        name="race",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
        cooldown_seconds=300,
    )
    rule = await get_rule(db_path, rule_id)
    assert rule is not None

    engine_a = AlertEngine(db_path, provider=StaticProvider({"race": 5.0}))
    engine_b = AlertEngine(db_path, provider=StaticProvider({"race": 5.0}))
    fired_at = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)

    inserted_a = await engine_a._record_firing(rule, value=5.0, fired_at=fired_at)
    inserted_b = await engine_b._record_firing(
        rule, value=5.0, fired_at=fired_at + timedelta(seconds=60)
    )

    assert inserted_a is True
    assert inserted_b is False

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_firings WHERE rule_id = ?", (rule_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


async def test_concurrent_ticks_produce_single_firing(db_path: Path) -> None:
    """Two concurrent engine ticks on the same rule record at most one firing."""
    import asyncio

    rule_id = await create_rule(
        db_path,
        name="concurrent",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
        cooldown_seconds=600,
    )
    # Pin both engines to the same wall clock so they compute the same bucket.
    fixed_now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    provider = StaticProvider({"concurrent": 10.0})
    engine_a = AlertEngine(db_path, provider=provider, now=lambda: fixed_now)
    engine_b = AlertEngine(db_path, provider=provider, now=lambda: fixed_now)

    result_a, result_b = await asyncio.gather(engine_a.tick(), engine_b.tick())

    fired_total = (result_a.fired or []) + (result_b.fired or [])
    assert fired_total == [rule_id]

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_firings WHERE rule_id = ?", (rule_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


async def test_zero_cooldown_allows_multiple_firings(db_path: Path) -> None:
    """Zero-cooldown rules store NULL buckets, so the unique index doesn't apply."""
    rule_id = await create_rule(
        db_path,
        name="no-cooldown",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
        cooldown_seconds=0,
    )
    rule = await get_rule(db_path, rule_id)
    assert rule is not None
    engine = AlertEngine(db_path, provider=StaticProvider({"no-cooldown": 5.0}))
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    assert await engine._record_firing(rule, value=5.0, fired_at=now) is True
    assert await engine._record_firing(rule, value=5.0, fired_at=now) is True

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_firings WHERE rule_id = ?", (rule_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2


# --- alert_firings retention ---


async def _insert_firing(
    db_path: Path,
    *,
    rule_id: int,
    fired_at: datetime,
    cooldown_bucket: int | None = None,
) -> None:
    """Insert a firing row directly so retention tests can seed arbitrary ages."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO alert_firings (
                rule_id, fired_at, metric_value, user_email, platform,
                state, resolved_at, trigger_context_json, cooldown_bucket
            )
            VALUES (?, ?, 1.0, NULL, NULL, 'active', NULL, NULL, ?)
            """,
            (rule_id, fired_at.isoformat(), cooldown_bucket),
        )
        conn.commit()
    finally:
        conn.close()


async def test_prune_alert_firings_deletes_expired(db_path: Path) -> None:
    """Firings older than ``retention_days`` are removed."""
    rule_id = await create_rule(
        db_path, name="r", metric="failure_count", comparator=Comparator.GT, threshold=0
    )
    now = datetime.now(tz=UTC)
    for i in range(3):
        await _insert_firing(
            db_path,
            rule_id=rule_id,
            fired_at=now - timedelta(days=45 + i),
            cooldown_bucket=1000 + i,
        )

    deleted = await prune_alert_firings_older_than(db_path, retention_days=30)

    assert deleted == 3
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM alert_firings").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


async def test_prune_alert_firings_preserves_fresh(db_path: Path) -> None:
    """Firings newer than the cutoff are kept."""
    rule_id = await create_rule(
        db_path, name="r", metric="failure_count", comparator=Comparator.GT, threshold=0
    )
    now = datetime.now(tz=UTC)
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(days=45))
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(days=10))
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(hours=1))

    deleted = await prune_alert_firings_older_than(db_path, retention_days=30)

    assert deleted == 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT fired_at FROM alert_firings ORDER BY fired_at").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2


async def test_prune_alert_firings_retention_zero_wipes_all(db_path: Path) -> None:
    """``retention_days=0`` removes every row (cutoff == now)."""
    rule_id = await create_rule(
        db_path, name="r", metric="failure_count", comparator=Comparator.GT, threshold=0
    )
    now = datetime.now(tz=UTC)
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(seconds=5))
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(seconds=50))

    deleted = await prune_alert_firings_older_than(db_path, retention_days=0)
    assert deleted == 2


async def test_prune_alert_firings_rejects_negative_retention(db_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        await prune_alert_firings_older_than(db_path, retention_days=-1)


async def test_prune_alert_firings_chunks_commits(db_path: Path) -> None:
    """Large delete fans out into multiple batches so the write lock is
    released between them. We set ``batch_size=2`` and seed 5 expired rows;
    the function must return all 5 after iterating at least three commits.
    """
    rule_id = await create_rule(
        db_path, name="r", metric="failure_count", comparator=Comparator.GT, threshold=0
    )
    now = datetime.now(tz=UTC)
    for i in range(5):
        await _insert_firing(
            db_path,
            rule_id=rule_id,
            fired_at=now - timedelta(days=60, seconds=i),
            cooldown_bucket=2000 + i,
        )
    # Seed one fresh row that must survive the chunked delete.
    await _insert_firing(db_path, rule_id=rule_id, fired_at=now - timedelta(hours=1))

    deleted = await prune_alert_firings_older_than(db_path, retention_days=30, batch_size=2)

    assert deleted == 5
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT fired_at FROM alert_firings").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


async def test_prune_alert_firings_handles_empty_table(db_path: Path) -> None:
    deleted = await prune_alert_firings_older_than(db_path, retention_days=30)
    assert deleted == 0
