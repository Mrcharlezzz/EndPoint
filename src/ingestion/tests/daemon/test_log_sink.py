"""SqliteLogHandler + retention: logs land in SQLite, prune_logs_older_than deletes."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ingestion.daemon.store.local import apply_migrations
from ingestion.daemon.store.logs import (
    SqliteLogHandler,
    prune_logs_older_than,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _rows(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM logs ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_handler_writes_plain_message(db_path: Path) -> None:
    handler = SqliteLogHandler(db_path)
    try:
        record = logging.LogRecord(
            name="ingestion.daemon.test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="plain message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        handler.close()

    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "plain message"
    assert rows[0]["level"] == "WARNING"
    assert rows[0]["user_email"] is None
    assert rows[0]["sync_id"] is None
    assert rows[0]["fields_json"] is None


def test_handler_parses_structlog_json_message(db_path: Path) -> None:
    handler = SqliteLogHandler(db_path)
    try:
        payload = {
            "event": "sync.started",
            "user_email": "alice@example.com",
            "platform": "moodle",
            "sync_id": "abc-123",
            "level": "info",
            "timestamp": "2026-04-19T00:00:00+00:00",
            "duration": 1.5,
        }
        record = logging.LogRecord(
            name="ingestion.daemon.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=json.dumps(payload),
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        handler.close()

    rows = _rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "sync.started"
    # Level is canonicalised to upper-case on write so the logs column
    # doesn't mix "info"/"INFO" across structlog and stdlib paths.
    assert row["level"] == "INFO"
    assert row["user_email"] == "alice@example.com"
    assert row["platform"] == "moodle"
    assert row["sync_id"] == "abc-123"
    assert row["timestamp"] == "2026-04-19T00:00:00+00:00"
    # Extra fields land in fields_json
    assert row["fields_json"] is not None
    assert json.loads(row["fields_json"]) == {"duration": 1.5}  # type: ignore[arg-type]


def test_handler_handles_malformed_json(db_path: Path) -> None:
    handler = SqliteLogHandler(db_path)
    try:
        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="{not json",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        handler.close()
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "{not json"


async def test_prune_logs_older_than_deletes_expired(db_path: Path) -> None:
    now = datetime.now(tz=UTC)
    old = (now - timedelta(days=10)).isoformat()
    fresh = (now - timedelta(hours=1)).isoformat()
    iso_now = now.isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for ts in (old, old, fresh):
            conn.execute(
                "INSERT INTO logs (timestamp, level, event, created_at) VALUES (?, 'INFO', 'e', ?)",
                (ts, iso_now),
            )
        conn.commit()
    finally:
        conn.close()

    deleted = await prune_logs_older_than(db_path, retention_days=7)
    assert deleted == 2
    assert len(_rows(db_path)) == 1


async def test_prune_negative_days_raises(db_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        await prune_logs_older_than(db_path, retention_days=-1)
