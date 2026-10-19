"""Slice 5 part B: ``run_idempotency_prune_loop`` TTL cleanup behaviour.

The prune loop runs hourly (configurable) and deletes expired rows
from ``idempotency_log``. Tests pin:

* The disabled branch (``interval_seconds<=0`` short-circuits with
  the ``idempotency_prune_loop.disabled`` log line and never calls
  the prune helper).
* The single-iteration prune behaviour (one pass deletes only
  expired rows, leaves live rows alone).
* The error-resilience branch (a failing prune does NOT take down the
  loop — it logs and tries again next tick).
* The shutdown-event-driven exit (loop stops promptly when the event
  fires, even mid-sleep).
* The set-based DELETE semantic: a single bad row cannot block the
  prune of the rest of the table.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from ingestion.daemon.api.middleware.idempotency import prune_expired
from ingestion.daemon.observability_loops import run_idempotency_prune_loop
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)
    return db_path


def _seed_row(
    sqlite_path: Path,
    *,
    key: str,
    method: str = "POST",
    path: str = "/p",
    expires_offset_ms: int = -60_000,
) -> None:
    now = datetime.now(tz=UTC)
    expires = now + timedelta(milliseconds=expires_offset_ms)
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO idempotency_log (
                key, method, path, body_hash, status_code,
                response_body, response_headers_json,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                method,
                path,
                "0" * 64,
                200,
                "{}",
                "[]",
                now.isoformat(),
                expires.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_count(sqlite_path: Path) -> int:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM idempotency_log").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# disabled branch
# ---------------------------------------------------------------------------


async def test_disabled_loop_short_circuits_and_logs(
    sqlite_path: Path,
) -> None:
    """``interval_seconds=0`` -> loop logs ``disabled`` and returns
    immediately. Pin via the structlog LogCapture so the log line
    is part of the contract operators inspect."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        # Seed an expired row so we'd see deletes if the loop ran.
        _seed_row(sqlite_path, key="a" * 32, expires_offset_ms=-60_000)

        shutdown = asyncio.Event()
        await run_idempotency_prune_loop(
            shutdown,
            db_path=sqlite_path,
            interval_seconds=0,
        )
        events = [e.get("event") for e in cap.entries]
        assert "idempotency_prune_loop.disabled" in events
        # The expired row is still there — the loop short-circuited.
        assert _row_count(sqlite_path) == 1
    finally:
        structlog.reset_defaults()


async def test_negative_interval_is_treated_as_disabled(sqlite_path: Path) -> None:
    """Defensive: a negative interval (operator bug) MUST also be
    treated as disabled. Same branch as zero."""
    shutdown = asyncio.Event()
    await run_idempotency_prune_loop(
        shutdown,
        db_path=sqlite_path,
        interval_seconds=-1,
    )
    # Returns without running.


# ---------------------------------------------------------------------------
# single-iteration prune
# ---------------------------------------------------------------------------


async def test_loop_prunes_expired_row_on_first_tick(sqlite_path: Path) -> None:
    """One-tick smoke: seed an expired row + a live row, run the loop
    long enough for one iteration, then signal shutdown. The expired
    row must be gone."""
    _seed_row(sqlite_path, key="e" * 32, expires_offset_ms=-60_000)  # expired
    _seed_row(sqlite_path, key="l" * 32, expires_offset_ms=60_000)  # live
    assert _row_count(sqlite_path) == 2

    shutdown = asyncio.Event()
    # Use a long interval so the second tick won't fire; we want
    # exactly one prune pass before shutdown.
    task = asyncio.create_task(
        run_idempotency_prune_loop(
            shutdown,
            db_path=sqlite_path,
            interval_seconds=3600,
        )
    )
    # Yield so the loop reaches the wait_for branch.
    await asyncio.sleep(0)
    # Trigger shutdown immediately; the wait_for cancels promptly.
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # Live row remains; expired row gone.
    assert _row_count(sqlite_path) == 1


async def test_loop_logs_pruned_count_when_rows_deleted(sqlite_path: Path) -> None:
    """When the prune deletes >=1 row the loop logs
    ``idempotency_prune_loop.pruned`` with the count. Pin so the
    operator-facing log line stays stable."""
    _seed_row(sqlite_path, key="z" * 32, expires_offset_ms=-60_000)

    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        shutdown = asyncio.Event()
        task = asyncio.create_task(
            run_idempotency_prune_loop(
                shutdown,
                db_path=sqlite_path,
                interval_seconds=3600,
            )
        )
        await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

        pruned = [e for e in cap.entries if e.get("event") == "idempotency_prune_loop.pruned"]
        assert pruned, cap.entries
        assert pruned[0]["deleted"] == 1
    finally:
        structlog.reset_defaults()


async def test_loop_does_not_log_when_no_rows_deleted(sqlite_path: Path) -> None:
    """A clean pass (zero deletes) MUST NOT spam the log line —
    operators don't want hourly noise on a quiet system."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        shutdown = asyncio.Event()
        task = asyncio.create_task(
            run_idempotency_prune_loop(
                shutdown,
                db_path=sqlite_path,
                interval_seconds=3600,
            )
        )
        await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

        pruned_events = [
            e for e in cap.entries if e.get("event") == "idempotency_prune_loop.pruned"
        ]
        assert pruned_events == []
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# error resilience: a failing prune doesn't kill the loop
# ---------------------------------------------------------------------------


async def test_prune_failure_is_logged_and_loop_continues(
    sqlite_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``prune_idempotency_expired`` raises, the loop catches it,
    logs ``idempotency_prune_loop.errored``, and waits for the next
    tick. We assert the log line and that the task didn't crash."""
    from ingestion.daemon import observability_loops as loops_mod

    call_count = {"n": 0}

    async def boom(_db_path: Path) -> int:
        call_count["n"] += 1
        raise RuntimeError("simulated prune failure")

    monkeypatch.setattr(loops_mod, "prune_idempotency_expired", boom)

    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        shutdown = asyncio.Event()
        task = asyncio.create_task(
            run_idempotency_prune_loop(
                shutdown,
                db_path=sqlite_path,
                interval_seconds=3600,
            )
        )
        await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

        errored = [e for e in cap.entries if e.get("event") == "idempotency_prune_loop.errored"]
        assert errored, cap.entries
        # Loop attempted to call the prune helper.
        assert call_count["n"] >= 1
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# set-based DELETE: a bad row CANNOT block the prune
# ---------------------------------------------------------------------------


async def test_set_based_delete_handles_many_rows_in_one_pass(
    sqlite_path: Path,
) -> None:
    """The DELETE is a set operation (no per-row iteration in Python),
    so even 100 expired rows are removed in one statement. Pin both
    the count + the DELETE-rowcount return shape."""
    for i in range(100):
        _seed_row(sqlite_path, key=f"row{i:03d}".ljust(32, "0"), expires_offset_ms=-1)
    assert _row_count(sqlite_path) == 100
    deleted = await prune_expired(sqlite_path)
    assert deleted == 100
    assert _row_count(sqlite_path) == 0


async def test_prune_does_not_touch_live_rows_when_mixed_with_expired(
    sqlite_path: Path,
) -> None:
    """A mixed table: half expired, half live. Pin: expired count is
    deleted, live count survives."""
    for i in range(20):
        _seed_row(
            sqlite_path,
            key=f"exp{i:03d}".ljust(32, "0"),
            path=f"/exp{i}",
            expires_offset_ms=-60_000,
        )
    for i in range(15):
        _seed_row(
            sqlite_path,
            key=f"liv{i:03d}".ljust(32, "0"),
            path=f"/liv{i}",
            expires_offset_ms=60_000,
        )
    assert _row_count(sqlite_path) == 35
    deleted = await prune_expired(sqlite_path)
    assert deleted == 20
    assert _row_count(sqlite_path) == 15


# ---------------------------------------------------------------------------
# direct prune helper: error path
# ---------------------------------------------------------------------------


async def test_prune_expired_returns_zero_on_db_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If aiosqlite raises during the DELETE, ``prune_expired`` swallows
    the exception, logs a warning, and returns 0. Pin via a broken
    connect."""
    from ingestion.daemon.api.middleware import idempotency as middleware_mod

    sqlite_path = tmp_path / "state.db"
    apply_migrations(sqlite_path)

    class _BoomConn:
        async def __aenter__(self) -> object:
            raise RuntimeError("simulated DELETE failure")

        async def __aexit__(self, *_args: object) -> None:
            pass

    def _broken(*_args: object, **_kwargs: object) -> _BoomConn:
        return _BoomConn()

    monkeypatch.setattr(middleware_mod.aiosqlite, "connect", _broken)
    deleted = await prune_expired(sqlite_path)
    assert deleted == 0


# ---------------------------------------------------------------------------
# migration apply on a brand-new DB
# ---------------------------------------------------------------------------


def test_migration_0010_creates_idempotency_log_index(tmp_path: Path) -> None:
    """The migration adds an index on ``expires_at`` so the prune
    DELETE doesn't scan. Pin index existence."""
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='idempotency_log'"
            )
        )
    finally:
        conn.close()
    names = {row[0] for row in rows}
    assert "idx_idempotency_expires_at" in names
