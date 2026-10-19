"""Cascade-delete + TTL prune for daemon-owned tables (slice 12).

Pins the lifecycle module's contract:

* :func:`cascade_delete_user` wipes every per-user row in one
  transaction, returns counts, and is a safe no-op for unknown email;
* TTL prunes only delete eligible rows (terminal sync_history,
  cleared dead_letter rows, plain timestamp threshold for audit_log);
* a sync currently in-flight (``state='running'``) survives the
  sync_history prune even past the cutoff;
* per-scope cursor cleanup deletes rows for scopes outside the active
  set; ``frozenset()`` is the documented kill switch;
* the optional worker-pool argument frees the per-user semaphore so
  the slice-10 leak pin resolves.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.lifecycle.cleanup import (
    cascade_delete_user,
    cleanup_stale_cursors,
    prune_audit_log,
    prune_dead_letter,
    prune_sync_history,
)
from ingestion.daemon.lifecycle.loops import run_lifecycle_prune_loop
from ingestion.daemon.store.local import apply_migrations
from ingestion.daemon.worker import WorkerPool


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _exec(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _count(db_path: Path, table: str, where: str = "1=1", params: tuple[Any, ...] = ()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        # Statement is internal to tests with a static table list — not
        # composed from external input.
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _seed_user_rows(db_path: Path, email: str = "alice@x", platform: str = "moodle") -> None:
    """Seed one row in every cascade-delete table for ``email``."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"sh-{email}-1", email, platform, now, now, "complete"),
    )
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures) "
        "VALUES (?, ?, ?)",
        (email, platform, 0),
    )
    _exec(
        db_path,
        "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (email, platform, "default", "{}", now),
    )
    _exec(
        db_path,
        "INSERT INTO force_sync_requests (user_email, platform, requested_at, reason) "
        "VALUES (?, ?, ?, ?)",
        (email, platform, now, None),
    )
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "user.pause", email),
    )
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "credential.pause", f"{email}/{platform}"),
    )


# ---------------------------------------------------------------------------
# cascade_delete_user
# ---------------------------------------------------------------------------


async def test_cascade_delete_wipes_all_tables(db_path: Path) -> None:
    """One pass deletes every per-user row across the cascade tables."""
    _seed_user_rows(db_path, email="alice@x", platform="moodle")
    # Seed one row for a different user to confirm scope isolation.
    _seed_user_rows(db_path, email="bob@x", platform="moodle")

    result = await cascade_delete_user(db_path, email="alice@x")

    assert result.sync_history_deleted == 1
    assert result.dead_letter_deleted == 1
    assert result.sync_cursors_deleted == 1
    assert result.force_sync_deleted == 1
    # audit kept by default
    assert result.audit_deleted == 0
    assert _count(db_path, "sync_history", "user_email=?", ("alice@x",)) == 0
    assert _count(db_path, "sync_history", "user_email=?", ("bob@x",)) == 1


async def test_cascade_delete_unknown_email_is_no_op(db_path: Path) -> None:
    """Unknown email returns zero counts without raising."""
    result = await cascade_delete_user(db_path, email="ghost@x")
    assert result.total == 0


async def test_cascade_delete_audit_kept_by_default(db_path: Path) -> None:
    """Compliance default: audit_log entries for the user survive."""
    _seed_user_rows(db_path, email="alice@x")

    await cascade_delete_user(db_path, email="alice@x")

    # The two audit rows seeded for alice must still be present.
    assert _count(db_path, "audit_log", "target=? OR target LIKE ?", ("alice@x", "alice@x/%")) == 2


async def test_cascade_delete_with_include_audit_drops_audit_rows(db_path: Path) -> None:
    _seed_user_rows(db_path, email="alice@x")
    result = await cascade_delete_user(db_path, email="alice@x", include_audit=True)
    assert result.audit_deleted == 2
    assert _count(db_path, "audit_log", "target=? OR target LIKE ?", ("alice@x", "alice@x/%")) == 0


async def test_cascade_delete_emits_event(db_path: Path) -> None:
    """Event sink receives a ``lifecycle.user_deleted`` payload with counts."""
    _seed_user_rows(db_path, email="alice@x")
    captured: list[tuple[str, dict[str, Any]]] = []

    def sink(channel: str, payload: dict[str, Any]) -> None:
        captured.append((channel, payload))

    await cascade_delete_user(db_path, email="alice@x", event_sink=sink)

    assert len(captured) == 1
    channel, payload = captured[0]
    assert channel == "lifecycle"
    assert payload["event"] == "lifecycle.user_deleted"
    assert payload["email"] == "alice@x"
    assert payload["sync_history_deleted"] == 1


async def test_cascade_delete_calls_worker_pool_remove_user(db_path: Path) -> None:
    """The worker_pool argument flushes the per-user semaphore."""
    _seed_user_rows(db_path, email="alice@x")
    pool = WorkerPool(global_limit=2, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001

    await cascade_delete_user(db_path, email="alice@x", worker_pool=pool)

    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001


async def test_cascade_delete_rolls_back_on_failure(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an inner DELETE raises, the transaction rolls back and no
    cascade-table row is removed (single-transaction semantics)."""
    _seed_user_rows(db_path, email="alice@x")

    # Drop a table mid-transaction by patching aiosqlite.connect to
    # return a wrapped connection that raises on the third execute.
    real_connect: Any = None
    import aiosqlite as _aiosqlite

    real_connect = _aiosqlite.connect

    class _RaisingConn:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._calls = 0

        async def __aenter__(self) -> Any:
            self._inner = await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc: object) -> None:
            await self._inner.__aexit__(*exc)

        async def execute(self, sql: str, params: Any = ()) -> Any:
            self._calls += 1
            if self._calls == 4:  # BEGIN, sync_history, dead_letter, then this one
                raise sqlite3.OperationalError("simulated failure")
            return await self._inner.execute(sql, params)

        async def commit(self) -> None:
            await self._inner.commit()

    def patched(path: Any, *args: Any, **kw: Any) -> Any:
        return _RaisingConn(real_connect(path, *args, **kw))

    monkeypatch.setattr("ingestion.daemon.lifecycle.cleanup.aiosqlite.connect", patched)

    with pytest.raises(sqlite3.OperationalError):
        await cascade_delete_user(db_path, email="alice@x")

    # ROLLBACK kept the sync_history row alive.
    assert _count(db_path, "sync_history", "user_email=?", ("alice@x",)) == 1


# ---------------------------------------------------------------------------
# prune_sync_history
# ---------------------------------------------------------------------------


def _seed_sync_history_row(
    db_path: Path,
    *,
    sync_id: str,
    email: str,
    state: str,
    finished_days_ago: int | None,
) -> None:
    started = datetime.now(tz=UTC) - timedelta(days=finished_days_ago or 0)
    finished_at: str | None = None
    if finished_days_ago is not None:
        finished_at = started.isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sync_id, email, "moodle", started.isoformat(), finished_at, state),
    )


async def test_prune_sync_history_drops_old_terminal_rows(db_path: Path) -> None:
    _seed_sync_history_row(
        db_path, sync_id="old-1", email="a@x", state="complete", finished_days_ago=60
    )
    _seed_sync_history_row(
        db_path, sync_id="recent-1", email="a@x", state="complete", finished_days_ago=2
    )

    deleted = await prune_sync_history(db_path, retention_days=30)

    assert deleted == 1
    assert _count(db_path, "sync_history", "id=?", ("old-1",)) == 0
    assert _count(db_path, "sync_history", "id=?", ("recent-1",)) == 1


async def test_prune_sync_history_keeps_running_rows(db_path: Path) -> None:
    """A long-running sync past the cutoff must NOT be deleted —
    ``state='running'`` is excluded from the prune."""
    started = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("running-1", "a@x", "moodle", started, None, "running"),
    )

    deleted = await prune_sync_history(db_path, retention_days=30)

    assert deleted == 0
    assert _count(db_path, "sync_history", "id=?", ("running-1",)) == 1


async def test_prune_sync_history_retention_zero_drops_all_terminal(db_path: Path) -> None:
    """Documented kill switch: retention_days=0 deletes every terminal row."""
    _seed_sync_history_row(
        db_path, sync_id="recent-1", email="a@x", state="complete", finished_days_ago=0
    )
    _seed_sync_history_row(
        db_path, sync_id="run-1", email="a@x", state="running", finished_days_ago=None
    )

    # The "now - 0 days" cutoff still excludes rows finished AT exactly now;
    # but a row finished a microsecond before "now" is < cutoff.
    await asyncio.sleep(0.01)
    deleted = await prune_sync_history(db_path, retention_days=0)

    assert deleted == 1
    assert _count(db_path, "sync_history", "id=?", ("run-1",)) == 1


# ---------------------------------------------------------------------------
# prune_dead_letter
# ---------------------------------------------------------------------------


async def test_prune_dead_letter_drops_old_cleared_rows(db_path: Path) -> None:
    """Cleared rows (count=0, no suppression) older than the cutoff go.

    Active suppressions and rows with non-zero counters survive."""
    old_iso = (datetime.now(tz=UTC) - timedelta(days=200)).isoformat()
    recent_iso = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
    # Cleared, old: should go.
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 0, ?, NULL)",
        ("old-cleared@x", "moodle", old_iso),
    )
    # Cleared, recent: stay.
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 0, ?, NULL)",
        ("recent-cleared@x", "moodle", recent_iso),
    )
    # Old, but still suppressed: must stay.
    suppressed_until = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 5, ?, ?)",
        ("still-suppressed@x", "moodle", old_iso, suppressed_until),
    )

    deleted = await prune_dead_letter(db_path, retention_days=90)

    assert deleted == 1
    assert _count(db_path, "dead_letter_state", "user_email=?", ("old-cleared@x",)) == 0
    assert _count(db_path, "dead_letter_state", "user_email=?", ("recent-cleared@x",)) == 1
    assert _count(db_path, "dead_letter_state", "user_email=?", ("still-suppressed@x",)) == 1


async def test_prune_dead_letter_negative_retention_raises(db_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        await prune_dead_letter(db_path, retention_days=-1)


# ---------------------------------------------------------------------------
# prune_audit_log
# ---------------------------------------------------------------------------


async def test_prune_audit_log_drops_old_rows(db_path: Path) -> None:
    old = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    recent = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat()
    _exec(db_path, "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)", (old, "old.action"))
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
        (recent, "recent.action"),
    )

    deleted = await prune_audit_log(db_path, retention_days=365)

    assert deleted == 1
    assert _count(db_path, "audit_log", "action=?", ("old.action",)) == 0
    assert _count(db_path, "audit_log", "action=?", ("recent.action",)) == 1


# ---------------------------------------------------------------------------
# cleanup_stale_cursors
# ---------------------------------------------------------------------------


async def test_cleanup_stale_cursors_drops_scopes_not_in_active_set(db_path: Path) -> None:
    now = datetime.now(tz=UTC).isoformat()
    for scope in ("default", "channels", "assignments", "old_legacy_scope"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "teams", scope, "{}", now),
        )

    deleted = await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset({"default", "channels", "assignments"}),
    )

    assert deleted == 1
    assert _count(db_path, "sync_cursors", "scope=?", ("old_legacy_scope",)) == 0
    assert _count(db_path, "sync_cursors", "scope=?", ("channels",)) == 1


async def test_cleanup_stale_cursors_empty_set_drops_all(db_path: Path) -> None:
    """Documented kill switch: ``frozenset()`` deletes every cursor for the platform."""
    now = datetime.now(tz=UTC).isoformat()
    for scope in ("default", "channels"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "teams", scope, "{}", now),
        )
    # Insert one for moodle to confirm platform isolation.
    _exec(
        db_path,
        "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("alice@x", "moodle", "default", "{}", now),
    )

    deleted = await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset(),
    )

    assert deleted == 2
    assert _count(db_path, "sync_cursors", "platform=?", ("teams",)) == 0
    assert _count(db_path, "sync_cursors", "platform=?", ("moodle",)) == 1


async def test_cleanup_stale_cursors_emits_one_event_per_scope(db_path: Path) -> None:
    now = datetime.now(tz=UTC).isoformat()
    for scope in ("default", "stale_a", "stale_b"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "teams", scope, "{}", now),
        )
    captured: list[tuple[str, dict[str, Any]]] = []

    def sink(channel: str, payload: dict[str, Any]) -> None:
        captured.append((channel, payload))

    await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset({"default"}),
        event_sink=sink,
    )

    events = [(c, p["event"], p.get("scope")) for c, p in captured]
    assert ("lifecycle", "lifecycle.cursor_pruned", "stale_a") in events
    assert ("lifecycle", "lifecycle.cursor_pruned", "stale_b") in events


# ---------------------------------------------------------------------------
# Lifecycle prune loop
# ---------------------------------------------------------------------------


async def test_lifecycle_prune_loop_runs_then_exits_on_shutdown(db_path: Path) -> None:
    """One iteration completes and the loop exits cleanly when shutdown
    is set. Confirms the loop driver is shutdown-aware."""
    _seed_sync_history_row(
        db_path, sync_id="old", email="a@x", state="complete", finished_days_ago=60
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=30,
            dead_letter_retention_days=0,
            audit_log_retention_days=0,
            interval_seconds=10,
        )
    )
    # Yield the loop so the prune pass runs.
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # The 60-day-old terminal row should be gone.
    assert _count(db_path, "sync_history", "id=?", ("old",)) == 0


async def test_lifecycle_prune_loop_disabled_when_interval_zero(db_path: Path) -> None:
    """``interval_seconds=0`` short-circuits: returns immediately."""
    _seed_sync_history_row(
        db_path, sync_id="old", email="a@x", state="complete", finished_days_ago=60
    )
    shutdown = asyncio.Event()
    await run_lifecycle_prune_loop(
        shutdown,
        db_path=db_path,
        sync_history_retention_days=30,
        dead_letter_retention_days=30,
        audit_log_retention_days=30,
        interval_seconds=0,
    )
    # Nothing pruned because the loop never ran.
    assert _count(db_path, "sync_history", "id=?", ("old",)) == 1
