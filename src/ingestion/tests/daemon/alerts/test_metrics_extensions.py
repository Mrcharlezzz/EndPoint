"""Slice 13 part A metric extensions on :class:`SqliteMetricProvider`.

Pins the four new metric branches:

* ``sync_duration_seconds_p95`` — rank-based query against ``sync_profiles``.
  For N<20 rows the p95 collapses to the slowest row (index 0 after DESC sort).
* ``sync_duration_seconds_max`` — slowest row in the window.
* ``items_fetched_max`` — peak ``items_fetched`` in the window via SQL MAX().
* ``refresh_failures_total`` — count of ``sync_kind='credential_refresh'``
  failures in the window.

Tests seed the relevant tables directly via stdlib ``sqlite3`` to keep
the writes synchronous and easy to reason about; the metric provider
itself uses :mod:`aiosqlite`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ingestion.daemon.alerts.metrics import SqliteMetricProvider
from ingestion.daemon.alerts.rules import AlertRule, Comparator
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _rule(
    metric: str,
    *,
    window_seconds: int | None = 3600,
    platform: str | None = None,
) -> AlertRule:
    return AlertRule(
        id=1,
        name="r",
        enabled=True,
        metric=metric,
        comparator=Comparator.GT,
        threshold=0.0,
        window_seconds=window_seconds,
        cooldown_seconds=0,
        user_email=None,
        platform=platform,
    )


def _seed_profile(
    db_path: Path,
    *,
    sync_id: str,
    user_email: str = "u@x",
    platform: str = "teams",
    duration: float | None = 1.0,
    items_fetched: int | None = 0,
    created_at: datetime | None = None,
) -> None:
    when = (created_at or datetime.now(tz=UTC)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_profiles (
                sync_id, user_email, platform, total_duration_seconds,
                api_call_duration_seconds, normalization_duration_seconds,
                items_fetched, items_new, items_updated, error_count,
                per_scope_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sync_id,
                user_email,
                platform,
                duration,
                None,
                None,
                items_fetched,
                0,
                0,
                0,
                None,
                when,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_history(
    db_path: Path,
    *,
    sync_id: str,
    state: str = "failed",
    sync_kind: str = "content_sync",
    user_email: str = "u@x",
    platform: str = "teams",
    finished_at: datetime | None = None,
) -> None:
    when = (finished_at or datetime.now(tz=UTC)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state, sync_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sync_id, user_email, platform, when, when, state, sync_kind),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sync_duration_seconds_p95
# ---------------------------------------------------------------------------


async def test_sync_duration_p95_returns_slowest_row_when_under_twenty(db_path: Path) -> None:
    """N<20 rows: ``floor(N * 0.05)`` is 0 → p95 collapses to the slowest row.

    The impl docstring pins this as "the conservative behaviour we want
    for alerting" — a small sample size shouldn't dilute the alert. With
    5 rows the slowest dictates the metric."""
    durations = [1.0, 2.0, 3.0, 100.0, 5.0]
    for i, d in enumerate(durations):
        _seed_profile(db_path, sync_id=f"s{i}", duration=d)
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_p95"))
    assert value == 100.0


async def test_sync_duration_p95_returns_none_when_window_empty(db_path: Path) -> None:
    """Empty ``sync_profiles`` window → ``None``. The engine treats that
    as "metric unavailable" and skips the rule rather than firing on 0."""
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_p95"))
    assert value is None


async def test_sync_duration_p95_respects_window_bound(db_path: Path) -> None:
    """A row outside ``window_seconds`` must NOT influence the p95."""
    now = datetime.now(tz=UTC)
    _seed_profile(db_path, sync_id="recent", duration=10.0, created_at=now)
    _seed_profile(
        db_path,
        sync_id="ancient",
        duration=999.0,
        created_at=now - timedelta(days=2),
    )
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_p95", window_seconds=300))
    # Only the recent row is in the 5-minute window.
    assert value == 10.0


async def test_sync_duration_p95_picks_index_when_n_large(db_path: Path) -> None:
    """N=20 rows → idx = floor(20 * 0.05) = 1 → second slowest row.

    The impl uses ``ORDER BY total_duration_seconds DESC`` then indexes
    floor(N*0.05). Construct a 20-row window where row 1 (after DESC
    sort) is a known value and assert that's what gets returned."""
    # Row 0 (slowest): 1000s
    # Row 1: 500s     <-- expected p95 with N=20
    # Rows 2..19: smaller values
    durations = [1000.0, 500.0, *(float(i) for i in range(20, 2, -1))]
    assert len(durations) == 20
    for i, d in enumerate(durations):
        _seed_profile(db_path, sync_id=f"s{i}", duration=d)
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_p95"))
    assert value == 500.0


# ---------------------------------------------------------------------------
# sync_duration_seconds_max
# ---------------------------------------------------------------------------


async def test_sync_duration_max_returns_slowest_row(db_path: Path) -> None:
    for i, d in enumerate([1.0, 50.0, 3.0]):
        _seed_profile(db_path, sync_id=f"s{i}", duration=d)
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_max"))
    assert value == 50.0


async def test_sync_duration_max_returns_none_when_empty(db_path: Path) -> None:
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("sync_duration_seconds_max"))
    assert value is None


# ---------------------------------------------------------------------------
# items_fetched_max — verify the right column
# ---------------------------------------------------------------------------


async def test_items_fetched_max_queries_items_fetched_column(db_path: Path) -> None:
    """The metric must read ``items_fetched`` and not, e.g., ``items_new``.

    Seed three rows where ``items_fetched`` and ``items_new`` are
    intentionally different and check the metric returns the
    items_fetched maximum."""
    conn = sqlite3.connect(db_path)
    try:
        for i, fetched in enumerate([100, 7000, 50]):
            conn.execute(
                """
                INSERT INTO sync_profiles (
                    sync_id, user_email, platform, total_duration_seconds,
                    api_call_duration_seconds, normalization_duration_seconds,
                    items_fetched, items_new, items_updated, error_count,
                    per_scope_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"s{i}",
                    "u@x",
                    "teams",
                    1.0,
                    None,
                    None,
                    fetched,
                    1,  # items_new (small) — wrong column would return 1
                    0,
                    0,
                    None,
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("items_fetched_max"))
    assert value == 7000.0


async def test_items_fetched_max_returns_none_when_empty(db_path: Path) -> None:
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("items_fetched_max"))
    assert value is None


async def test_items_fetched_max_respects_platform_scope(db_path: Path) -> None:
    """Platform scoping must filter by ``platform = ?`` so a moodle row
    can't poison a teams alert."""
    _seed_profile(db_path, sync_id="t1", platform="teams", items_fetched=10)
    _seed_profile(db_path, sync_id="m1", platform="moodle", items_fetched=999)
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("items_fetched_max", platform="teams"))
    assert value == 10.0


# ---------------------------------------------------------------------------
# refresh_failures_total
# ---------------------------------------------------------------------------


async def test_refresh_failures_total_counts_only_credential_refresh_failures(
    db_path: Path,
) -> None:
    """Must filter on ``sync_kind='credential_refresh'`` so a content-sync
    failure storm does NOT bleed into the credential-refresh alert (and
    vice versa)."""
    _seed_history(db_path, sync_id="r1", sync_kind="credential_refresh", state="failed")
    _seed_history(db_path, sync_id="r2", sync_kind="credential_refresh", state="failed")
    _seed_history(db_path, sync_id="c1", sync_kind="content_sync", state="failed")
    _seed_history(db_path, sync_id="rok", sync_kind="credential_refresh", state="complete")
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("refresh_failures_total"))
    assert value == 2.0


async def test_refresh_failures_total_zero_on_empty(db_path: Path) -> None:
    """Empty table → 0.0, not ``None`` (this is a count not a percentile)."""
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("refresh_failures_total"))
    assert value == 0.0


async def test_refresh_failures_total_filters_by_window(db_path: Path) -> None:
    now = datetime.now(tz=UTC)
    _seed_history(
        db_path,
        sync_id="recent",
        sync_kind="credential_refresh",
        state="failed",
        finished_at=now,
    )
    _seed_history(
        db_path,
        sync_id="old",
        sync_kind="credential_refresh",
        state="failed",
        finished_at=now - timedelta(hours=2),
    )
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("refresh_failures_total", window_seconds=600))
    # Only the recent row is in the 10min window.
    assert value == 1.0


async def test_refresh_failures_total_respects_platform_scope(db_path: Path) -> None:
    _seed_history(
        db_path,
        sync_id="t1",
        sync_kind="credential_refresh",
        state="failed",
        platform="teams",
    )
    _seed_history(
        db_path,
        sync_id="m1",
        sync_kind="credential_refresh",
        state="failed",
        platform="moodle",
    )
    provider = SqliteMetricProvider(db_path)
    value = await provider.value_for(_rule("refresh_failures_total", platform="teams"))
    assert value == 1.0
