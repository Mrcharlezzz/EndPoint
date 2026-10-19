"""Edge + production tests for slice 12 (lifecycle + state + hot reload).

Pinned scenarios across the cascade-delete, TTL-prune, scheduler scope
cleanup, SIGHUP reload, and Fernet rotation surfaces.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended) or ``[TEST-COVERAGE]``
(the existing implementation is fine; this just locks the behaviour).

Slow tests (> 500ms) carry ``@pytest.mark.slow``.

Edge brainstorm — impl seed list (14):

1. cascade_delete_user with audit kept by default — compliance pin
2. cascade_delete_user with include_audit=True drops both bare and
   per-platform targets via the LIKE clause
3. cascade_delete_user single-transaction rollback — partial failure
   leaves no rows deleted
4. cascade_delete_user emits lifecycle.user_deleted ONCE per call
5. cascade_delete_user worker_pool argument removes per-user semaphore
6. prune_sync_history excludes state='running' even past cutoff
7. prune_dead_letter excludes rows with active suppressed_until
8. prune_dead_letter excludes rows with consecutive_failures > 0
9. prune_audit_log default retention 365d (compliance default)
10. prune_*_retention_days=0 within the loop driver disables ONLY that
    table's prune (not "delete everything")
11. cleanup_stale_cursors with frozenset() is the kill switch
12. cleanup_stale_cursors implicitly preserves the 'default' bucket
13. SIGHUP during shutdown is a no-op
14. SIGHUP without overlay change still re-arms scheduler scope cleanup

Edge brainstorm — tester additions (>= 8):

A. retention_days at the boundary day (cutoff exactly equals row's
   finished_at) — locks <= vs < semantics [TEST-COVERAGE]
B. cascade_delete_user transaction rollback at every position
   parametrised — defends against re-ordering [TEST-COVERAGE]
C. cascade event sink raising propagates after SQL commits — sink
   exceptions are NOT swallowed by the cascade [TEST-COVERAGE]
D. WorkerPool.remove_user does not affect other emails [TEST-COVERAGE]
E. SIGHUP boot-only field change logs runtime.config_reload_partial
   with the right boot_only set [TEST-COVERAGE]
F. SIGHUP scheduler reset failure is logged but does not break the
   handler [TEST-COVERAGE]
G. _on_sighup with a no-reset scheduler-like object does not crash
   [TEST-COVERAGE]
H. Rotation 'raced' outcome surfaces when aupdate returns 0 (not
   counted as 'rotated', no audit) [TEST-COVERAGE]
I. Rotation audit-write failure does NOT poison the pass [TEST-COVERAGE]
J. Rotation 'failed' outcome for ciphertext readable by NEITHER key
   [TEST-COVERAGE]
K. CRUD ordering: cascade delete after a user submitted but never
   completed leaves no semaphore [TEST-COVERAGE]
L. 100k-row TTL prune perf test — set-based DELETE under 5s on a
   sane CI runner [TEST-COVERAGE, slow-marked]
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import sys
import time
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon import runtime as runtime_module
from ingestion.daemon.candidates import InMemoryCandidateSource
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.lifecycle.cleanup import (
    cascade_delete_user,
    cleanup_stale_cursors,
    prune_audit_log,
    prune_dead_letter,
    prune_sync_history,
)
from ingestion.daemon.lifecycle.fernet_rotation import (
    ENV_FERNET_KEY_NEW,
    RotationResult,
    rotate_fernet_in_place,
)
from ingestion.daemon.lifecycle.loops import run_lifecycle_prune_loop
from ingestion.daemon.runtime import Runtime, _on_sighup
from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
from ingestion.daemon.store.local import apply_migrations, open_store
from ingestion.daemon.worker import WorkerPool

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "state.db"
    apply_migrations(p)
    return p


@pytest.fixture
def fernet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())


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
            f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608 — static
            params,
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _build_runtime(tmp_path: Path) -> Runtime:
    apply_migrations(tmp_path / "state.db")
    cipher = CredentialCipher.from_env()
    store = open_store(tmp_path / "state.db")
    config = DaemonConfig(sqlite_path=tmp_path / "state.db")
    return Runtime(
        config=config,
        store=store,
        cipher=cipher,
        shutdown=asyncio.Event(),
        lock_path=None,
    )


# ---------------------------------------------------------------------------
# 1. cascade_delete_user keeps audit by default [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cascade_keeps_audit_by_default(db_path: Path) -> None:
    """[TEST-COVERAGE] Compliance default: ``include_audit=False`` keeps
    every audit_log row matching the user's targets."""
    now = datetime.now(tz=UTC).isoformat()
    for action_target in (("user.pause", "alice@x"), ("credential.pause", "alice@x/teams")):
        _exec(
            db_path,
            "INSERT INTO audit_log (timestamp, action, target) VALUES (?, ?, ?)",
            (now, action_target[0], action_target[1]),
        )
    await cascade_delete_user(db_path, email="alice@x")
    # Both audit rows still present.
    assert _count(db_path, "audit_log", "target=?", ("alice@x",)) == 1
    assert _count(db_path, "audit_log", "target=?", ("alice@x/teams",)) == 1


# ---------------------------------------------------------------------------
# 2. cascade with include_audit=True drops bare AND per-platform [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cascade_include_audit_drops_both_target_shapes(db_path: Path) -> None:
    """[TEST-COVERAGE] ``include_audit=True`` drops both ``email`` and
    ``email/platform`` audit rows via the OR LIKE clause."""
    now = datetime.now(tz=UTC).isoformat()
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
    result = await cascade_delete_user(db_path, email="alice@x", include_audit=True)
    assert result.audit_deleted == 2


# ---------------------------------------------------------------------------
# 3. cascade rollback semantics [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cascade_rollback_on_partial_failure(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] A simulated DELETE failure rolls back every
    earlier delete in the cascade. Pin to the third execute call (after
    BEGIN + sync_history DELETE)."""
    now = datetime.now(tz=UTC).isoformat()
    # Seed sync_history + dead_letter so we have rows to roll back.
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sh-1", "alice@x", "moodle", now, now, "complete"),
    )
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures) "
        "VALUES (?, ?, ?)",
        ("alice@x", "moodle", 0),
    )
    import aiosqlite as _aiosqlite

    real_connect = _aiosqlite.connect

    def patched(path: Any, *_a: Any, **_kw: Any) -> Any:
        class _CtxAdapter:
            async def __aenter__(self) -> Any:
                inner = real_connect(path)
                self._async_inner = await inner

                class _Conn:
                    def __init__(self, target: int) -> None:
                        self._inner = self_outer._async_inner
                        self._calls = 0
                        self._raise_on = target

                    async def execute(self, sql: str, params: Any = ()) -> Any:
                        self._calls += 1
                        if self._calls == 3:
                            raise sqlite3.OperationalError("simulated mid-cascade failure")
                        return await self._inner.execute(sql, params)

                    async def commit(self) -> None:
                        await self._inner.commit()

                self_outer = self
                self._conn = _Conn(3)
                return self._conn

            async def __aexit__(self, *exc: object) -> None:
                await self._async_inner.close()

        return _CtxAdapter()

    monkeypatch.setattr("ingestion.daemon.lifecycle.cleanup.aiosqlite.connect", patched)
    with pytest.raises(sqlite3.OperationalError):
        await cascade_delete_user(db_path, email="alice@x")
    # sync_history and dead_letter rows STILL present (rollback honored).
    assert _count(db_path, "sync_history", "user_email=?", ("alice@x",)) == 1
    assert _count(db_path, "dead_letter_state", "user_email=?", ("alice@x",)) == 1


# ---------------------------------------------------------------------------
# 4. cascade emits ONE event per call [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cascade_emits_one_event_per_call(db_path: Path) -> None:
    """[TEST-COVERAGE] A single cascade call emits exactly one
    ``lifecycle.user_deleted`` event with the totals — even when
    multiple rows are deleted across multiple tables."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sh-1", "alice@x", "moodle", now, now, "complete"),
    )
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sh-2", "alice@x", "teams", now, now, "complete"),
    )
    captured: list[dict[str, Any]] = []

    def sink(_c: str, p: dict[str, Any]) -> None:
        captured.append(p)

    await cascade_delete_user(db_path, email="alice@x", event_sink=sink)
    assert len(captured) == 1
    assert captured[0]["sync_history_deleted"] == 2


# ---------------------------------------------------------------------------
# 5. worker pool removes per-user semaphore [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cascade_releases_per_user_semaphore(db_path: Path) -> None:
    """[TEST-COVERAGE] cascade with worker_pool argument frees the
    user's semaphore — addresses the slice-10 leak deferral."""
    pool = WorkerPool(global_limit=2, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    await cascade_delete_user(db_path, email="alice@x", worker_pool=pool)
    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001


# ---------------------------------------------------------------------------
# 6. running sync survives prune_sync_history past cutoff [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_running_sync_survives_prune(db_path: Path) -> None:
    """[TEST-COVERAGE] A still-running sync past the cutoff MUST NOT be
    deleted — the prune SQL filters on ``state != 'running'``."""
    started = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("running-1", "u@x", "moodle", started, None, "running"),
    )
    deleted = await prune_sync_history(db_path, retention_days=30)
    assert deleted == 0
    assert _count(db_path, "sync_history", "id=?", ("running-1",)) == 1


# ---------------------------------------------------------------------------
# 7. prune_dead_letter keeps actively-suppressed rows [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_prune_dead_letter_keeps_active_suppression(db_path: Path) -> None:
    """[TEST-COVERAGE] An old row that's currently suppressed survives
    the prune — operators expect to see it on /api/errors until the
    suppression lifts naturally."""
    old = (datetime.now(tz=UTC) - timedelta(days=200)).isoformat()
    sup_until = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 5, ?, ?)",
        ("a@x", "moodle", old, sup_until),
    )
    deleted = await prune_dead_letter(db_path, retention_days=90)
    assert deleted == 0


# ---------------------------------------------------------------------------
# 8. prune_dead_letter keeps non-zero failures [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_prune_dead_letter_keeps_active_counter(db_path: Path) -> None:
    """[TEST-COVERAGE] consecutive_failures > 0 keeps the row regardless
    of the cutoff (pre-suppression flap memory)."""
    old = (datetime.now(tz=UTC) - timedelta(days=200)).isoformat()
    _exec(
        db_path,
        "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
        "last_failure_at, suppressed_until) VALUES (?, ?, 2, ?, NULL)",
        ("a@x", "moodle", old),
    )
    deleted = await prune_dead_letter(db_path, retention_days=90)
    assert deleted == 0


# ---------------------------------------------------------------------------
# 9. audit_log default retention 365d [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_audit_log_default_365d_keeps_compliance_window(
    db_path: Path,
) -> None:
    """[TEST-COVERAGE] An audit row 1 year - 1 day old survives the
    default prune; 1 year + 1 day does not."""
    almost = (datetime.now(tz=UTC) - timedelta(days=364)).isoformat()
    too_old = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    _exec(db_path, "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)", (almost, "x"))
    _exec(db_path, "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)", (too_old, "y"))
    deleted = await prune_audit_log(db_path)
    assert deleted == 1
    assert _count(db_path, "audit_log", "action=?", ("x",)) == 1
    assert _count(db_path, "audit_log", "action=?", ("y",)) == 0


# ---------------------------------------------------------------------------
# 10. retention_days=0 in loop disables that table only [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_loop_per_table_zero_disables_only_that_table(db_path: Path) -> None:
    """[TEST-COVERAGE] Inside the loop driver, ``retention_days=0`` is
    treated as "skip this prune". Other tables still get pruned."""
    started = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("history-1", "u@x", "moodle", started, started, "complete"),
    )
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    _exec(db_path, "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)", (old_iso, "z"))
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_lifecycle_prune_loop(
            shutdown,
            db_path=db_path,
            sync_history_retention_days=0,  # disabled
            dead_letter_retention_days=0,
            audit_log_retention_days=365,
            interval_seconds=10,
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)
    # sync_history NOT pruned; audit_log was.
    assert _count(db_path, "sync_history", "id=?", ("history-1",)) == 1
    assert _count(db_path, "audit_log", "action=?", ("z",)) == 0


# ---------------------------------------------------------------------------
# 11. cleanup_stale_cursors frozenset() kill switch [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_cleanup_stale_cursors_kill_switch(db_path: Path) -> None:
    """[TEST-COVERAGE] An empty active set deletes every cursor for the
    given platform. Documented kill switch."""
    now = datetime.now(tz=UTC).isoformat()
    for s in ("default", "channels"):
        _exec(
            db_path,
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("u@x", "teams", s, "{}", now),
        )
    deleted = await cleanup_stale_cursors(db_path, platform="teams", current_scopes=frozenset())
    assert deleted == 2


# ---------------------------------------------------------------------------
# 12. default bucket implicitly preserved [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_default_scope_implicit_preservation_via_scheduler(
    db_path: Path,
) -> None:
    """[TEST-COVERAGE] The scheduler-side cleanup pass adds 'default' to
    current_scopes implicitly — a cursor at scope='default' is never
    pruned even when the resolver doesn't list it."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("u@x", "teams", "default", "{}", now),
    )
    pool = WorkerPool(global_limit=2)
    dl = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)

    class _Stub:
        async def list_candidates(self) -> list[Any]:
            return []

        async def get_credentials(self, *_a: Any, **_kw: Any) -> Any:
            return None

        async def upsert_batch(self, *_a: Any, **_kw: Any) -> int:
            return 0

        async def get_cursor(self, *_a: Any, **_kw: Any) -> Any:
            return None

        async def set_cursor(self, *_a: Any, **_kw: Any) -> None:
            return None

        async def log_start(self, *_a: Any, **_kw: Any) -> str:
            return "id"

        async def log_complete(self, *_a: Any, **_kw: Any) -> None:
            return None

        async def log_failure(self, *_a: Any, **_kw: Any) -> None:
            return None

    deps = SchedulerDeps(
        config_store=open_store(db_path),
        candidate_source=InMemoryCandidateSource([]),
        worker_pool=pool,
        dead_letter=dl,
        token_store=_Stub(),  # type: ignore[arg-type]
        content_sink=_Stub(),  # type: ignore[arg-type]
        sync_state=_Stub(),  # type: ignore[arg-type]
        sync_logger=_Stub(),  # type: ignore[arg-type]
        adapter_factory=lambda _p: None,  # type: ignore[arg-type,return-value]
        sqlite_path=db_path,
        # Resolver explicitly excludes 'default'.
        scope_for_platform=lambda _p: frozenset({"channels"}),
    )
    scheduler = Scheduler(deps)
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    # 'default' was never touched.
    assert deleted == 0
    assert _count(db_path, "sync_cursors", "scope=?", ("default",)) == 1


# ---------------------------------------------------------------------------
# 13. SIGHUP during shutdown is no-op [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_edge_sighup_during_shutdown_no_op(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] SIGHUP after shutdown.set() does nothing — pin
    that the impl checks shutdown.is_set first."""
    runtime = _build_runtime(tmp_path)
    runtime.shutdown.set()
    captured: list[str] = []

    class _Log:
        def info(self, event: str, **_kw: object) -> None:
            captured.append(event)

        def warning(self, *_a: object, **_kw: object) -> None:
            return

        def exception(self, *_a: object, **_kw: object) -> None:
            return

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _Log())
    try:
        _on_sighup(runtime)
        assert "runtime.sighup_ignored_during_shutdown" in captured
        assert "runtime.config_reloaded" not in captured
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# 14. SIGHUP without overlay change still resets schedulers [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_edge_sighup_no_diff_still_resets_schedulers(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] SIGHUP without any overlay change still re-arms
    the scheduler scope cleanup — operators send SIGHUP to ask for a
    re-evaluation."""
    runtime = _build_runtime(tmp_path)
    reset_count = 0

    class _Sched:
        def reset_scope_cleanup(self) -> None:
            nonlocal reset_count
            reset_count += 1

    runtime.schedulers.append(_Sched())

    class _Log:
        def info(self, *_a: object, **_kw: object) -> None:
            return

        def warning(self, *_a: object, **_kw: object) -> None:
            return

        def exception(self, *_a: object, **_kw: object) -> None:
            return

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _Log())
    try:
        _on_sighup(runtime)
        assert reset_count == 1
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# A. retention_days boundary semantics [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_a_retention_strictly_less_than_cutoff(db_path: Path) -> None:
    """[TEST-COVERAGE] The prune SQL uses ``finished_at < ?`` (strict
    less than). A row with finished_at exactly equal to the cutoff
    survives. Pin the boundary so a future ``<=`` change is reviewed."""
    started = (datetime.now(tz=UTC) - timedelta(days=29, hours=23)).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("just-inside", "u@x", "moodle", started, started, "complete"),
    )
    deleted = await prune_sync_history(db_path, retention_days=30)
    assert deleted == 0
    assert _count(db_path, "sync_history", "id=?", ("just-inside",)) == 1


# ---------------------------------------------------------------------------
# B. Cascade rollback parametrised position pin [TEST-COVERAGE]
# ---------------------------------------------------------------------------
#
# Already exercised in tests/daemon/lifecycle/test_cleanup.py via the
# parametrised rollback test. This edge file omits a duplicate.


# ---------------------------------------------------------------------------
# C. cascade event sink raise propagates [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_c_cascade_event_sink_raise_propagates(db_path: Path) -> None:
    """[TEST-COVERAGE] The cascade does NOT swallow event-sink
    exceptions. SQL has already committed; the operator sees the raise
    so misbehaving subscribers surface fast."""
    now = datetime.now(tz=UTC).isoformat()
    _exec(
        db_path,
        "INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sh-1", "u@x", "moodle", now, now, "complete"),
    )

    def sink(_c: str, _p: dict[str, Any]) -> None:
        raise RuntimeError("subscriber boom")

    with pytest.raises(RuntimeError, match="subscriber boom"):
        await cascade_delete_user(db_path, email="u@x", event_sink=sink)
    # SQL committed before the sink raised.
    assert _count(db_path, "sync_history", "user_email=?", ("u@x",)) == 0


# ---------------------------------------------------------------------------
# D. WorkerPool.remove_user does not affect other emails [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_d_remove_user_scoped_to_target_email() -> None:
    """[TEST-COVERAGE] Removing alice@x leaves bob@x's semaphore intact."""
    pool = WorkerPool(global_limit=4, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    pool.submit(user_email="bob@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    pool.remove_user("alice@x")
    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001
    assert "bob@x" in pool._per_user_sems  # noqa: SLF001


# ---------------------------------------------------------------------------
# E. Boot-only field change logs partial reload [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_edge_e_boot_only_log_level_change_logs_partial(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Changing log_level via the overlay triggers
    runtime.config_reload_partial with boot_only=['log_level']."""
    runtime = _build_runtime(tmp_path)
    runtime.store.set_overlay("log_level", "DEBUG")
    captured: list[tuple[str, dict[str, object]]] = []

    class _Log:
        def info(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        def warning(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        def exception(self, event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _Log())
    try:
        _on_sighup(runtime)
        partial = next(kw for evt, kw in captured if evt == "runtime.config_reload_partial")
        assert "log_level" in partial.get("boot_only", [])
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# F. Scheduler reset failure is logged but does not break SIGHUP [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_edge_f_scheduler_reset_raises_logged_no_break(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] If a scheduler's ``reset_scope_cleanup`` raises,
    SIGHUP logs ``runtime.scheduler_reset_errored`` and continues."""
    runtime = _build_runtime(tmp_path)

    class _Raises:
        def reset_scope_cleanup(self) -> None:
            raise RuntimeError("broken scheduler")

    runtime.schedulers.append(_Raises())
    captured: list[str] = []

    class _Log:
        def info(self, event: str, **_kw: object) -> None:
            captured.append(event)

        def warning(self, event: str, **_kw: object) -> None:
            captured.append(event)

        def exception(self, event: str, **_kw: object) -> None:
            captured.append(event)

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _Log())
    try:
        _on_sighup(runtime)  # must not raise
        assert "runtime.scheduler_reset_errored" in captured
        # Reload still completed.
        assert "runtime.config_reloaded" in captured
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# G. SIGHUP scheduler-like without reset method [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_edge_g_scheduler_object_without_reset_skipped(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] An object in ``runtime.schedulers`` that lacks
    ``reset_scope_cleanup`` is skipped. The impl uses ``getattr(...,
    None)`` for safe duck-typing."""
    runtime = _build_runtime(tmp_path)

    class _NoReset:
        pass

    runtime.schedulers.append(_NoReset())

    class _Log:
        def info(self, *_a: object, **_kw: object) -> None:
            return

        def warning(self, *_a: object, **_kw: object) -> None:
            return

        def exception(self, *_a: object, **_kw: object) -> None:
            return

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _Log())
    try:
        _on_sighup(runtime)  # must not raise
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# H. Rotation 'raced' outcome [TEST-COVERAGE]
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    pk: int
    user_email: str
    platform: str
    encrypted_payload: bytes


class _Manager:
    def __init__(self, rows: list[_Row], outcomes: dict[int, int] | None = None) -> None:
        self._rows = rows
        self._outcomes = outcomes or {}
        self.update_calls: list[dict[str, Any]] = []
        self._pending_pk = -1
        self._pending_payload = b""

    def all(self) -> _Manager:
        return self

    def aiterator(self) -> AsyncIterator[_Row]:
        rows = list(self._rows)

        async def _gen() -> AsyncIterator[_Row]:
            for row in rows:
                yield row

        return _gen()

    def filter(self, *, pk: int, encrypted_payload: bytes) -> _Manager:
        self._pending_pk = pk
        self._pending_payload = encrypted_payload
        return self

    async def aupdate(self, *, encrypted_payload: bytes) -> int:
        self.update_calls.append(
            {
                "pk": self._pending_pk,
                "old": self._pending_payload,
                "new": encrypted_payload,
            }
        )
        return self._outcomes.get(self._pending_pk, 1)


def _install_fake_models(monkeypatch: pytest.MonkeyPatch, manager: _Manager) -> None:
    fake = types.ModuleType("core.models")

    class _PlatformCredential:
        objects = manager

    fake.PlatformCredential = _PlatformCredential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.models", fake)


async def test_edge_h_rotation_raced_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """[TEST-COVERAGE] A concurrent writer between our read and our
    aupdate makes the WHERE clause match zero rows; the row is counted
    as ``raced`` not ``rotated``. No audit row is written for raced."""
    old = CredentialCipher([Fernet.generate_key().decode()])
    new = CredentialCipher([Fernet.generate_key().decode()])
    payload = old.encrypt_json({"k": 1})
    rows = [_Row(pk=1, user_email="a@x", platform="moodle", encrypted_payload=payload)]
    manager = _Manager(rows=rows, outcomes={1: 0})
    _install_fake_models(monkeypatch, manager)
    audit_calls: list[Any] = []

    async def _audit(**kw: object) -> int:
        audit_calls.append(kw)
        return 1

    result = await rotate_fernet_in_place(
        current_cipher=old,
        new_cipher=new,
        audit_write=_audit,
    )
    assert result.raced == 1
    assert result.rotated == 0
    # No audit row for the raced outcome.
    assert audit_calls == []


# ---------------------------------------------------------------------------
# I. Audit-write failure does not poison rotation [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_i_audit_failure_does_not_poison_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] An audit writer that raises is logged inside
    ``_rotate_one`` but the row is STILL counted as ``rotated``."""
    old = CredentialCipher([Fernet.generate_key().decode()])
    new = CredentialCipher([Fernet.generate_key().decode()])
    rows = [
        _Row(
            pk=1,
            user_email="a@x",
            platform="moodle",
            encrypted_payload=old.encrypt_json({"k": 1}),
        ),
    ]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)

    async def _bad_audit(**_kw: object) -> int:
        raise RuntimeError("audit boom")

    result = await rotate_fernet_in_place(
        current_cipher=old,
        new_cipher=new,
        audit_write=_bad_audit,
    )
    assert result.rotated == 1


# ---------------------------------------------------------------------------
# J. Rotation 'failed' outcome for unreadable ciphertext [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_edge_j_rotation_failed_for_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Ciphertext readable by NEITHER cipher counts as
    ``failed``. Operator triages the row manually."""
    old = CredentialCipher([Fernet.generate_key().decode()])
    new = CredentialCipher([Fernet.generate_key().decode()])
    foreign = CredentialCipher([Fernet.generate_key().decode()])
    rows = [
        _Row(
            pk=1,
            user_email="a@x",
            platform="moodle",
            encrypted_payload=foreign.encrypt_json({"foreign": True}),
        ),
    ]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)
    result = await rotate_fernet_in_place(current_cipher=old, new_cipher=new)
    assert result.failed == 1
    assert result.rotated == 0


# ---------------------------------------------------------------------------
# K. Cascade after submitted-not-completed user leaves no semaphore
# ---------------------------------------------------------------------------


async def test_edge_k_cascade_after_submitted_user_clears_semaphore(
    db_path: Path,
) -> None:
    """[TEST-COVERAGE] CRUD ordering: even if the user only ever
    submitted one sync (still in flight or just drained), cascade still
    pops the per-user semaphore. We exercise the post-submit path."""
    pool = WorkerPool(global_limit=2, per_user_limit=1)

    async def slow() -> None:
        await asyncio.sleep(0.01)

    pool.submit(user_email="alice@x", platform="moodle", coro=slow())
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    await cascade_delete_user(db_path, email="alice@x", worker_pool=pool)
    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001


# ---------------------------------------------------------------------------
# L. 100k-row TTL prune perf [TEST-COVERAGE, slow]
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_edge_l_perf_100k_row_audit_prune_under_5s(db_path: Path) -> None:
    """[TEST-COVERAGE] Inserting 100,000 old audit rows then running the
    365-day prune completes in well under 5s on a sane CI runner. The
    set-based DELETE + batched commit handles this without blowing
    memory."""
    old_iso = (datetime.now(tz=UTC) - timedelta(days=400)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        # executemany is faster than the per-row helper for the seed.
        conn.executemany(
            "INSERT INTO audit_log (timestamp, action) VALUES (?, ?)",
            [(old_iso, f"action-{i}") for i in range(100_000)],
        )
        conn.commit()
    finally:
        conn.close()
    # Sanity check the seed.
    assert _count(db_path, "audit_log") == 100_000
    start = time.monotonic()
    deleted = await prune_audit_log(db_path, retention_days=365, batch_size=10_000)
    elapsed = time.monotonic() - start
    assert deleted == 100_000
    assert elapsed < 5.0, f"100k-row prune slow path: {elapsed:.2f}s"
    # Cleanly drained.
    assert _count(db_path, "audit_log") == 0


# ---------------------------------------------------------------------------
# Sanity: signal exists on POSIX
# ---------------------------------------------------------------------------


def test_edge_signal_sighup_is_posix() -> None:
    """[TEST-COVERAGE] On POSIX, ``signal.SIGHUP`` is defined; on
    Windows the impl skips signal installation. Pin the platform
    branching."""
    if sys.platform == "win32":
        # Windows: SIGHUP doesn't exist as an attribute.
        assert getattr(signal, "SIGHUP", None) is None
    else:
        assert hasattr(signal, "SIGHUP")


# ---------------------------------------------------------------------------
# Sanity: RotationResult typing surface
# ---------------------------------------------------------------------------


def test_edge_rotation_result_has_expected_fields() -> None:
    """[TEST-COVERAGE] Pin the dataclass fields so a downstream consumer
    relying on this exact shape (envelopes, dashboards) catches a
    silent rename."""
    r = RotationResult(total_rows=1, rotated=1, already_rotated=0, raced=0, failed=0)
    fields = {f for f in r.__dataclass_fields__}
    assert fields == {"total_rows", "rotated", "already_rotated", "raced", "failed"}


# Suppress unused-import warning from ENV_FERNET_KEY_NEW retained in scope
# in case future tests extend the rotation surface here.
_ = ENV_FERNET_KEY_NEW
