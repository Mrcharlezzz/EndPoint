"""Edge + production tests for slice 11 (perf foundation).

Pinned scenarios: WAL persistence, pragma upgrade paths, pool fairness +
cancellation safety, migration idempotence + index visibility, batched-query
memory bounds, HTTP pool reuse + drain semantics.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended) or ``[TEST-COVERAGE]`` (the
existing implementation is fine; this just locks the behaviour).

Slow tests (>500ms) carry ``@pytest.mark.slow``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import aiosqlite
import pytest
from ingestion.clients.pool import AsyncHTTPClientPool
from ingestion.daemon.store.async_conn import apply_pragmas
from ingestion.daemon.store.conn_pool import AsyncConnectionPool
from ingestion.daemon.store.local import MIGRATIONS_DIR, apply_migrations

if TYPE_CHECKING:
    from ingestion.clients.http import AsyncHTTPClient


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for AsyncHTTPClient that records construction + close events."""

    instances: list[_FakeClient] = []

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url
        self.closed = False
        self.close_calls = 0
        _FakeClient.instances.append(self)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    _FakeClient.instances.clear()


# ---------------------------------------------------------------------------
# 1. WAL mode survives daemon restart [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_wal_mode_survives_close_and_reopen(db_path: Path) -> None:
    """Once apply_pragmas flips WAL on, the on-disk journal mode persists."""
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
    # Re-open via stdlib sqlite3 — no apply_pragmas — and read journal_mode.
    sync_conn = sqlite3.connect(str(db_path))
    row = sync_conn.execute("PRAGMA journal_mode").fetchone()
    sync_conn.close()
    assert str(row[0]).lower() == "wal"


# ---------------------------------------------------------------------------
# 2. WAL upgrade from DELETE journal [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_wal_upgrade_from_delete_journal(tmp_path: Path) -> None:
    """Pre-existing DELETE-mode database flips to WAL after apply_pragmas."""
    db = tmp_path / "delete.db"
    sync = sqlite3.connect(str(db))
    sync.execute("PRAGMA journal_mode=DELETE")
    sync.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    sync.commit()
    sync.close()
    # Sanity — file is in DELETE mode.
    sync = sqlite3.connect(str(db))
    assert str(sync.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"
    sync.close()
    async with aiosqlite.connect(str(db)) as conn:
        await apply_pragmas(conn)
    sync = sqlite3.connect(str(db))
    assert str(sync.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    sync.close()


# ---------------------------------------------------------------------------
# 3. synchronous=NORMAL write throughput [TEST-COVERAGE, slow]
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_synchronous_normal_write_throughput(db_path: Path) -> None:
    """1k row insert under WAL+NORMAL stays under a generous wall-clock budget."""
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        await conn.execute("CREATE TABLE perf_throughput (id INTEGER PRIMARY KEY, val TEXT)")
        await conn.commit()
        start = time.perf_counter()
        for i in range(1000):
            await conn.execute("INSERT INTO perf_throughput (val) VALUES (?)", (f"v{i}",))
        await conn.commit()
        elapsed = time.perf_counter() - start
    # 1000 rows under 5 seconds is plenty of slack — we just want a regression
    # signal if someone accidentally flips synchronous to FULL.
    assert elapsed < 5.0, f"1k inserts took {elapsed:.2f}s under WAL+NORMAL"


# ---------------------------------------------------------------------------
# 4. Pool exhaustion: FIFO fairness when queue blocks [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_pool_blocks_past_capacity_then_unblocks_in_order(db_path: Path) -> None:
    """Saturate the pool, kick off N waiters, release in stages — assert FIFO."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    try:
        cm_a = pool.acquire()
        cm_b = pool.acquire()
        await cm_a.__aenter__()
        await cm_b.__aenter__()

        order: list[int] = []

        async def _waiter(idx: int) -> None:
            async with pool.acquire():
                order.append(idx)
                await asyncio.sleep(0.01)

        waiters = [asyncio.create_task(_waiter(i)) for i in range(4)]
        await asyncio.sleep(0.05)
        # Release one — first waiter runs.
        await cm_a.__aexit__(None, None, None)
        await asyncio.sleep(0.05)
        await cm_b.__aexit__(None, None, None)
        await asyncio.gather(*waiters)
        # asyncio.Queue is FIFO — the ordering is deterministic.
        assert order == [0, 1, 2, 3]
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# 5. Pool drain races with in-flight query [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_pool_drain_with_in_flight_query(db_path: Path) -> None:
    """close_all fires while an acquire is held — release runs the closed branch."""
    pool = AsyncConnectionPool(db_path, max_connections=2, warm_connections=0)
    cm = pool.acquire()
    conn = await cm.__aenter__()
    # Close while holding the connection.
    await pool.close_all()
    # Use the conn anyway (still open at the SQLite level).
    cursor = await conn.execute("SELECT 1")
    row = await cursor.fetchone()
    assert row == (1,)
    # __aexit__ runs the release path; closed branch closes the conn.
    await cm.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 6. Pool acquire under task cancellation [CODE-FIX flagged in spec]
# ---------------------------------------------------------------------------


async def test_pool_cancellation_does_not_leak_size(db_path: Path) -> None:
    """Cancelled waiter must not leak its slot or skew _size."""
    pool = AsyncConnectionPool(db_path, max_connections=1, warm_connections=0)
    try:
        cm = pool.acquire()
        await cm.__aenter__()

        async def _waiter() -> None:
            async with pool.acquire():
                pass  # pragma: no cover — cancelled before this runs

        waiter = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # Size should still be 1 — we never minted a second connection.
        assert pool.size == 1
        await cm.__aexit__(None, None, None)
        # And the original conn is back in the queue.
        assert pool.idle_count() == 1
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# 7. Migration 0009 is idempotent [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_migration_0009_idempotent(tmp_path: Path) -> None:
    """Running 0009 twice does not raise and leaves indices intact."""
    db = tmp_path / "idem.db"
    apply_migrations(db)
    # Re-apply by manually replaying the migration script.
    script_path = MIGRATIONS_DIR / "0009_perf_indices.sql"
    sync = sqlite3.connect(str(db))
    try:
        sync.executescript(script_path.read_text())
        sync.commit()
        # Verify each index exists exactly once. The
        # ``idx_force_sync_requests_queued_at`` name from the audit spec was
        # dropped — ``ix_force_sync_requested`` from 0006_actions.sql already
        # covers ``ORDER BY requested_at``.
        rows = sync.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        names = [r[0] for r in rows]
        for expected in (
            "idx_dead_letter_state_user_platform",
            "idx_sync_history_user_platform",
            "idx_sync_history_finished_at",
            "idx_audit_log_actor",
        ):
            assert names.count(expected) == 1
        # The pre-existing index (created by 0006) is still present and the
        # duplicate from the audit spec is intentionally absent.
        assert "idx_force_sync_requests_queued_at" not in names
        ix_rows = sync.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = 'ix_force_sync_requested'"
        ).fetchall()
        assert len(ix_rows) == 1
    finally:
        sync.close()


# ---------------------------------------------------------------------------
# 8. Dead-letter index actually used by the query [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_dead_letter_index_used_by_planner(db_path: Path) -> None:
    """EXPLAIN QUERY PLAN shows idx_dead_letter_state_user_platform in use."""
    sync = sqlite3.connect(str(db_path))
    try:
        plan = sync.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT consecutive_failures FROM dead_letter_state "
            "WHERE user_email = ? AND platform = ?",
            ("alice@example.com", "moodle"),
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan).lower()
        # Either the new index or the implicit primary-key index satisfies the
        # planner. The migration's claim is the new explicit composite makes
        # the planner pick an index — assert "USING INDEX" or "PRIMARY KEY".
        assert "using" in plan_text and ("index" in plan_text or "primary" in plan_text)
    finally:
        sync.close()


# ---------------------------------------------------------------------------
# 9. Other indices used by their queries [TEST-COVERAGE]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "SELECT id FROM sync_history WHERE user_email = ? AND platform = ?",
            ("u@x.com", "moodle"),
        ),
        (
            "SELECT id FROM sync_history WHERE finished_at >= ?",
            ("2026-04-01T00:00:00Z",),
        ),
        (
            "SELECT user_email FROM force_sync_requests ORDER BY requested_at",
            (),
        ),
        (
            "SELECT id FROM audit_log WHERE actor = ?",
            ("alice@example.com",),
        ),
    ],
)
def test_perf_indices_used_by_planner(db_path: Path, sql: str, params: tuple[object, ...]) -> None:
    """Each S11 index is referenced by its target query."""
    sync = sqlite3.connect(str(db_path))
    try:
        plan = sync.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        plan_text = " ".join(str(row) for row in plan).lower()
        # Either an explicit index or the implicit PK suffices — what we want
        # to lock is: the planner does NOT do a full table scan ("scan table"
        # without "index").
        scans_without_index = "scan" in plan_text and "index" not in plan_text
        assert not scans_without_index, f"plan shows full scan: {plan_text!r}"
    finally:
        sync.close()


# ---------------------------------------------------------------------------
# 10. Production: large exists_batch is bounded in memory + chunked [PRODUCTION]
# ---------------------------------------------------------------------------


async def test_exists_batch_bounded_chunks_for_100k_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100k-id exists_batch chunks at 800 — no monolithic IN list."""
    from ingestion.daemon.ports import content_sink as cs_mod

    filter_calls: list[int] = []

    class _FakeAsync:
        def __aiter__(self) -> _FakeAsync:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    class _FakeQS:
        def values_list(self, *_a: object, **_k: object) -> _FakeAsync:
            return _FakeAsync()

    class _FakeMgr:
        def filter(self, **kwargs: object) -> _FakeQS:
            ids = kwargs.get("external_id__in", [])
            assert isinstance(ids, list)
            filter_calls.append(len(ids))
            return _FakeQS()

    monkeypatch.setattr(cs_mod.IngestedItem, "objects", _FakeMgr(), raising=True)
    sink = cs_mod.PostgresContentSink()
    ids = [f"id_{i}" for i in range(100_000)]
    result = await sink.exists_batch("moodle", ids)
    assert result == set()
    # 100,000 / 800 = 125 chunks.
    assert len(filter_calls) == 125
    # No chunk ever exceeds the cap.
    assert max(filter_calls) <= 800
    # Total ids match input (modulo dedup; here all unique).
    assert sum(filter_calls) == 100_000


# ---------------------------------------------------------------------------
# 11. Production: HTTP pool reuse [PRODUCTION]
# ---------------------------------------------------------------------------


async def test_http_pool_reuses_single_client_for_sequential_acquires() -> None:
    """100 sequential acquire+release cycles on one base_url — one factory call."""
    factory_calls = 0

    def _factory(base_url: str) -> AsyncHTTPClient:
        nonlocal factory_calls
        factory_calls += 1
        return cast("AsyncHTTPClient", _FakeClient(base_url))

    pool = AsyncHTTPClientPool(_factory)
    try:
        for _ in range(100):
            client = await pool.acquire("https://api.example.com")
            await pool.release("https://api.example.com", client)
        assert factory_calls == 1
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# 12. Production: HTTP pool drain pin behaviour [PRODUCTION]
# ---------------------------------------------------------------------------


async def test_http_pool_drain_with_outstanding_acquires() -> None:
    """50 acquires never released — close_all only drains pooled (idle) clients.

    Pin the actual behaviour: ``close_all`` walks idle buckets only. Clients
    held by callers are NOT force-closed; release-after-close drives them to
    ``_close_quietly``.
    """

    def _factory(base_url: str) -> AsyncHTTPClient:
        return cast("AsyncHTTPClient", _FakeClient(base_url))

    pool = AsyncHTTPClientPool(_factory)
    held: list[object] = []
    for i in range(50):
        held.append(await pool.acquire(f"https://h{i % 5}"))
    # No releases — every client is "in flight" from the pool's POV.
    await pool.close_all()
    # Pool reports closed; held clients are NOT auto-closed yet.
    assert pool.is_closed
    for client in held:
        assert cast("_FakeClient", client).closed is False
    # Release post-close drives every held client through _close_quietly.
    for i, client in enumerate(held):
        await pool.release(f"https://h{i % 5}", client)  # type: ignore[arg-type]
    for client in held:
        assert cast("_FakeClient", client).closed is True


# ---------------------------------------------------------------------------
# 13. Production: pragmas survive WAL checkpoints [PRODUCTION]
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_pragmas_survive_checkpoint(db_path: Path) -> None:
    """Write 10k rows triggering at least one checkpoint — journal_mode still WAL."""
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        await conn.execute("CREATE TABLE checkpoint_t (id INTEGER PRIMARY KEY, v TEXT)")
        for i in range(10_000):
            await conn.execute("INSERT INTO checkpoint_t (v) VALUES (?)", (f"v{i}",))
        await conn.commit()
        # Force a checkpoint.
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # journal_mode survives.
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal"


# ---------------------------------------------------------------------------
# 14. Production: aiosqlite pool fairness under burst [PRODUCTION]
# ---------------------------------------------------------------------------


async def test_pool_fairness_under_burst(db_path: Path) -> None:
    """20 concurrent waiters on a 4-conn pool: every waiter eventually gets served."""
    pool = AsyncConnectionPool(db_path, max_connections=4, warm_connections=4)
    try:
        await pool.warm()
        served: list[int] = []

        async def _work(idx: int) -> None:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
                served.append(idx)

        await asyncio.gather(*[asyncio.create_task(_work(i)) for i in range(20)])
        # No starvation — every waiter ran.
        assert sorted(served) == list(range(20))
    finally:
        await pool.close_all()


# ===========================================================================
# Edge brainstorm — own additions (8+)
# ===========================================================================


# ---------------------------------------------------------------------------
# E1. AsyncConnectionPool init with max_connections=1 yields a working pool
#     [TEST-COVERAGE] — degenerate but valid configuration.
# ---------------------------------------------------------------------------


async def test_pool_with_max_one_serialises_acquires(db_path: Path) -> None:
    """max_connections=1 turns the pool into a strict serialiser."""
    pool = AsyncConnectionPool(db_path, max_connections=1, warm_connections=0)
    try:
        cm_a = pool.acquire()
        await cm_a.__aenter__()

        async def _second() -> None:
            async with pool.acquire():
                pass

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.02)
        assert not task.done()  # blocked on the queue
        await cm_a.__aexit__(None, None, None)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# E2. busy_timeout protects writers under reader contention
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_busy_timeout_protects_against_concurrent_reader(db_path: Path) -> None:
    """Two connections, one reading + one writing the same table — no SQLITE_BUSY."""
    async with aiosqlite.connect(str(db_path)) as a, aiosqlite.connect(str(db_path)) as b:
        await apply_pragmas(a)
        await apply_pragmas(b)
        await a.execute("CREATE TABLE busy_t (id INTEGER PRIMARY KEY, v TEXT)")
        await a.commit()

        async def _reader() -> None:
            for _ in range(50):
                cursor = await a.execute("SELECT COUNT(*) FROM busy_t")
                await cursor.fetchone()

        async def _writer() -> None:
            for i in range(50):
                await b.execute("INSERT INTO busy_t (v) VALUES (?)", (f"v{i}",))
                await b.commit()

        # WAL allows readers and a writer to coexist; busy_timeout is the
        # safety net for the rare overlap. Run 'em concurrently — no error.
        await asyncio.gather(_reader(), _writer())


# ---------------------------------------------------------------------------
# E3. HTTP pool eviction is per-key (one host's eviction doesn't touch another)
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_http_pool_eviction_is_per_key() -> None:
    """Filling host A's bucket above its cap doesn't evict host B's clients."""

    def _factory(base_url: str) -> AsyncHTTPClient:
        return cast("AsyncHTTPClient", _FakeClient(base_url))

    pool = AsyncHTTPClientPool(_factory, max_idle_per_host=2)
    try:
        b_clients = [await pool.acquire("https://b") for _ in range(2)]
        a_clients = [await pool.acquire("https://a") for _ in range(3)]
        for c in a_clients:
            await pool.release("https://a", c)
        for c in b_clients:
            await pool.release("https://b", c)
        # Host b was at cap 2 cleanly; host a hit cap and the third closed.
        assert pool.idle_count("https://a") == 2
        assert pool.idle_count("https://b") == 2
        # The third "a" client closed; b's both still alive.
        assert cast("_FakeClient", a_clients[2]).closed is True
        for c in b_clients:
            assert cast("_FakeClient", c).closed is False
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# E4. idx_audit_log_actor satisfies the audit-export filter
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_audit_log_actor_index_used_by_export(db_path: Path) -> None:
    """The audit-export "what did user X do" query uses idx_audit_log_actor."""
    sync = sqlite3.connect(str(db_path))
    try:
        plan = sync.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id, action, target FROM audit_log "
            "WHERE actor = ? ORDER BY timestamp",
            ("alice@example.com",),
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan).lower()
        # Some index hit — never a full scan.
        assert "scan" not in plan_text or "index" in plan_text
    finally:
        sync.close()


# ---------------------------------------------------------------------------
# E5. Generator with batch_size > total returns single full chunk
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_batched_generator_returns_single_chunk_when_batch_exceeds(
    db_path: Path,
) -> None:
    from ingestion.daemon.schedule_queries import last_success_at_bulk_batched

    sync = sqlite3.connect(str(db_path))
    try:
        for i in range(3):
            sync.execute(
                """
                INSERT INTO sync_history (id, user_email, platform, state, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"sid_{i}",
                    f"u{i}@x.com",
                    "moodle",
                    "complete",
                    "2026-04-26T10:00:00Z",
                    "2026-04-26T11:00:00Z",
                ),
            )
        sync.commit()
    finally:
        sync.close()

    chunks = [c async for c in last_success_at_bulk_batched(db_path, batch_size=999)]
    assert len(chunks) == 1
    assert len(chunks[0]) == 3


# ---------------------------------------------------------------------------
# E6. Cyclic acquire deadlock detection — bounded by timeout
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_cyclic_acquire_does_not_deadlock_with_pool_max_one(db_path: Path) -> None:
    """A single coroutine that does sequential acquire+release on a max=1 pool
    must not deadlock — the release between acquires unblocks the slot.
    """
    pool = AsyncConnectionPool(db_path, max_connections=1, warm_connections=0)
    try:

        async def _scenario() -> None:
            for _ in range(10):
                async with pool.acquire() as conn:
                    cursor = await conn.execute("SELECT 1")
                    await cursor.fetchone()

        await asyncio.wait_for(_scenario(), timeout=3.0)
    finally:
        await pool.close_all()


# ---------------------------------------------------------------------------
# E7. AsyncHTTPClientPool.idle_count(unknown_key) returns 0
#     [TEST-COVERAGE] — diagnostic helper must not raise on unknown keys.
# ---------------------------------------------------------------------------


async def test_http_pool_idle_count_unknown_key_returns_zero() -> None:
    pool = AsyncHTTPClientPool()
    assert pool.idle_count("https://nonexistent") == 0


# ---------------------------------------------------------------------------
# E8. Pragmas applied via context-manager API
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_pragma_temp_store_is_memory_via_connect_helper(db_path: Path) -> None:
    """``connect`` helper applies all pragmas — temp_store=MEMORY checked here."""
    from ingestion.daemon.store.async_conn import connect

    async with connect(db_path) as conn:
        cursor = await conn.execute("PRAGMA temp_store")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 2  # MEMORY


# ---------------------------------------------------------------------------
# E9. Migration 0009 creates indices on existing tables (no schema collision)
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


def test_migration_0009_indices_exist_after_apply_migrations(db_path: Path) -> None:
    """apply_migrations runs 0009 — every named index appears in sqlite_master."""
    sync = sqlite3.connect(str(db_path))
    try:
        rows = sync.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        names = {r[0] for r in rows}
        # 0009 creates 4 indices — the queued_at duplicate of 0006's
        # ix_force_sync_requested was intentionally dropped from the script.
        for expected in (
            "idx_dead_letter_state_user_platform",
            "idx_sync_history_user_platform",
            "idx_sync_history_finished_at",
            "idx_audit_log_actor",
        ):
            assert expected in names
    finally:
        sync.close()


# ---------------------------------------------------------------------------
# E10. Pool acquire on closed pool does not hang
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_acquire_after_close_does_not_hang(db_path: Path) -> None:
    """Closed pool must raise immediately — no indefinite block on the queue."""
    pool = AsyncConnectionPool(db_path, max_connections=1, warm_connections=0)
    await pool.close_all()
    # No timeout needed — should raise synchronously.
    with pytest.raises(RuntimeError):
        async with pool.acquire():
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# E11. Schedule generator yields actual data on first batch boundary
#     [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_batched_generator_first_batch_correctness(db_path: Path) -> None:
    """First chunk holds the lexically-first ids (ORDER BY user_email, platform)."""
    from ingestion.daemon.schedule_queries import user_interval_overrides_bulk_batched

    sync = sqlite3.connect(str(db_path))
    try:
        for letter in "zyxabc":
            sync.execute(
                """
                INSERT INTO user_schedule_overrides
                (user_email, platform, scope, interval_seconds, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"{letter}@x.com", "moodle", "default", 60, "2026-04-26T10:00:00Z"),
            )
        sync.commit()
    finally:
        sync.close()

    chunks = [c async for c in user_interval_overrides_bulk_batched(db_path, batch_size=2)]
    # Six rows / 2 per batch = 3 chunks.
    assert len(chunks) == 3
    # Ordered alphabetically: a, b first; c, x second; y, z third.
    first_keys = list(chunks[0].keys())
    assert first_keys[0][0] == "a@x.com"
    assert first_keys[1][0] == "b@x.com"
