"""Unit tests for the batched generator variants of ``schedule_queries``.

Cover correctness (batches accumulate to the full set), error guards
(``batch_size < 1``), empty-result short-circuit, and one-connection-per-pass
semantics.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ingestion.daemon.schedule_queries import (
    last_success_at_bulk_batched,
    user_interval_overrides_bulk_batched,
)
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _seed_sync_history(
    db_path: Path,
    rows: list[tuple[str, str, str, datetime | None]],
) -> None:
    """Insert ``(user_email, platform, state, finished_at)`` into sync_history."""
    conn = sqlite3.connect(db_path)
    try:
        for i, (user_email, platform, state, finished_at) in enumerate(rows):
            sync_id = f"sync_{i}"
            started_at = "2026-04-26T10:00:00+00:00"
            finished = finished_at.isoformat() if finished_at else None
            conn.execute(
                """
                INSERT INTO sync_history (
                    id, user_email, platform, state, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sync_id, user_email, platform, state, started_at, finished),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_overrides(
    db_path: Path,
    rows: list[tuple[str, str, str, int]],
) -> None:
    """Insert ``(user_email, platform, scope, interval_seconds)`` into overrides."""
    conn = sqlite3.connect(db_path)
    try:
        for user_email, platform, scope, interval_seconds in rows:
            conn.execute(
                """
                INSERT INTO user_schedule_overrides (
                    user_email, platform, scope, interval_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user_email, platform, scope, interval_seconds, "2026-04-26T10:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# last_success_at_bulk_batched
# ---------------------------------------------------------------------------


async def test_last_success_at_batched_yields_in_chunks(db_path: Path) -> None:
    rows = [(f"user{i}@example.com", "moodle", "complete", datetime.now(tz=UTC)) for i in range(7)]
    _seed_sync_history(db_path, rows)
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path, batch_size=3)]
    # 7 rows / 3 per batch = 3 chunks (3, 3, 1).
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 7


async def test_last_success_at_batched_skips_empty_chunks(db_path: Path) -> None:
    """Rows with finished_at IS NULL are filtered out at the SQL layer.

    Per the helper: empty chunks are not yielded — the inner ``if chunk:``
    guard skips a no-op iteration.
    """
    # Note: SQL filters WHERE state='complete' AND finished_at IS NOT NULL.
    # Seed only one valid row.
    _seed_sync_history(db_path, [("u@x.com", "moodle", "complete", datetime.now(tz=UTC))])
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path, batch_size=10)]
    assert len(chunks) == 1
    assert len(chunks[0]) == 1


async def test_last_success_at_batched_empty_db(db_path: Path) -> None:
    """Empty table — generator terminates without yielding."""
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path)]
    assert chunks == []


async def test_last_success_at_batched_rejects_zero_batch_size(db_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        async for _ in last_success_at_bulk_batched(db_path, batch_size=0):
            pass  # pragma: no cover


async def test_last_success_at_batched_rejects_negative_batch_size(db_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        async for _ in last_success_at_bulk_batched(db_path, batch_size=-1):
            pass  # pragma: no cover


async def test_last_success_at_batched_returns_full_set_when_batch_exceeds(
    db_path: Path,
) -> None:
    rows = [(f"u{i}@x.com", "moodle", "complete", datetime.now(tz=UTC)) for i in range(3)]
    _seed_sync_history(db_path, rows)
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path, batch_size=100)]
    assert len(chunks) == 1
    assert len(chunks[0]) == 3


async def test_last_success_at_batched_picks_max_finished_at(db_path: Path) -> None:
    """The SQL aggregates MAX(finished_at) per (user, platform) — older runs ignored."""
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 4, 1, tzinfo=UTC)
    _seed_sync_history(
        db_path,
        [
            ("u@x.com", "moodle", "complete", older),
            ("u@x.com", "moodle", "complete", newer),
        ],
    )
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path)]
    assert len(chunks) == 1
    timestamp = chunks[0][("u@x.com", "moodle")]
    assert timestamp == newer


async def test_last_success_at_batched_excludes_failed_runs(db_path: Path) -> None:
    _seed_sync_history(
        db_path,
        [("u@x.com", "moodle", "failed", datetime.now(tz=UTC))],
    )
    chunks = [chunk async for chunk in last_success_at_bulk_batched(db_path)]
    assert chunks == []


# ---------------------------------------------------------------------------
# user_interval_overrides_bulk_batched
# ---------------------------------------------------------------------------


async def test_user_overrides_batched_yields_in_chunks(db_path: Path) -> None:
    rows = [(f"u{i}@x.com", "moodle", "default", 60 * (i + 1)) for i in range(5)]
    _seed_overrides(db_path, rows)
    chunks = [chunk async for chunk in user_interval_overrides_bulk_batched(db_path, batch_size=2)]
    # 5 rows / 2 per batch = 3 chunks (2, 2, 1).
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 5


async def test_user_overrides_batched_filters_by_scope(db_path: Path) -> None:
    _seed_overrides(
        db_path,
        [
            ("u@x.com", "moodle", "default", 60),
            ("u@x.com", "moodle", "off-hours", 120),
        ],
    )
    chunks = [
        chunk async for chunk in user_interval_overrides_bulk_batched(db_path, scope="off-hours")
    ]
    assert len(chunks) == 1
    assert chunks[0] == {("u@x.com", "moodle"): 120}


async def test_user_overrides_batched_empty(db_path: Path) -> None:
    chunks = [chunk async for chunk in user_interval_overrides_bulk_batched(db_path)]
    assert chunks == []


async def test_user_overrides_batched_rejects_zero_batch_size(db_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        async for _ in user_interval_overrides_bulk_batched(db_path, batch_size=0):
            pass  # pragma: no cover


async def test_user_overrides_batched_rejects_negative_batch_size(db_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        async for _ in user_interval_overrides_bulk_batched(db_path, batch_size=-2):
            pass  # pragma: no cover


async def test_user_overrides_batched_full_set_one_batch(db_path: Path) -> None:
    rows = [(f"u{i}@x.com", "moodle", "default", 60) for i in range(3)]
    _seed_overrides(db_path, rows)
    chunks = [
        chunk async for chunk in user_interval_overrides_bulk_batched(db_path, batch_size=100)
    ]
    assert len(chunks) == 1
    assert len(chunks[0]) == 3


async def test_user_overrides_batched_returns_int_values(db_path: Path) -> None:
    """Verifies the value coercion to int (DB returns it but defensive cast)."""
    _seed_overrides(db_path, [("u@x.com", "moodle", "default", 90)])
    chunks = [chunk async for chunk in user_interval_overrides_bulk_batched(db_path)]
    interval = chunks[0][("u@x.com", "moodle")]
    assert isinstance(interval, int)
    assert interval == 90


async def test_user_overrides_batched_orders_by_user_platform(db_path: Path) -> None:
    """ORDER BY user_email, platform — verify deterministic ordering."""
    _seed_overrides(
        db_path,
        [
            ("zeta@x.com", "moodle", "default", 60),
            ("alpha@x.com", "moodle", "default", 60),
            ("alpha@x.com", "teams", "default", 90),
        ],
    )
    chunks = [chunk async for chunk in user_interval_overrides_bulk_batched(db_path, batch_size=10)]
    # All in one batch — keys are dict-order from insert.
    keys = list(chunks[0].keys())
    # First two are alpha rows, then zeta.
    assert keys[0] == ("alpha@x.com", "moodle")
    assert keys[1] == ("alpha@x.com", "teams")
    assert keys[2] == ("zeta@x.com", "moodle")
