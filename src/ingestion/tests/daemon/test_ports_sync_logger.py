"""sync_history lifecycle: start -> complete or failure."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from ingestion.daemon.ports.sync_logger import (
    KIND_CONTENT_SYNC,
    KIND_CREDENTIAL_REFRESH,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_RUNNING,
    SqliteSyncLogger,
)
from ingestion.daemon.store.local import apply_migrations
from ingestion.schemas import ScopeStats, SyncStats


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _row(db_path: Path, sync_id: str) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM sync_history WHERE id = ?",
            (sync_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


async def test_log_start_creates_running_row(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")

    row = _row(db_path, sync_id)
    assert row["user_email"] == "alice@example.com"
    assert row["platform"] == "moodle"
    assert row["state"] == STATE_RUNNING
    assert row["started_at"] is not None
    assert row["finished_at"] is None
    assert row["stats_json"] is None
    assert row["error"] is None


async def test_log_complete_transitions_to_complete_and_persists_stats(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")

    stats = SyncStats(
        items_fetched=5,
        items_new=4,
        items_updated=1,
        duration_seconds=1.25,
        per_scope_stats={"messages": ScopeStats(items_fetched=5, items_new=4)},
    )
    await logger.log_complete(sync_id, stats)

    row = _row(db_path, sync_id)
    assert row["state"] == STATE_COMPLETE
    assert row["finished_at"] is not None
    assert row["error"] is None
    assert row["stats_json"] is not None
    loaded = json.loads(row["stats_json"])  # type: ignore[arg-type]
    assert loaded["items_fetched"] == 5
    assert loaded["per_scope_stats"]["messages"]["items_fetched"] == 5


async def test_log_failure_transitions_to_failed_and_persists_error(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")

    await logger.log_failure(sync_id, "token expired")

    row = _row(db_path, sync_id)
    assert row["state"] == STATE_FAILED
    assert row["error"] == "token expired"
    assert row["stats_json"] is None
    assert row["finished_at"] is not None


async def test_log_start_emits_unique_sync_ids(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    ids = {await logger.log_start("alice@example.com", "moodle") for _ in range(5)}
    assert len(ids) == 5


async def test_log_complete_raises_when_no_log_start(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    stats = SyncStats(
        items_fetched=0,
        items_new=0,
        items_updated=0,
        duration_seconds=0.0,
        per_scope_stats={},
    )
    with pytest.raises(ValueError, match="call log_start first"):
        await logger.log_complete("not-a-real-sync-id", stats)


async def test_log_start_defaults_sync_kind_to_content_sync(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")

    row = _row(db_path, sync_id)
    assert row["sync_kind"] == KIND_CONTENT_SYNC


async def test_log_start_accepts_credential_refresh_kind(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start(
        "alice@example.com",
        "teams",
        sync_kind=KIND_CREDENTIAL_REFRESH,
    )

    row = _row(db_path, sync_id)
    assert row["sync_kind"] == KIND_CREDENTIAL_REFRESH
    # State machine is unchanged: a refresh still starts as running and
    # expects a separate log_complete / log_failure to close it out.
    assert row["state"] == STATE_RUNNING


async def test_log_failure_clears_stale_stats_json(db_path: Path) -> None:
    # Clean flows never log_complete followed by log_failure, but if a caller
    # ever does, the failure must overwrite the stale stats payload.
    logger = SqliteSyncLogger(db_path)
    sync_id = await logger.log_start("alice@example.com", "moodle")
    await logger.log_complete(
        sync_id,
        SyncStats(
            items_fetched=3,
            items_new=3,
            items_updated=0,
            duration_seconds=0.5,
            per_scope_stats={},
        ),
    )
    await logger.log_failure(sync_id, "late failure")

    row = _row(db_path, sync_id)
    assert row["state"] == STATE_FAILED
    assert row["stats_json"] is None
    assert row["error"] == "late failure"


async def test_reconcile_flips_orphaned_running_rows_to_failed(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    a = await logger.log_start("alice@example.com", "teams")
    b = await logger.log_start("bob@example.com", "moodle")

    count = await logger.reconcile_orphaned_running()

    assert count == 2
    for sync_id in (a, b):
        row = _row(db_path, sync_id)
        assert row["state"] == STATE_FAILED
        assert row["error"] == "interrupted: daemon restart"
        assert row["finished_at"] is not None


async def test_reconcile_leaves_terminal_rows_alone(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    completed = await logger.log_start("alice@example.com", "teams")
    await logger.log_complete(
        completed,
        SyncStats(
            items_fetched=0,
            items_new=0,
            items_updated=0,
            duration_seconds=0.0,
            per_scope_stats={},
        ),
    )
    failed = await logger.log_start("bob@example.com", "moodle")
    await logger.log_failure(failed, "token expired")
    running = await logger.log_start("carol@example.com", "telegram")

    count = await logger.reconcile_orphaned_running()

    assert count == 1
    assert _row(db_path, completed)["state"] == STATE_COMPLETE
    # A prior-process failure keeps its original error message.
    assert _row(db_path, failed)["state"] == STATE_FAILED
    assert _row(db_path, failed)["error"] == "token expired"
    assert _row(db_path, running)["state"] == STATE_FAILED
    assert _row(db_path, running)["error"] == "interrupted: daemon restart"


async def test_reconcile_returns_zero_when_no_orphans(db_path: Path) -> None:
    logger = SqliteSyncLogger(db_path)
    assert await logger.reconcile_orphaned_running() == 0
