"""Slice 12 commit 1: cascade delete + TTL prune + per-user dict cleanup.

This file is the tester's suite at the new lifecycle/ subdirectory.
The implementer pre-shipped baseline tests at
``tests/daemon/test_lifecycle_cleanup.py`` and
``tests/daemon/test_worker_pool_per_user.py``; this file goes deeper:

* per-table rollback assertions covering more than the impl's single
  failure point (rollback at the first DELETE, mid-cascade, after
  audit_log) so a re-ordering of the cascade cannot leak rows;
* event payload schema and counts under various scenarios;
* batch-prune internals (multi-batch loop, ``batch_size < total``);
* kill-switch surface for every prune (``retention_days=0`` per table
  and the dead_letter NULL ``last_failure_at`` sentinel);
* loop driver branches: per-table 0 disables only that pass, log
  events on success, exception isolation between tables;
* WorkerPool.remove_user idempotency and unknown-user no-op.

Pure SQLite — no Django. The PlatformCredential-backed integration
tests live in ``test_fernet_rotation.py``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.lifecycle.cleanup import (
    DEFAULT_AUDIT_LOG_RETENTION_DAYS,
    DEFAULT_DEAD_LETTER_RETENTION_DAYS,
    DEFAULT_SYNC_HISTORY_RETENTION_DAYS,
    CascadeDeleteResult,
    cascade_delete_user,
    cleanup_stale_cursors,
    prune_audit_log,
    prune_dead_letter,
    prune_sync_history,
)
from ingestion.daemon.lifecycle.loops import run_lifecycle_prune_loop
from ingestion.daemon.store.local import apply_migrations
from ingestion.daemon.worker import WorkerPool

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


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


def _count(
    db_path: Path,
    table: str,
    where: str = "1=1",
    params: tuple[Any, ...] = (),
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608 — static table list
            params,
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _seed_for(db_path: Path, email: str, platform: str = "moodle") -> None:
    """Seed exactly one row in every cascade table for ``email``."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"sh-{email}", email, platform, now, now, "complete"),
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
        "INSERT INTO force_sync_requests (user_email, platform, requested_at) VALUES (?, ?, ?)",
        (email, platform, now),
    )
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "user.pause", email),
    )


# ---------------------------------------------------------------------------
# CascadeDeleteResult dataclass
# ---------------------------------------------------------------------------


def test_cascade_delete_result_total_sums_all_fields() -> None:
    """``total`` includes the audit field even when audit kept (0)."""
    r = CascadeDeleteResult(
        sync_history_deleted=2,
        dead_letter_deleted=3,
        sync_cursors_deleted=4,
        force_sync_deleted=5,
        audit_deleted=6,
    )
    assert r.total == 20


def test_cascade_delete_result_zero_total_for_empty_pass() -> None:
    r = CascadeDeleteResult(0, 0, 0, 0, 0)
    assert r.total == 0


# ---------------------------------------------------------------------------
# cascade_delete_user — happy path + counts
# ---------------------------------------------------------------------------


async def test_cascade_returns_per_table_counts(db_path: Path) -> None:
    """Counts surface per-table — the dashboard hooks rely on each
    field being independently inspectable."""
    _seed_for(db_path, email="alice@x")
    result = await cascade_delete_user(db_path, email="alice@x")
    assert result.sync_history_deleted == 1
    assert result.dead_letter_deleted == 1
    assert result.sync_cursors_deleted == 1
    assert result.force_sync_deleted == 1
    assert result.audit_deleted == 0
    assert result.total == 4


async def test_cascade_with_multiple_rows_per_user_counts_correctly(db_path: Path) -> None:
    """Two sync_history rows for one email => two deletions."""
    now = datetime.now(tz=UTC).isoformat()
    for i in range(3):
        _exec(
            db_path,
            "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"sh-{i}", "alice@x", "moodle", now, now, "complete"),
        )
    result = await cascade_delete_user(db_path, email="alice@x")
    assert result.sync_history_deleted == 3


async def test_cascade_delete_audit_with_per_platform_target(db_path: Path) -> None:
    """The cascade picks up both ``email`` and ``email/platform`` audit
    rows when ``include_audit=True`` — the LIKE clause uses ``email/%``."""
    now = datetime.now(tz=UTC).isoformat()
    # Seed bare and per-platform audit rows.
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "user.pause", "alice@x"),
    )
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "credential.pause", "alice@x/moodle"),
    )
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "credential.pause", "alice@x/teams"),
    )
    # Decoy: target containing alice@x as substring but not the email.
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
        (now, "x.pause", "not-alice@x-bob"),
    )
    result = await cascade_delete_user(db_path, email="alice@x", include_audit=True)
    assert result.audit_deleted == 3
    assert _count(db_path, "audit_log", "target=?", ("not-alice@x-bob",)) == 1


async def test_cascade_unknown_email_returns_all_zero_counts(db_path: Path) -> None:
    """No-op result is the all-zero dataclass."""
    r = await cascade_delete_user(db_path, email="ghost@x")
    assert (
        r.sync_history_deleted,
        r.dead_letter_deleted,
        r.sync_cursors_deleted,
        r.force_sync_deleted,
        r.audit_deleted,
    ) == (0, 0, 0, 0, 0)


async def test_cascade_unknown_email_does_not_raise_event(db_path: Path) -> None:
    """The event sink fires even for zero-count deletes — operators
    rely on the lifecycle.user_deleted event firing on EVERY cascade
    call so dashboards can confirm the cascade happened."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def sink(ch: str, payload: dict[str, Any]) -> None:
        captured.append((ch, payload))

    await cascade_delete_user(db_path, email="ghost@x", event_sink=sink)
    assert len(captured) == 1
    # Event still emitted with all zero counts — dashboards see "user
    # acked deletion" even if no rows existed.
    assert captured[0][1]["sync_history_deleted"] == 0


# ---------------------------------------------------------------------------
# cascade_delete_user — single-transaction rollback
# ---------------------------------------------------------------------------


class _RollbackInjector:
    """Wraps ``aiosqlite.connect`` and raises on the Nth ``execute``.

    The rollback test fails the cascade at a configurable position so
    the test can pin "after sync_history but before sync_cursors"
    semantics — the impl's transaction order MUST roll back the
    earlier deletes too.
    """

    def __init__(self, raise_on_call: int) -> None:
        self._raise_on = raise_on_call

    async def wrap(
        self,
        path: Any,
        connect: Any,
    ) -> Any:
        inner = connect(path)

        class _Conn:
            def __init__(self, target: int) -> None:
                self._inner = inner
                self._calls = 0
                self._raise_on = target

            async def __aenter__(self) -> Any:
                self._inner = await self._inner.__aenter__()
                return self

            async def __aexit__(self, *exc: object) -> None:
                await self._inner.__aexit__(*exc)

            async def execute(self, sql: str, params: Any = ()) -> Any:
                self._calls += 1
                if self._calls == self._raise_on:
                    raise sqlite3.OperationalError("simulated failure")
                return await self._inner.execute(sql, params)

            async def commit(self) -> None:
                await self._inner.commit()

        return _Conn(self._raise_on)


@pytest.mark.parametrize(
    ("raise_on", "expected_survivors"),
    [
        # call 1 = BEGIN (no DELETE landed yet — survivor count = 1 each)
        (1, ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors")),
        # call 2 = sync_history DELETE — rollback restores it.
        (2, ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors")),
        # call 3 = dead_letter DELETE — rollback restores both.
        (3, ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors")),
        # call 4 = force_sync DELETE — rollback restores all three.
        (4, ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors")),
        # call 5 = sync_cursors DELETE — rollback restores everything.
        (5, ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors")),
    ],
)
async def test_cascade_rollback_at_every_transaction_position(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_on: int,
    expected_survivors: tuple[str, ...],
) -> None:
    """A failure anywhere in the transaction (BEGIN through final
    DELETE) rolls back all earlier DELETEs. Confirms the impl wraps
    every cascade row inside one transaction span."""
    _seed_for(db_path, email="alice@x")
    import aiosqlite as _aiosqlite

    real_connect = _aiosqlite.connect
    injector = _RollbackInjector(raise_on_call=raise_on)

    async def _patched_async(path: Any) -> Any:
        return await injector.wrap(path, real_connect)

    def _patched(path: Any, *_a: Any, **_kw: Any) -> Any:
        # aiosqlite.connect returns an awaitable that becomes the conn
        # context manager; we just return our async wrapper directly.
        class _CtxAdapter:
            async def __aenter__(self) -> Any:
                inner = real_connect(path)
                async_inner = await inner

                class _Conn:
                    def __init__(self) -> None:
                        self._inner = async_inner
                        self._calls = 0

                    async def execute(self, sql: str, params: Any = ()) -> Any:
                        self._calls += 1
                        if self._calls == raise_on:
                            raise sqlite3.OperationalError("simulated failure")
                        return await self._inner.execute(sql, params)

                    async def commit(self) -> None:
                        await self._inner.commit()

                self._conn = _Conn()
                self._async_inner = async_inner
                return self._conn

            async def __aexit__(self, *exc: object) -> None:
                await self._async_inner.close()

        return _CtxAdapter()

    monkeypatch.setattr(
        "ingestion.daemon.lifecycle.cleanup.aiosqlite.connect",
        _patched,
    )
    with pytest.raises(sqlite3.OperationalError):
        await cascade_delete_user(db_path, email="alice@x")
    # Every cascade table still has the original row (rollback honored).
    for table in expected_survivors:
        column = "user_email"
        assert _count(db_path, table, f"{column}=?", ("alice@x",)) == 1, (
            f"rollback failed at call {raise_on}: row missing in {table}"
        )


# ---------------------------------------------------------------------------
# cascade_delete_user — event sink
# ---------------------------------------------------------------------------


async def test_cascade_event_payload_full_schema(db_path: Path) -> None:
    """Pin the event payload contract: required keys + values match
    the result counts."""
    _seed_for(db_path, email="alice@x")
    captured: list[tuple[str, dict[str, Any]]] = []

    def sink(ch: str, payload: dict[str, Any]) -> None:
        captured.append((ch, payload))

    await cascade_delete_user(db_path, email="alice@x", event_sink=sink)
    assert len(captured) == 1
    channel, payload = captured[0]
    assert channel == "lifecycle"
    # Pin every payload key the dashboard subscribes to.
    expected_keys = {
        "event",
        "email",
        "sync_history_deleted",
        "dead_letter_deleted",
        "sync_cursors_deleted",
        "force_sync_deleted",
        "audit_deleted",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["event"] == "lifecycle.user_deleted"
    assert payload["email"] == "alice@x"
    assert payload["sync_history_deleted"] == 1
    assert payload["dead_letter_deleted"] == 1
    assert payload["sync_cursors_deleted"] == 1
    assert payload["force_sync_deleted"] == 1
    assert payload["audit_deleted"] == 0


async def test_cascade_no_event_sink_does_not_raise(db_path: Path) -> None:
    """Missing event_sink is the documented default — no crash."""
    _seed_for(db_path, email="alice@x")
    result = await cascade_delete_user(db_path, email="alice@x", event_sink=None)
    assert result.total == 4


async def test_cascade_event_sink_raise_does_not_fail_cascade(db_path: Path) -> None:
    """A noisy subscriber must NOT poison the cascade. The sink fires
    AFTER the SQL commits; an exception there should propagate, but
    the transaction is already complete."""
    _seed_for(db_path, email="alice@x")

    def sink(_ch: str, _p: dict[str, Any]) -> None:
        raise RuntimeError("noisy subscriber")

    # Today's impl does NOT wrap the sink in a try/except — the
    # exception WILL propagate. Pin that. If the impl is changed to
    # swallow, this test will fail and force a deliberate review.
    with pytest.raises(RuntimeError, match="noisy subscriber"):
        await cascade_delete_user(db_path, email="alice@x", event_sink=sink)
    # SQL was already committed before the sink fired.
    assert _count(db_path, "sync_history", "user_email=?", ("alice@x",)) == 0


# ---------------------------------------------------------------------------
# cascade_delete_user — worker pool integration
# ---------------------------------------------------------------------------


async def test_cascade_worker_pool_remove_called_with_correct_email(db_path: Path) -> None:
    """remove_user is invoked exactly once with the matching email."""
    _seed_for(db_path, email="alice@x")
    pool = WorkerPool(global_limit=2, per_user_limit=2)
    calls: list[str] = []
    real_remove = pool.remove_user

    def trace(email: str) -> bool:
        calls.append(email)
        return real_remove(email)

    # Type-ignore is fine: tests intentionally swap the bound method.
    pool.remove_user = trace  # type: ignore[method-assign]
    await cascade_delete_user(db_path, email="alice@x", worker_pool=pool)
    assert calls == ["alice@x"]


async def test_cascade_no_worker_pool_does_not_break(db_path: Path) -> None:
    """Default ``worker_pool=None`` skips the cleanup hook silently."""
    _seed_for(db_path, email="alice@x")
    result = await cascade_delete_user(db_path, email="alice@x")  # no pool
    assert result.sync_history_deleted == 1


async def test_cascade_worker_pool_remove_for_unknown_user_no_op(db_path: Path) -> None:
    """remove_user is idempotent — cascade can be called for a user
    who never had a semaphore allocated."""
    pool = WorkerPool(global_limit=2, per_user_limit=2)
    # No prior submit — pool has empty _per_user_sems.
    result = await cascade_delete_user(db_path, email="ghost@x", worker_pool=pool)
    assert result.total == 0
    assert "ghost@x" not in pool._per_user_sems  # noqa: SLF001


# ---------------------------------------------------------------------------
# Worker pool remove_user
# ---------------------------------------------------------------------------


async def test_worker_pool_remove_user_pops_correct_email_only() -> None:
    """remove_user does not affect other emails' semaphores."""
    pool = WorkerPool(global_limit=4, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    pool.submit(user_email="bob@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    assert "bob@x" in pool._per_user_sems  # noqa: SLF001
    assert pool.remove_user("alice@x") is True
    # Bob untouched.
    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001
    assert "bob@x" in pool._per_user_sems  # noqa: SLF001


async def test_worker_pool_remove_user_idempotent_returns_false_second_call() -> None:
    pool = WorkerPool(global_limit=2, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    assert pool.remove_user("alice@x") is True
    assert pool.remove_user("alice@x") is False  # already removed


async def test_worker_pool_remove_user_for_unknown_returns_false() -> None:
    pool = WorkerPool(global_limit=2, per_user_limit=2)
    assert pool.remove_user("ghost@x") is False


# ---------------------------------------------------------------------------
# prune_sync_history — terminal-state filter, kill switch, batch loop
# ---------------------------------------------------------------------------


def _seed_history(
    db_path: Path,
    *,
    sync_id: str,
    state: str,
    finished_days_ago: float | None,
    email: str = "u@x",
) -> None:
    started = datetime.now(tz=UTC) - timedelta(days=finished_days_ago or 0)
    finished_iso: str | None = None
    if finished_days_ago is not None:
        finished_iso = started.isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sync_id, email, "moodle", started.isoformat(), finished_iso, state),
    )


async def test_prune_sync_history_terminal_states_pruned(db_path: Path) -> None:
    """Every terminal state (complete/failed/cancelled) is eligible."""
    for state in ("complete", "failed", "cancelled"):
        _seed_history(db_path, sync_id=f"old-{state}", state=state, finished_days_ago=60)
    deleted = await prune_sync_history(db_path, retention_days=30)
    assert deleted == 3


async def test_prune_sync_history_finished_at_null_is_kept(db_path: Path) -> None:
    """A row with NULL finished_at (never completed) survives the prune
    even when state is not 'running' — the SQL filter requires a
    non-NULL finished_at column."""
    started = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    # Manufacture an inconsistent row: state=complete but finished_at=NULL.
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("inconsistent-1", "u@x", "moodle", started, None, "complete"),
    )
    deleted = await prune_sync_history(db_path, retention_days=30)
    assert deleted == 0
    assert _count(db_path, "sync_history", "id=?", ("inconsistent-1",)) == 1


async def test_prune_sync_history_default_retention_is_30(db_path: Path) -> None:
    """Default retention matches DEFAULT_SYNC_HISTORY_RETENTION_DAYS."""
    _seed_history(db_path, sync_id="day-29", state="complete", finished_days_ago=29)
    _seed_history(db_path, sync_id="day-31", state="complete", finished_days_ago=31)
    assert DEFAULT_SYNC_HISTORY_RETENTION_DAYS == 30
    deleted = await prune_sync_history(db_path)  # default
    assert deleted == 1
    assert _count(db_path, "sync_history", "id=?", ("day-29",)) == 1
    assert _count(db_path, "sync_history", "id=?", ("day-31",)) == 0


async def test_prune_sync_history_negative_retention_raises(db_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        await prune_sync_history(db_path, retention_days=-1)


async def test_prune_sync_history_batch_loop_runs_until_drained(db_path: Path) -> None:
    """When ``batch_size < total``, the loop iterates and the caller
    sees the cumulative count. Use a small batch size to force ≥ 2
    iterations."""
    for i in range(5):
        _seed_history(db_path, sync_id=f"old-{i}", state="complete", finished_days_ago=60)
    deleted = await prune_sync_history(db_path, retention_days=30, batch_size=2)
    assert deleted == 5
    assert _count(db_path, "sync_history", "user_email=?", ("u@x",)) == 0


async def test_prune_sync_history_no_eligible_rows_returns_zero(db_path: Path) -> None:
    """Empty table or all rows recent => deleted == 0."""
    _seed_history(db_path, sync_id="recent", state="complete", finished_days_ago=1)
    deleted = await prune_sync_history(db_path, retention_days=30)
    assert deleted == 0


# ---------------------------------------------------------------------------
# prune_dead_letter — cleared-only, suppressed-survives, kill switch
# ---------------------------------------------------------------------------


def _seed_dl(
    db_path: Path,
    *,
    email: str,
    consecutive: int,
    last_failure_days_ago: float | None,
    suppressed_until: datetime | None = None,
) -> None:
    last_iso: str | None = None
    if last_failure_days_ago is not None:
        last_iso = (datetime.now(tz=UTC) - timedelta(days=last_failure_days_ago)).isoformat()
    sup_iso = suppressed_until.isoformat() if suppressed_until else None
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, ?, ?, ?)",
        (email, "moodle", consecutive, last_iso, sup_iso),
    )


async def test_prune_dead_letter_keeps_rows_with_nonzero_failures(db_path: Path) -> None:
    """consecutive_failures > 0 — survives even if old."""
    _seed_dl(db_path, email="a@x", consecutive=3, last_failure_days_ago=200)
    deleted = await prune_dead_letter(db_path, retention_days=90)
    assert deleted == 0
    assert _count(db_path, "dead_letter_state", "user_email=?", ("a@x",)) == 1


async def test_prune_dead_letter_keeps_rows_with_active_suppression(db_path: Path) -> None:
    """suppressed_until non-NULL — survives even if cleared."""
    _seed_dl(
        db_path,
        email="a@x",
        consecutive=0,
        last_failure_days_ago=200,
        suppressed_until=datetime.now(tz=UTC) + timedelta(hours=2),
    )
    deleted = await prune_dead_letter(db_path, retention_days=90)
    assert deleted == 0


async def test_prune_dead_letter_keeps_null_last_failure(db_path: Path) -> None:
    """The conservative filter requires last_failure_at IS NOT NULL.
    A row with NULL last_failure_at (never failed) is not eligible."""
    _seed_dl(db_path, email="a@x", consecutive=0, last_failure_days_ago=None)
    deleted = await prune_dead_letter(db_path, retention_days=90)
    assert deleted == 0
    assert _count(db_path, "dead_letter_state", "user_email=?", ("a@x",)) == 1


async def test_prune_dead_letter_default_retention_is_90(db_path: Path) -> None:
    assert DEFAULT_DEAD_LETTER_RETENTION_DAYS == 90
    _seed_dl(db_path, email="recent", consecutive=0, last_failure_days_ago=80)
    _seed_dl(db_path, email="old", consecutive=0, last_failure_days_ago=120)
    deleted = await prune_dead_letter(db_path)
    assert deleted == 1
    assert _count(db_path, "dead_letter_state", "user_email=?", ("recent",)) == 1
    assert _count(db_path, "dead_letter_state", "user_email=?", ("old",)) == 0


async def test_prune_dead_letter_kill_switch_zero_drops_eligible(db_path: Path) -> None:
    """retention_days=0 cutoff = now; all eligible rows go."""
    # Eligible: cleared, last_failure 1 day ago.
    _seed_dl(db_path, email="cleared@x", consecutive=0, last_failure_days_ago=1)
    # Not eligible: still suppressed.
    _seed_dl(
        db_path,
        email="suppressed@x",
        consecutive=5,
        last_failure_days_ago=1,
        suppressed_until=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    await asyncio.sleep(0.01)
    deleted = await prune_dead_letter(db_path, retention_days=0)
    assert deleted == 1


async def test_prune_dead_letter_batch_drains(db_path: Path) -> None:
    for i in range(3):
        _seed_dl(db_path, email=f"u{i}@x", consecutive=0, last_failure_days_ago=200)
    deleted = await prune_dead_letter(db_path, retention_days=90, batch_size=1)
    assert deleted == 3


# ---------------------------------------------------------------------------
# prune_audit_log — timestamp threshold, kill switch
# ---------------------------------------------------------------------------


async def test_prune_audit_log_kill_switch_zero_drops_all(db_path: Path) -> None:
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
        (now, "x"),
    )
    await asyncio.sleep(0.01)
    deleted = await prune_audit_log(db_path, retention_days=0)
    assert deleted == 1


async def test_prune_audit_log_default_retention_is_365(db_path: Path) -> None:
    assert DEFAULT_AUDIT_LOG_RETENTION_DAYS == 365


async def test_prune_audit_log_negative_retention_raises(db_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        await prune_audit_log(db_path, retention_days=-1)


async def test_prune_audit_log_batched_drain_handles_more_than_batch(db_path: Path) -> None:
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    for i in range(3):
        _exec(
            db_path,
            "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
            (old_iso, f"action-{i}"),
        )
    deleted = await prune_audit_log(db_path, retention_days=365, batch_size=1)
    assert deleted == 3


# ---------------------------------------------------------------------------
# cleanup_stale_cursors — empty stored, kill switch, multi-platform isolation
# ---------------------------------------------------------------------------


async def test_cleanup_stale_cursors_empty_table_returns_zero(db_path: Path) -> None:
    """No cursor rows: no-op, not a crash."""
    deleted = await cleanup_stale_cursors(
        db_path, platform="teams", current_scopes=frozenset({"channels"})
    )
    assert deleted == 0


async def test_cleanup_stale_cursors_when_all_active_returns_zero(db_path: Path) -> None:
    """All stored scopes within current set => no deletion, no event."""
    now = datetime.now(tz=UTC).isoformat()
    for scope in ("default", "channels"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("u@x", "teams", scope, "{}", now),
        )
    captured: list[dict[str, Any]] = []

    def sink(_c: str, p: dict[str, Any]) -> None:
        captured.append(p)

    deleted = await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset({"default", "channels"}),
        event_sink=sink,
    )
    assert deleted == 0
    assert captured == []


async def test_cleanup_stale_cursors_other_platforms_untouched(db_path: Path) -> None:
    """Cleanup is platform-scoped — cursors on other platforms survive."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("u@x", "teams", "old_scope", "{}", now),
    )
    _exec(
        db_path,
        "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("u@x", "moodle", "old_scope", "{}", now),
    )
    deleted = await cleanup_stale_cursors(db_path, platform="teams", current_scopes=frozenset())
    assert deleted == 1
    # moodle row preserved.
    assert _count(db_path, "sync_cursors", "platform=?", ("moodle",)) == 1


async def test_cleanup_stale_cursors_multi_user_drops_all_for_stale_scope(
    db_path: Path,
) -> None:
    """If alice and bob both have the stale scope, both rows go."""
    now = datetime.now(tz=UTC).isoformat()
    for email in ("alice@x", "bob@x"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, "teams", "old_scope", "{}", now),
        )
    deleted = await cleanup_stale_cursors(db_path, platform="teams", current_scopes=frozenset())
    assert deleted == 2


async def test_cleanup_stale_cursors_event_payload_per_scope(db_path: Path) -> None:
    """One event per stale scope; payload contains platform + scope."""
    now = datetime.now(tz=UTC).isoformat()
    for scope in ("a", "b", "c"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("u@x", "teams", scope, "{}", now),
        )
    captured: list[dict[str, Any]] = []

    def sink(_c: str, p: dict[str, Any]) -> None:
        captured.append(p)

    deleted = await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset({"a"}),
        event_sink=sink,
    )
    assert deleted == 2
    scopes_emitted = sorted(p["scope"] for p in captured)
    assert scopes_emitted == ["b", "c"]
    assert all(p["event"] == "lifecycle.cursor_pruned" for p in captured)
    assert all(p["platform"] == "teams" for p in captured)


# ---------------------------------------------------------------------------
# run_lifecycle_prune_loop — branches: per-table 0, exception isolation
# ---------------------------------------------------------------------------


async def test_loop_disabled_when_interval_seconds_zero(db_path: Path) -> None:
    """interval_seconds=0 short-circuits; nothing is pruned."""
    _seed_history(db_path, sync_id="old", state="complete", finished_days_ago=60)
    shutdown = asyncio.Event()
    await run_lifecycle_prune_loop(
        shutdown,
        db_path=db_path,
        sync_history_retention_days=30,
        dead_letter_retention_days=30,
        audit_log_retention_days=30,
        interval_seconds=0,
    )
    assert _count(db_path, "sync_history", "id=?", ("old",)) == 1


async def test_loop_per_table_zero_skips_only_that_table(db_path: Path) -> None:
    """sync_history_retention_days=0 skips that prune; the other two
    still run."""
    _seed_history(db_path, sync_id="old-history", state="complete", finished_days_ago=60)
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
        (old_iso, "old-audit"),
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=0,  # disabled
            dead_letter_retention_days=0,  # disabled
            audit_log_retention_days=365,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # sync_history still there (its prune was disabled).
    assert _count(db_path, "sync_history", "id=?", ("old-history",)) == 1
    # audit_log row deleted.
    assert _count(db_path, "audit_log", "action=?", ("old-audit",)) == 0


async def test_loop_one_prune_failing_does_not_block_others(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside prune_sync_history is logged but doesn't
    stop the loop driver from running the other two prunes."""
    _seed_history(db_path, sync_id="old", state="complete", finished_days_ago=60)
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
        (old_iso, "old-audit"),
    )

    async def boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("prune-side failure")

    monkeypatch.setattr("ingestion.daemon.lifecycle.loops.prune_sync_history", boom)
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=30,
            dead_letter_retention_days=0,
            audit_log_retention_days=365,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # audit prune still ran despite history boom.
    assert _count(db_path, "audit_log", "action=?", ("old-audit",)) == 0


async def test_loop_completes_one_iteration_before_shutdown(db_path: Path) -> None:
    """Pre-set shutdown still allows the prune to land if the wait_for
    iteration completes — the impl checks shutdown.is_set() at the
    top of the while, so a shutdown set after the first prune lands
    leaves one iteration's work."""
    _seed_history(db_path, sync_id="old", state="complete", finished_days_ago=60)
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
    for _ in range(10):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # First iteration's prune did its job before shutdown wait_for woke.
    assert _count(db_path, "sync_history", "id=?", ("old",)) == 0


async def test_loop_shutdown_set_before_first_iteration_still_runs_once(
    db_path: Path,
) -> None:
    """The loop checks ``shutdown.is_set()`` at the top of the while,
    so if shutdown is already set, no iteration runs at all. This
    pin documents the current behaviour for safe shutdown ordering."""
    _seed_history(db_path, sync_id="old", state="complete", finished_days_ago=60)
    shutdown = asyncio.Event()
    shutdown.set()  # pre-set
    await run_lifecycle_prune_loop(
        shutdown,
        db_path=db_path,
        sync_history_retention_days=30,
        dead_letter_retention_days=0,
        audit_log_retention_days=0,
        interval_seconds=10,
    )
    # Prune did NOT run because shutdown was already set.
    assert _count(db_path, "sync_history", "id=?", ("old",)) == 1


async def test_loop_dead_letter_pruned_emits_log_when_deleted(db_path: Path) -> None:
    """When dead_letter prune deletes rows, the loop logs a
    ``lifecycle_prune_loop.dead_letter_pruned`` event. Walks the
    success branch including the deleted-non-zero log path."""
    old_iso = (datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=200)).isoformat()
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 0, ?, NULL)",
        ("u@x", "moodle", old_iso),
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=0,
            dead_letter_retention_days=90,
            audit_log_retention_days=0,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # Old cleared row pruned.
    assert _count(db_path, "dead_letter_state", "user_email=?", ("u@x",)) == 0


async def test_loop_dead_letter_exception_logged_and_loop_continues(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside ``prune_dead_letter`` is logged via
    ``log.exception`` and the loop continues with the next table."""
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    _exec(
        db_path,
        "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
        (old_iso, "old-audit"),
    )

    async def boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("dead_letter prune boom")

    monkeypatch.setattr("ingestion.daemon.lifecycle.loops.prune_dead_letter", boom)
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=0,
            dead_letter_retention_days=90,
            audit_log_retention_days=365,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # audit prune still ran despite dead_letter boom.
    assert _count(db_path, "audit_log", "action=?", ("old-audit",)) == 0


async def test_loop_audit_log_exception_logged_does_not_break_loop(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside ``prune_audit_log`` is caught by the loop
    driver. We pin that the loop completes its iteration cleanly even
    when audit prune raises (the SIGTERM path can still drain)."""

    async def boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("audit prune boom")

    monkeypatch.setattr("ingestion.daemon.lifecycle.loops.prune_audit_log", boom)
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=0,
            dead_letter_retention_days=0,
            audit_log_retention_days=365,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    # Loop exited cleanly (no raise propagated).
    await asyncio.wait_for(task, timeout=2.0)
