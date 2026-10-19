"""SqliteSyncLogger.log_complete writes both sync_history and sync_profiles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
from ingestion.daemon.store.local import apply_migrations
from ingestion.schemas import ScopeStats, SyncStats


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _profile_row(db_path: Path, sync_id: str) -> dict[str, object] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM sync_profiles WHERE sync_id = ?",
            (sync_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


async def test_log_complete_writes_profile_row(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")
    stats = SyncStats(
        items_fetched=10,
        items_new=7,
        items_updated=3,
        duration_seconds=2.5,
        api_call_duration_seconds=1.8,
        normalization_duration_seconds=0.6,
        per_scope_stats={
            "assignments": ScopeStats(items_fetched=4, items_new=4),
            "forums": ScopeStats(items_fetched=6, items_new=3, items_updated=3),
        },
    )
    await logger.log_complete(sync_id, stats)

    row = _profile_row(db_path, sync_id)
    assert row is not None
    assert row["user_email"] == "alice@example.com"
    assert row["platform"] == "moodle"
    assert row["total_duration_seconds"] == pytest.approx(2.5)
    assert row["api_call_duration_seconds"] == pytest.approx(1.8)
    assert row["normalization_duration_seconds"] == pytest.approx(0.6)
    assert row["items_fetched"] == 10
    assert row["items_new"] == 7
    assert row["items_updated"] == 3
    assert row["error_count"] == 0
    assert row["created_at"] is not None

    scoped = json.loads(row["per_scope_json"])  # type: ignore[arg-type]
    assert scoped["assignments"]["items_fetched"] == 4
    assert scoped["forums"]["items_new"] == 3


async def test_log_complete_upserts_profile_row(db_path: Path) -> None:
    # Re-calling log_complete (e.g. retry) must overwrite, not dupe.
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")
    await logger.log_complete(sync_id, SyncStats(items_fetched=1))
    await logger.log_complete(sync_id, SyncStats(items_fetched=99))

    row = _profile_row(db_path, sync_id)
    assert row is not None
    assert row["items_fetched"] == 99


async def test_log_failure_does_not_write_profile(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")
    await logger.log_failure(sync_id, "boom")
    assert _profile_row(db_path, sync_id) is None


async def test_log_complete_raises_without_sync_history_row(db_path: Path) -> None:
    # Contract: log_complete must have a preceding log_start. Silently
    # dropping the profile hides caller bugs — raise instead so the sync
    # pipeline treats the mismatch as a real failure.
    import pytest

    logger = SqliteSyncLogger(db_path)
    with pytest.raises(ValueError, match="call log_start first"):
        await logger.log_complete("unknown-id", SyncStats())
    assert _profile_row(db_path, "unknown-id") is None
