"""DeadLetterTracker: consecutive-failure counting and cooldown-based suppression."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _row(db_path: Path, user_email: str, platform: str) -> dict[str, object] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM dead_letter_state WHERE user_email = ? AND platform = ?",
            (user_email, platform),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


async def test_fresh_row_is_not_suppressed(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)
    assert await tracker.is_suppressed("alice@example.com", "moodle") is False


async def test_failures_under_threshold_do_not_suppress(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)
    await tracker.record_failure("alice@example.com", "moodle")
    await tracker.record_failure("alice@example.com", "moodle")

    assert await tracker.is_suppressed("alice@example.com", "moodle") is False
    row = _row(db_path, "alice@example.com", "moodle")
    assert row is not None
    assert row["consecutive_failures"] == 2
    assert row["suppressed_until"] is None


async def test_reaching_threshold_suppresses(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)
    for _ in range(3):
        await tracker.record_failure("alice@example.com", "moodle")

    assert await tracker.is_suppressed("alice@example.com", "moodle") is True
    row = _row(db_path, "alice@example.com", "moodle")
    assert row is not None
    assert row["consecutive_failures"] == 3
    assert row["suppressed_until"] is not None


async def test_success_resets_counter_and_unsets_suppression_on_next_failure_cycle(
    db_path: Path,
) -> None:
    tracker = DeadLetterTracker(db_path, threshold=2, cooldown_seconds=60)
    await tracker.record_failure("alice@example.com", "moodle")
    await tracker.record_success("alice@example.com", "moodle")

    row = _row(db_path, "alice@example.com", "moodle")
    assert row is not None
    assert row["consecutive_failures"] == 0
    assert row["last_failure_at"] is None

    # After reset, one more failure must not cross the threshold.
    await tracker.record_failure("alice@example.com", "moodle")
    assert await tracker.is_suppressed("alice@example.com", "moodle") is False


async def test_cooldown_expiry_lifts_suppression(db_path: Path) -> None:
    # Zero-second cooldown: suppression expires immediately.
    tracker = DeadLetterTracker(db_path, threshold=1, cooldown_seconds=0)
    await tracker.record_failure("alice@example.com", "moodle")

    # After a beat, the suppressed_until is in the past.
    await asyncio.sleep(0.01)
    assert await tracker.is_suppressed("alice@example.com", "moodle") is False


async def test_cooldown_respected_until_it_elapses(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=1, cooldown_seconds=3600)
    await tracker.record_failure("alice@example.com", "moodle")
    assert await tracker.is_suppressed("alice@example.com", "moodle") is True

    # Manually rewind suppressed_until to the past to simulate time passing.
    past = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE dead_letter_state SET suppressed_until = ? WHERE user_email = ?",
            (past, "alice@example.com"),
        )
        conn.commit()
    finally:
        conn.close()

    assert await tracker.is_suppressed("alice@example.com", "moodle") is False


async def test_clear_un_suppresses_and_resets(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=1, cooldown_seconds=3600)
    await tracker.record_failure("alice@example.com", "moodle")
    assert await tracker.is_suppressed("alice@example.com", "moodle") is True

    await tracker.clear("alice@example.com", "moodle")
    assert await tracker.is_suppressed("alice@example.com", "moodle") is False
    row = _row(db_path, "alice@example.com", "moodle")
    assert row is not None
    assert row["consecutive_failures"] == 0
    assert row["suppressed_until"] is None


async def test_per_row_isolation(db_path: Path) -> None:
    tracker = DeadLetterTracker(db_path, threshold=2, cooldown_seconds=60)
    await tracker.record_failure("alice@example.com", "moodle")
    await tracker.record_failure("alice@example.com", "moodle")
    # Different platform — same user — is a separate row.
    assert await tracker.is_suppressed("alice@example.com", "moodle") is True
    assert await tracker.is_suppressed("alice@example.com", "teams") is False
    # Different user — same platform — is a separate row.
    assert await tracker.is_suppressed("bob@example.com", "moodle") is False


def test_invalid_threshold_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="threshold"):
        DeadLetterTracker(tmp_path / "x.db", threshold=0, cooldown_seconds=60)
