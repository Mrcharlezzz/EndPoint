"""Drain-then-restart: cursors persist across daemon instances.

Runs one scheduler instance to completion on a given sqlite file, drains it,
then builds a second scheduler pointed at the same file. The second instance
must pick up the cursor the first instance wrote and pass it to the adapter
on the next fetch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from ingestion.daemon.candidates import Candidate, InMemoryCandidateSource
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
from ingestion.daemon.ports.sync_state import SqliteSyncStateStore
from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
from ingestion.daemon.store.local import apply_migrations, open_store
from ingestion.daemon.worker import WorkerPool
from ingestion.schemas import FetchResult, SyncStats

from tests.daemon._scheduler_helpers import (
    FakeAdapter,
    InMemoryContentSink,
    InMemoryTokenStore,
)

if TYPE_CHECKING:
    from ingestion.ports import Adapter


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _build_scheduler(
    sqlite_path: Path,
    adapter: FakeAdapter,
    *,
    now: datetime,
    default_interval_seconds: int = 1,
) -> tuple[Scheduler, WorkerPool, SqliteSyncStateStore]:
    config_store = open_store(sqlite_path)
    config_store.set_overlay("scheduler.default_interval_seconds", default_interval_seconds)
    dead_letter = DeadLetterTracker(sqlite_path, threshold=3, cooldown_seconds=60)
    token_store = InMemoryTokenStore({("alice@x", "moodle"): {"token": "tok"}})
    content_sink = InMemoryContentSink()
    sync_state = SqliteSyncStateStore(sqlite_path)
    sync_logger = SqliteSyncLogger(sqlite_path)

    async def on_finish(_user: str, _platform: str, _exc: BaseException | None) -> None:
        return None

    pool = WorkerPool(global_limit=2, on_finish=on_finish)

    def _factory(_platform: str) -> Adapter:
        return cast("Adapter", adapter)

    deps = SchedulerDeps(
        config_store=config_store,
        candidate_source=InMemoryCandidateSource([Candidate("alice@x", "moodle")]),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=cast("Adapter", token_store),  # type: ignore[arg-type]
        content_sink=cast("Adapter", content_sink),  # type: ignore[arg-type]
        sync_state=cast("Adapter", sync_state),  # type: ignore[arg-type]
        sync_logger=cast("Adapter", sync_logger),  # type: ignore[arg-type]
        adapter_factory=_factory,
        sqlite_path=sqlite_path,
        now=lambda: now,
    )
    return Scheduler(deps), pool, sync_state


async def test_drain_then_restart_resumes_at_saved_cursor(sqlite_path: Path) -> None:
    saved_cursor = {"ts": "2026-04-19T00:00:00Z", "offset": 42}

    # First daemon instance: runs once, writes the cursor, then drains.
    first_adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor=saved_cursor, stats=SyncStats())
    )
    now_1 = datetime.now(tz=UTC)
    scheduler_1, pool_1, state_1 = _build_scheduler(sqlite_path, first_adapter, now=now_1)

    assert await scheduler_1.tick_once() == 1
    await pool_1.drain(timeout=1.0)

    # Cursor landed in the shared SQLite.
    persisted = await state_1.get_cursor("alice@x", "moodle", "default")
    assert persisted == saved_cursor

    # Simulate restart: close the first pool, bring up a fresh scheduler.
    await pool_1.close()

    # Second adapter records the cursor it's handed on fetch.
    second_adapter = FakeAdapter(
        result_factory=lambda: FetchResult(cursor={"ts": "v2"}, stats=SyncStats()),
    )
    now_2 = now_1 + timedelta(seconds=30)  # past the default interval
    scheduler_2, pool_2, state_2 = _build_scheduler(sqlite_path, second_adapter, now=now_2)

    assert await scheduler_2.tick_once() == 1
    await pool_2.drain(timeout=1.0)

    # The adapter was invoked with the cursor the previous run wrote.
    assert len(second_adapter.fetch_calls) == 1
    assert second_adapter.fetch_calls[0]["cursor"] == saved_cursor

    # New cursor wrote over the old one.
    persisted_2 = await state_2.get_cursor("alice@x", "moodle", "default")
    assert persisted_2 == {"ts": "v2"}

    await pool_2.close()


async def test_drain_waits_for_inflight_before_restart_can_begin(sqlite_path: Path) -> None:
    """Belt-and-braces: restart semantics rely on drain actually awaiting."""
    release = asyncio.Event()
    adapter = FakeAdapter(
        before_return=release.wait(),
        result_factory=lambda: FetchResult(cursor={"ts": "v"}, stats=SyncStats()),
    )
    now = datetime.now(tz=UTC)
    scheduler, pool, state = _build_scheduler(sqlite_path, adapter, now=now)

    await scheduler.tick_once()
    assert pool.in_flight == 1

    # Simulate: signal drain and await. The drain must not return before the
    # in-flight task completes.
    drain_task = asyncio.create_task(pool.drain(timeout=5.0))
    await asyncio.sleep(0.05)
    assert not drain_task.done()

    release.set()
    await drain_task
    saved = await state.get_cursor("alice@x", "moodle", "default")
    assert saved == {"ts": "v"}
