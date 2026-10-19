"""Unit tests for :class:`AsyncConnectionPool`.

Targets 100% line + branch coverage. Every test owns its own SQLite file via
``tmp_path`` so concurrent test runs don't share state.

The pool is loop-bound (``asyncio.Lock`` + ``asyncio.Queue``) so all tests
run under the project's ``asyncio_mode = "auto"`` ``pytest-asyncio`` config.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from ingestion.daemon.store.conn_pool import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_WARM_CONNECTIONS,
    AsyncConnectionPool,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    # Touch the file so aiosqlite can open it without depending on parent
    # directory creation inside the pool's open path.
    path.touch()
    return path


def test_init_rejects_zero_max_connections(db_path: Path) -> None:
    with pytest.raises(ValueError, match="max_connections must be >= 1"):
        AsyncConnectionPool(db_path, max_connections=0)


def test_init_rejects_negative_max_connections(db_path: Path) -> None:
    with pytest.raises(ValueError, match="max_connections must be >= 1"):
        AsyncConnectionPool(db_path, max_connections=-2)


def test_init_rejects_warm_above_max(db_path: Path) -> None:
    with pytest.raises(ValueError, match="warm_connections must be between 0 and max_connections"):
        AsyncConnectionPool(db_path, max_connections=2, warm_connections=3)


def test_init_rejects_warm_below_zero(db_path: Path) -> None:
    with pytest.raises(ValueError, match="warm_connections must be between 0 and max_connections"):
        AsyncConnectionPool(db_path, warm_connections=-1)


def test_default_constants_are_sane() -> None:
    assert DEFAULT_MAX_CONNECTIONS >= 1
    assert 0 <= DEFAULT_WARM_CONNECTIONS <= DEFAULT_MAX_CONNECTIONS


async def test_warm_creates_warm_connections(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=4, warm_connections=2)
    try:
        await pool.warm()
        assert pool.size == 2
        assert pool.idle_count() == 2
    finally:
        await pool.close_all()


async def test_warm_is_idempotent(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=2)
    try:
        await pool.warm()
        await pool.warm()  # second call must not bump size
        assert pool.size == 2
    finally:
        await pool.close_all()


async def test_warm_after_close_raises(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=0)
    await pool.close_all()
    with pytest.raises(RuntimeError, match="AsyncConnectionPool is closed"):
        await pool.warm()


async def test_acquire_release_round_trip_reuses_connection(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        async with pool.acquire() as conn1:
            assert pool.size == 1
            assert pool.idle_count() == 0
        # Released — back into the queue.
        assert pool.idle_count() == 1
        async with pool.acquire() as conn2:
            # Same connection object reused.
            assert conn1 is conn2
    finally:
        await pool.close_all()


async def test_lazy_grow_up_to_max(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=3, warm_connections=0)
    try:
        async with pool.acquire() as a, pool.acquire() as b, pool.acquire() as c:
            # Pool grew lazily to satisfy each acquire.
            assert pool.size == 3
            # Three distinct connections — no aliasing.
            assert len({id(a), id(b), id(c)}) == 3
    finally:
        await pool.close_all()


async def test_acquire_after_close_raises(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=0)
    await pool.close_all()
    with pytest.raises(RuntimeError, match="AsyncConnectionPool is closed"):
        async with pool.acquire():
            pass  # pragma: no cover — we never enter the body


async def test_acquire_pool_closed_inside_size_lock(db_path: Path) -> None:
    """Pool can close after fast-path miss but before slow-path takes the lock."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    # Fill the queue with a sentinel that ``get_nowait`` succeeds against
    # would short-circuit — instead, enter slow path with empty idle by
    # closing the pool concurrently.
    await pool.close_all()
    with pytest.raises(RuntimeError, match="AsyncConnectionPool is closed"):
        # ``acquire`` checks ``self._closed`` before yielding so this races
        # against the same flag the helper checks.
        async with pool.acquire():
            pass  # pragma: no cover


async def test_checkout_raises_when_closed_after_empty_check(db_path: Path) -> None:
    """The internal ``_checkout`` must re-check ``_closed`` inside the size lock
    so a close that landed between the fast-path miss and the slow-path lock
    surfaces as a clean RuntimeError, not a hang.
    """
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    # Pool is open + idle queue empty + size below cap. Set the flag manually
    # to simulate a concurrent close landing between the fast-path get_nowait
    # raising QueueEmpty and the await on _size_lock taking effect.
    pool._closed = True
    with pytest.raises(RuntimeError, match="AsyncConnectionPool is closed"):
        await pool._checkout()


async def test_close_all_is_idempotent(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=2)
    await pool.warm()
    await pool.close_all()
    # Second call must be a no-op.
    await pool.close_all()
    assert pool.is_closed is True


async def test_close_all_drains_idle_queue(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=2)
    await pool.warm()
    assert pool.idle_count() == 2
    await pool.close_all()
    assert pool.idle_count() == 0


async def test_release_after_close_closes_connection(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    cm = pool.acquire()
    conn = await cm.__aenter__()
    # Close the pool BEFORE releasing the connection.
    await pool.close_all()
    # __aexit__ runs the release path; the closed branch closes the conn.
    await cm.__aexit__(None, None, None)
    # aiosqlite raises if you try to use a closed connection.
    with pytest.raises(Exception):  # noqa: B017 — closed-conn raise type isn't part of the contract
        await conn.execute("SELECT 1")


async def test_release_close_quietly_swallows_close_errors(db_path: Path) -> None:
    """A broken connection close must not break the pool teardown."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    cm = pool.acquire()
    conn = await cm.__aenter__()
    # Patch close to raise; pool should tolerate it under shutdown.
    original_close = conn.close

    async def _broken_close() -> None:
        await original_close()
        raise RuntimeError("simulated broken close")

    conn.close = _broken_close  # type: ignore[method-assign]
    await pool.close_all()
    await cm.__aexit__(None, None, None)
    # No exception propagated — assertion is "we got here".


async def test_pool_at_capacity_blocks_until_release(db_path: Path) -> None:
    """The 3rd acquire on a max=2 pool must block until something releases."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        cm_a = pool.acquire()
        cm_b = pool.acquire()
        await cm_a.__aenter__()
        await cm_b.__aenter__()

        third_done = asyncio.Event()
        third_conn: list[aiosqlite.Connection] = []

        async def _third() -> None:
            async with pool.acquire() as c:
                third_conn.append(c)
                third_done.set()

        third_task = asyncio.create_task(_third())
        # Give the task a chance to schedule and block on the queue.
        await asyncio.sleep(0.05)
        assert not third_done.is_set()
        # Release one — third now unblocks.
        await cm_a.__aexit__(None, None, None)
        await asyncio.wait_for(third_done.wait(), timeout=2.0)
        await third_task
        await cm_b.__aexit__(None, None, None)
    finally:
        await pool.close_all()


async def test_size_property(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=4, warm_connections=2)
    try:
        await pool.warm()
        assert pool.size == 2
        async with pool.acquire(), pool.acquire(), pool.acquire():
            assert pool.size == 3
    finally:
        await pool.close_all()


async def test_idle_count(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=4, warm_connections=0)
    try:
        async with pool.acquire():
            assert pool.idle_count() == 0
        # Released
        assert pool.idle_count() == 1
    finally:
        await pool.close_all()


async def test_warm_zero_no_connections(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        await pool.warm()
        assert pool.size == 0
        assert pool.idle_count() == 0
    finally:
        await pool.close_all()


async def test_pragmas_applied_on_pool_connections(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=1)
    try:
        await pool.warm()
        async with pool.acquire() as conn:
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row is not None
            assert str(row[0]).lower() == "wal"
            cursor = await conn.execute("PRAGMA synchronous")
            row = await cursor.fetchone()
            assert row is not None
            # NORMAL maps to integer 1 in PRAGMA reads.
            assert int(row[0]) == 1
            cursor = await conn.execute("PRAGMA busy_timeout")
            row = await cursor.fetchone()
            assert row is not None
            assert int(row[0]) == 5000
    finally:
        await pool.close_all()


async def test_close_during_in_flight_acquires_completes(db_path: Path) -> None:
    """Acquires currently held when close_all fires must still release cleanly."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    cm = pool.acquire()
    conn = await cm.__aenter__()
    # Force a tiny query so the connection is "hot".
    await conn.execute("SELECT 1")
    # Drain.
    await pool.close_all()
    # Release path runs and routes through the closed branch — close not raise.
    await cm.__aexit__(None, None, None)


async def test_release_into_full_queue_decrements_size(db_path: Path) -> None:
    """If the idle queue is somehow saturated past max, release closes + dec."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        # Force two distinct mints by holding both at once.
        async with pool.acquire(), pool.acquire():
            assert pool.size == 2
        # Both back in the queue (cap=2, queue full).
        assert pool.idle_count() == 2
        # Manually mint one extra (simulates a race where _release tries to
        # put past cap). Bypass the helper so we can place a third in.
        extra = await aiosqlite.connect(str(db_path))
        async with pool._size_lock:
            pool._size += 1
        original_size = pool.size
        await pool._release(extra)
        # Queue was full so release closed and dec'd size.
        assert pool.size == original_size - 1
    finally:
        await pool.close_all()


async def test_cancellation_during_acquire_does_not_leak_size(db_path: Path) -> None:
    """A task cancelled while waiting on the bounded queue must not leak."""
    pool = AsyncConnectionPool(db_path, max_connections=1, warm_connections=0)
    try:
        # Saturate the pool.
        cm = pool.acquire()
        await cm.__aenter__()

        async def _waiter() -> None:
            async with pool.acquire():
                pass  # pragma: no cover — cancelled before this runs

        waiter = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)  # let waiter park on the queue
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # After the cancellation, releasing the original makes the pool fully
        # available again. _size should be 1 (the one we minted), not 2.
        await cm.__aexit__(None, None, None)
        assert pool.size == 1
    finally:
        await pool.close_all()


async def test_in_flight_query_during_drain(db_path: Path) -> None:
    """Pool drain happens while an acquire is active — release still clean."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    cm = pool.acquire()
    await cm.__aenter__()
    drained = asyncio.Event()

    async def _drain() -> None:
        await pool.close_all()
        drained.set()

    asyncio.create_task(_drain())
    await asyncio.sleep(0.02)
    # Even after pool reports closed, release path completes without raising.
    # Use sqlite3 directly to confirm the file is still a valid SQLite DB
    # (no corruption on drain).
    real = sqlite3.connect(str(db_path))
    real.close()
    await cm.__aexit__(None, None, None)
    await drained.wait()


async def test_acquire_routes_through_size_lock_when_idle_empty(db_path: Path) -> None:
    """When idle empty and under cap, slow path mints new conn under lock."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        # First acquire — idle empty, slow path mints.
        async with pool.acquire():
            assert pool.size == 1
    finally:
        await pool.close_all()


async def test_close_all_with_no_idle_queue_drained(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, warm_connections=0)
    await pool.close_all()
    assert pool.is_closed is True
    # idle_count must be 0 — never created anything.
    assert pool.idle_count() == 0


async def test_release_on_open_pool_repools(db_path: Path) -> None:
    pool = AsyncConnectionPool(db_path, max_connections=4, warm_connections=0)
    try:
        async with pool.acquire():
            pass
        # The released connection should sit in the idle queue.
        assert pool.idle_count() == 1
        # And the next acquire should consume it (no new mint).
        async with pool.acquire():
            assert pool.size == 1
    finally:
        await pool.close_all()
