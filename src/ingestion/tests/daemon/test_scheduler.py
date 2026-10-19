"""Scheduler behaviors: kill switches, drain, emergency stop, dead-letter, overrides."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from ingestion.daemon.candidates import Candidate, InMemoryCandidateSource
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
from ingestion.daemon.schedule_queries import set_user_interval_override
from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
from ingestion.daemon.store.local import apply_migrations, open_store
from ingestion.daemon.worker import WorkerPool
from ingestion.schemas import FetchResult, NormalizedItem, SyncStats

from tests.daemon._scheduler_helpers import (
    FakeAdapter,
    InMemoryContentSink,
    InMemorySyncStateStore,
    InMemoryTokenStore,
    drive_event_loop,
)

if TYPE_CHECKING:
    from ingestion.ports import Adapter


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _build(
    sqlite_path: Path,
    candidates: list[Candidate],
    adapter: FakeAdapter,
    *,
    default_interval_seconds: int = 60,
    now: datetime | None = None,
    overlay_overrides: dict[str, object] | None = None,
    dead_letter_threshold: int = 3,
    dead_letter_cooldown: int = 60,
    global_limit: int = 4,
) -> tuple[
    Scheduler,
    WorkerPool,
    DeadLetterTracker,
    SqliteSyncLogger,
    InMemorySyncStateStore,
    InMemoryContentSink,
    InMemoryTokenStore,
]:
    config_store = open_store(sqlite_path)
    if overlay_overrides:
        for k, v in overlay_overrides.items():
            config_store.set_overlay(k, v)

    dead_letter = DeadLetterTracker(
        sqlite_path, threshold=dead_letter_threshold, cooldown_seconds=dead_letter_cooldown
    )
    token_store = InMemoryTokenStore(
        {(c.user_email, c.platform): {"token": "tok"} for c in candidates}
    )
    content_sink = InMemoryContentSink()
    sync_state = InMemorySyncStateStore()
    # Real sqlite-backed logger so the scheduler's last-success due-check
    # reads values the pipeline actually wrote. The fake loggers skipped this
    # link and obscured the scheduler-query coupling.
    sync_logger = SqliteSyncLogger(sqlite_path)

    async def on_finish(user: str, platform: str, exc: BaseException | None) -> None:
        if exc is None:
            await dead_letter.record_success(user, platform)
        else:
            await dead_letter.record_failure(user, platform)

    pool = WorkerPool(global_limit=global_limit, on_finish=on_finish)

    def _adapter_factory(_platform: str) -> Adapter:
        return cast("Adapter", adapter)

    # Inject default_interval_seconds by writing directly to the overlay.
    config_store.set_overlay("scheduler.default_interval_seconds", default_interval_seconds)

    deps = SchedulerDeps(
        config_store=config_store,
        candidate_source=InMemoryCandidateSource(candidates),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=cast("Adapter", token_store),  # type: ignore[arg-type]
        content_sink=cast("Adapter", content_sink),  # type: ignore[arg-type]
        sync_state=cast("Adapter", sync_state),  # type: ignore[arg-type]
        sync_logger=cast("Adapter", sync_logger),  # type: ignore[arg-type]
        adapter_factory=_adapter_factory,
        sqlite_path=sqlite_path,
        now=(lambda: now) if now is not None else (lambda: datetime.now(tz=UTC)),
    )
    return Scheduler(deps), pool, dead_letter, sync_logger, sync_state, content_sink, token_store


def _item(external_id: str, platform: str = "moodle") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform=platform,
        content_type="announcement",  # type: ignore[arg-type]
        body="test",
    )


async def test_tick_submits_all_eligible_candidates(sqlite_path: Path) -> None:
    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(
            items=[_item("x_1")], cursor={"ts": "v1"}, stats=SyncStats()
        )
    )
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle"), Candidate("bob@x", "moodle")],
        adapter,
    )

    submitted = await scheduler.tick_once()
    assert submitted == 2
    await pool.drain(timeout=1.0)


async def test_platform_kill_switch_stops_submission(sqlite_path: Path) -> None:
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"platforms.moodle_enabled": False},
    )
    assert await scheduler.tick_once() == 0
    assert pool.in_flight == 0


async def test_platform_pause_overlay_skips_submission(sqlite_path: Path) -> None:
    """``platforms.paused.<name>=True`` must skip every candidate on that
    platform without submitting. Distinct from the kill switch because the
    credential/routing state stays intact for a quick un-pause."""
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"platforms.paused.moodle": True},
    )
    assert await scheduler.tick_once() == 0
    assert pool.in_flight == 0


async def test_platform_pause_does_not_affect_other_platforms(sqlite_path: Path) -> None:
    """Pausing one platform must not block other platforms from running."""
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [
            Candidate("alice@x", "moodle"),
            Candidate("alice@x", "teams"),
        ],
        adapter,
        overlay_overrides={"platforms.paused.moodle": True},
    )
    # Only the teams candidate should submit; moodle is paused.
    submitted = await scheduler.tick_once()
    assert submitted == 1
    await pool.drain(timeout=1.0)


async def test_platform_pause_can_be_toggled_round_trip(sqlite_path: Path) -> None:
    """Overlay PUT/DELETE round-trip must flip the pause flag so the
    scheduler resumes submission on the next tick."""
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )
    store = scheduler._deps.config_store

    # No overlay row -> default False -> runs.
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)

    # Set pause: the next tick must not submit.
    store.set_overlay("platforms.paused.moodle", True)
    assert await scheduler.tick_once() == 0

    # Clear pause: scheduler must resume. Use a 0s interval so the user
    # is due again immediately after the first success.
    store.set_overlay("scheduler.default_interval_seconds", 0)
    store.delete_overlay("platforms.paused.moodle")
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)


async def test_forced_sync_respects_platform_pause(sqlite_path: Path) -> None:
    """A force_sync request must stay queued under an active pause so it
    fires as soon as the operator un-pauses, rather than being silently
    dropped (which is what happens under the kill-switch)."""
    import sqlite3

    from ingestion.daemon.force_sync import enqueue_force_sync

    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"platforms.paused.moodle": True},
        default_interval_seconds=86400,
    )

    await enqueue_force_sync(sqlite_path, user_email="alice@x", platform="moodle")
    # Pause is active: the forced sync must not submit, and the row must
    # remain in force_sync_requests for later.
    assert await scheduler.tick_once() == 0
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute("SELECT user_email FROM force_sync_requests").fetchall()
    finally:
        conn.close()
    assert rows == [("alice@x",)]


async def test_pause_skip_logs_once_per_platform(sqlite_path: Path) -> None:
    """``sync_paused`` must be emitted once per (user, platform) per
    contiguous pause window — not once per tick. Re-pause after un-pause
    should re-log."""
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"platforms.paused.moodle": True},
    )

    await scheduler.tick_once()
    await scheduler.tick_once()
    assert ("alice@x", "moodle") in scheduler._paused_skipped_logged
    assert len(scheduler._paused_skipped_logged) == 1

    # Un-pause: the tick clear cycle should drop the warn-once entry so a
    # later re-pause emits the log again.
    scheduler._deps.config_store.delete_overlay("platforms.paused.moodle")
    await scheduler.tick_once()
    assert scheduler._paused_skipped_logged == set()


async def test_paused_user_is_skipped(sqlite_path: Path) -> None:
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle", is_paused=True)],
        adapter,
    )
    assert await scheduler.tick_once() == 0
    assert pool.in_flight == 0


async def test_drain_mode_blocks_new_submissions(sqlite_path: Path) -> None:
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"scheduler.drain_mode": True},
    )
    assert await scheduler.tick_once() == 0
    assert pool.in_flight == 0


async def test_drain_preserves_cursors_for_inflight(sqlite_path: Path) -> None:
    release = asyncio.Event()
    adapter = FakeAdapter(
        before_return=release.wait(),
        result_factory=lambda: FetchResult(
            items=[_item("x_1")],
            cursor={"ts": "saved-during-drain"},
            stats=SyncStats(),
        ),
    )
    scheduler, pool, _dl, _sl, sync_state, _cs, _ts = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )

    await scheduler.tick_once()
    assert pool.in_flight == 1

    # Enter drain mode: the scheduler must not submit anything new.
    scheduler._deps.config_store.set_overlay("scheduler.drain_mode", True)
    assert await scheduler.tick_once() == 0

    # Let the in-flight fetch complete; its cursor must persist.
    release.set()
    await pool.drain(timeout=1.0)
    saved = await sync_state.get_cursor("alice@x", "moodle", "default")
    assert saved == {"ts": "saved-during-drain"}


async def test_force_sync_request_submits_outside_interval(
    sqlite_path: Path,
) -> None:
    """A pending force_sync_requests row must submit the sync even when
    the user isn't due per the interval check. Row gets deleted on
    submission so a restart doesn't duplicate."""
    import sqlite3

    from ingestion.daemon.force_sync import enqueue_force_sync

    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats())
    )
    now = datetime.now(tz=UTC)
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        default_interval_seconds=86400,  # would normally block the second run
        now=now,
    )
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)
    # Without force, second tick is blocked by the interval.
    assert await scheduler.tick_once() == 0

    await enqueue_force_sync(sqlite_path, user_email="alice@x", platform="moodle")
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)

    # Row should be consumed.
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute("SELECT user_email FROM force_sync_requests").fetchall()
    finally:
        conn.close()
    assert rows == []


async def test_emergency_stop_overlay_cancels_inflight(sqlite_path: Path) -> None:
    """Flipping scheduler.emergency_stop_requested via the overlay must
    cancel inflight tasks and clear itself so subsequent ticks resume."""
    stuck = asyncio.Event()
    adapter = FakeAdapter(
        before_return=stuck.wait(),
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats()),
    )
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )
    await scheduler.tick_once()
    await drive_event_loop()
    assert pool.in_flight == 1

    store = scheduler._deps.config_store
    store.set_overlay("scheduler.emergency_stop_requested", True)
    assert await scheduler.tick_once() == 0
    await pool.wait_closed()

    # Flag cleared so the scheduler doesn't loop forever cancelling.
    assert store.get_overlay("scheduler.emergency_stop_requested") is None


async def test_emergency_stop_cancels_inflight_without_cursor_advance(
    sqlite_path: Path,
) -> None:
    stuck = asyncio.Event()
    adapter = FakeAdapter(
        before_return=stuck.wait(),
        result_factory=lambda: FetchResult(
            items=[_item("x_1")], cursor={"ts": "would_have_been_saved"}, stats=SyncStats()
        ),
    )
    scheduler, pool, _dl, _sl, sync_state, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )

    await scheduler.tick_once()
    await drive_event_loop()
    assert pool.in_flight == 1

    pool.emergency_stop()
    await pool.wait_closed()

    saved = await sync_state.get_cursor("alice@x", "moodle", "default")
    assert saved is None  # cursor did NOT advance


async def test_dead_letter_suppresses_after_threshold(sqlite_path: Path) -> None:
    adapter = FakeAdapter(fail_with=RuntimeError("boom"))
    scheduler, pool, dead_letter, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        dead_letter_threshold=2,
        dead_letter_cooldown=3600,
    )

    # Two ticks each run one task to failure. Second failure trips suppression.
    for _ in range(2):
        await scheduler.tick_once()
        await pool.drain(timeout=1.0)

    assert await dead_letter.is_suppressed("alice@x", "moodle") is True

    # Third tick must skip the suppressed row.
    submitted = await scheduler.tick_once()
    assert submitted == 0


async def test_partial_success_with_errors_does_not_dead_letter(
    sqlite_path: Path,
) -> None:
    """Partial-success syncs (stats.errors>0 on happy path) must not dead-letter.

    The pipeline treats per-scope errors as observability signals and still
    writes the cursor. Incrementing the dead-letter counter on these would
    suppress healthy users over transient per-scope flakes.
    """
    from ingestion.schemas import ErrorCode, FetchError

    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(
            cursor={"ts": "v1"},
            errors=[FetchError(code=ErrorCode.INSUFFICIENT_SCOPE, message="one scope failed")],
            stats=SyncStats(),
        ),
    )
    scheduler, pool, dead_letter, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        dead_letter_threshold=1,
        dead_letter_cooldown=3600,
    )
    await scheduler.tick_once()
    await pool.drain(timeout=1.0)

    # Partial success: cursor was written, but the row is NOT suppressed.
    assert await dead_letter.is_suppressed("alice@x", "moodle") is False


async def test_dead_letter_clear_lets_user_sync_again(sqlite_path: Path) -> None:
    adapter = FakeAdapter(fail_with=RuntimeError("boom"))
    scheduler, pool, dead_letter, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        dead_letter_threshold=1,
        dead_letter_cooldown=3600,
    )
    await scheduler.tick_once()
    await pool.drain(timeout=1.0)
    assert await dead_letter.is_suppressed("alice@x", "moodle") is True

    await dead_letter.clear("alice@x", "moodle")
    # Flip adapter to success so the next run completes cleanly.
    adapter.fail_with = None
    adapter.result_factory = lambda: FetchResult(cursor={"ts": "v2"}, stats=SyncStats())

    submitted = await scheduler.tick_once()
    assert submitted == 1
    await pool.drain(timeout=1.0)


async def test_due_check_respects_last_success(sqlite_path: Path) -> None:
    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats())
    )
    now = datetime.now(tz=UTC)
    scheduler, pool, _dl, sync_logger, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        default_interval_seconds=600,
        now=now,
    )

    # First tick: no history -> must submit.
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)

    # Second tick at the same now: the just-written sync_history row is newer
    # than the interval, so nothing is due.
    assert await scheduler.tick_once() == 0


async def test_per_user_interval_override_wins_over_default(sqlite_path: Path) -> None:
    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats())
    )
    now = datetime.now(tz=UTC)
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        default_interval_seconds=3600,  # would normally block a re-run
        now=now,
    )

    # First tick runs.
    await scheduler.tick_once()
    await pool.drain(timeout=1.0)

    # Override: per-user interval of 0s means the user is always due.
    await set_user_interval_override(sqlite_path, "alice@x", "moodle", 1)

    # Wait 1.1 seconds... instead, fake time forward via the scheduler clock.
    # SchedulerDeps is frozen; rebind with dataclasses.replace instead
    # of mutating. Scheduler itself is not frozen so ``scheduler._deps``
    # can point at the new snapshot.
    scheduler._deps = dataclasses.replace(scheduler._deps, now=lambda: now + timedelta(seconds=5))
    assert await scheduler.tick_once() == 1
    await pool.drain(timeout=1.0)


async def test_no_duplicate_submission_while_busy(sqlite_path: Path) -> None:
    stuck = asyncio.Event()
    adapter = FakeAdapter(
        before_return=stuck.wait(),
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats()),
    )
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )

    await scheduler.tick_once()
    await drive_event_loop()
    assert pool.in_flight == 1

    # Second tick while the first task is still in flight must not submit.
    submitted = await scheduler.tick_once()
    assert submitted == 0

    stuck.set()
    await pool.drain(timeout=1.0)


async def test_unknown_platform_is_skipped(sqlite_path: Path) -> None:
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "reddit")],
        adapter,
    )
    assert await scheduler.tick_once() == 0


async def test_global_paused_blocks_submission(sqlite_path: Path) -> None:
    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        overlay_overrides={"scheduler.global_paused": True},
    )
    assert await scheduler.tick_once() == 0


async def test_run_exits_when_shutdown_set(sqlite_path: Path) -> None:
    adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor={"ts": "v1"}, stats=SyncStats())
    )
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
    )
    shutdown = asyncio.Event()
    # Tick very quickly so the test is fast.
    scheduler._deps.config_store.set_overlay("scheduler.tick_seconds", 0.01)

    run_task = asyncio.create_task(scheduler.run(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(run_task, timeout=2.0)
    await pool.drain(timeout=1.0)


async def test_failure_detection_uses_sync_id_not_timestamp_ordering(
    sqlite_path: Path,
) -> None:
    """The scheduler must ask about the run it just finished by ``sync_id``,
    not ``ORDER BY started_at DESC``.

    We stage a newer ``sync_history`` row from a (hypothetical) concurrent
    retry, then run a failing sync and assert the scheduler raises. If the
    scheduler were reading ``ORDER BY started_at DESC``, the newer fake
    row's ``state='complete'`` would mask the current run's failure.
    """
    import sqlite3

    adapter = FakeAdapter(fail_with=RuntimeError("boom"))
    scheduler, pool, dead_letter, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle")],
        adapter,
        dead_letter_threshold=1,
        dead_letter_cooldown=3600,
    )

    # Inject a row with a future started_at that is NOT the run the
    # scheduler is about to execute. Under the old query this would
    # become "most recent" and mask the real outcome.
    future = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("ghost-sync-id", "alice@x", "moodle", future, "complete"),
        )
        conn.commit()
    finally:
        conn.close()

    await scheduler.tick_once()
    await pool.drain(timeout=1.0)

    # The failed run must have incremented dead-letter state despite the
    # future ghost row. With threshold=1, a single failure suppresses.
    assert await dead_letter.is_suppressed("alice@x", "moodle") is True


async def test_eligibility_prefetch_batches_once_per_tick(
    sqlite_path: Path,
) -> None:
    """Proof that ``_eligible`` no longer issues per-candidate queries.

    Counts how many times ``last_success_at_bulk`` and
    ``user_interval_overrides_bulk`` are called per tick. The contract
    is "exactly one of each, regardless of candidate count" — without
    the batch fix, the per-candidate helpers fired N times instead.
    """
    import ingestion.daemon.scheduler as scheduler_mod

    bulk_last_calls = 0
    bulk_override_calls = 0

    orig_last = scheduler_mod.last_success_at_bulk
    orig_override = scheduler_mod.user_interval_overrides_bulk

    async def _counting_last(db_path: Path) -> dict[tuple[str, str], datetime]:
        nonlocal bulk_last_calls
        bulk_last_calls += 1
        return await orig_last(db_path)

    async def _counting_override(
        db_path: Path, scope: str = "default"
    ) -> dict[tuple[str, str], int]:
        nonlocal bulk_override_calls
        bulk_override_calls += 1
        return await orig_override(db_path, scope)

    scheduler_mod.last_success_at_bulk = _counting_last  # type: ignore[assignment]
    scheduler_mod.user_interval_overrides_bulk = _counting_override  # type: ignore[assignment]
    try:
        # Paused candidates skip the pipeline entirely so the submission
        # side doesn't inflate the count under test.
        candidates = [Candidate(f"u{i}@x", "moodle", is_paused=True) for i in range(50)]
        adapter = FakeAdapter()
        scheduler, pool, *_ = _build(sqlite_path, candidates, adapter)

        await scheduler.tick_once()
        await scheduler.tick_once()
        await pool.drain(timeout=1.0)
    finally:
        scheduler_mod.last_success_at_bulk = orig_last  # type: ignore[assignment]
        scheduler_mod.user_interval_overrides_bulk = orig_override  # type: ignore[assignment]

    # Two ticks, two calls to each bulk helper. 50 candidates per tick,
    # but the per-candidate singular helpers never fire.
    assert bulk_last_calls == 2, f"expected 2 bulk last-success calls, got {bulk_last_calls}"
    assert bulk_override_calls == 2, (
        f"expected 2 bulk interval-override calls, got {bulk_override_calls}"
    )


async def test_local_store_reads_do_not_block_behind_writes(
    sqlite_path: Path,
) -> None:
    """Prove readers use a separate connection path so a slow writer
    doesn't stall the scheduler's config load.

    Holds the write lock from one thread while the main thread reads
    the overlay; with a single shared lock, the read would wait. With
    the split read/write locks, the read proceeds immediately.
    """
    import threading
    import time

    from ingestion.daemon.store.local import open_store

    store = open_store(sqlite_path)
    try:
        store.set_overlay("scheduler.tick_seconds", 1.0)

        write_holding = threading.Event()
        write_release = threading.Event()

        def _hold_write_lock() -> None:
            # Acquire the write lock directly and sit on it until released.
            # Simulates an in-flight API mutation that hasn't committed yet.
            with store._write_lock:
                write_holding.set()
                write_release.wait(timeout=5.0)

        t = threading.Thread(target=_hold_write_lock, daemon=True)
        t.start()
        assert write_holding.wait(timeout=2.0), "writer failed to acquire lock"

        # The read MUST proceed promptly (well under 1s) while the
        # write lock is held. Previously it would block.
        start = time.monotonic()
        value = store.get_overlay("scheduler.tick_seconds")
        elapsed = time.monotonic() - start
        write_release.set()
        t.join(timeout=2.0)

        assert value == 1.0
        assert elapsed < 0.5, f"read blocked {elapsed:.3f}s behind write"
    finally:
        store.close()


async def test_scope_vanished_warning_fires_once_per_scope(
    sqlite_path: Path,
) -> None:
    """When ``scope_for_platform`` no longer covers a stored cursor scope,
    the scheduler logs the WARN once and (slice 12) deletes the row on
    the same tick.

    Pre-slice-12 behaviour kept the warn-once memory intact across
    ticks; post-slice-12 the cleanup pass clears the memory so a future
    drift re-warns. The DB-side check (no row left) is the canonical
    pin for the deletion path.
    """
    import sqlite3

    # Stage a cursor at a scope that's going to "vanish."
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("alice@x", "moodle", "forums", '{"ts": "v1"}', "2026-04-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle", is_paused=True)],
        adapter,
    )

    # Inject a scope resolver that omits "forums" on purpose.
    from ingestion.schemas import FetchScope

    scheduler._deps = dataclasses.replace(
        scheduler._deps,
        scope_for_platform=lambda _p: frozenset({FetchScope.ASSIGNMENTS}),
    )

    await scheduler.tick_once()
    await scheduler.tick_once()  # second tick: must NOT re-warn.
    await pool.drain(timeout=1.0)

    # The cleanup pass ran on the first tick and deleted the row.
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute(
            "SELECT scope FROM sync_cursors WHERE user_email=? AND platform=?",
            ("alice@x", "moodle"),
        ).fetchall()
    finally:
        conn.close()
    assert ("forums",) not in rows
    # And the cleanup pass marked itself done so subsequent ticks skip
    # the prune. The warn-once memory is cleared by the cleanup hook so
    # any future re-emergence of the same scope re-warns + re-deletes.
    assert scheduler._scope_cleanup_done is True


async def test_scope_vanished_skipped_when_resolver_returns_none(
    sqlite_path: Path,
) -> None:
    """A ``scope_for_platform`` that returns ``None`` means "adapter
    decides" and the scheduler can't prove any scope is orphaned."""
    import sqlite3

    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("alice@x", "moodle", "forums", '{"ts": "v1"}', "2026-04-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    adapter = FakeAdapter()
    scheduler, pool, *_ = _build(
        sqlite_path,
        [Candidate("alice@x", "moodle", is_paused=True)],
        adapter,
    )

    scheduler._deps = dataclasses.replace(
        scheduler._deps,
        scope_for_platform=lambda _p: None,
    )

    await scheduler.tick_once()
    await pool.drain(timeout=1.0)

    assert scheduler._scope_vanish_warned == set()


# DaemonConfig isn't directly referenced above, but the import validates the
# helper path imports cleanly.
_ = DaemonConfig
