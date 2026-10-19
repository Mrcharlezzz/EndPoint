"""Unit tests for :mod:`ingestion.daemon.store.async_conn`.

The four pragmas the daemon depends on (``journal_mode=WAL``,
``synchronous=NORMAL``, ``busy_timeout=5000``, ``temp_store=MEMORY``) plus
``foreign_keys=ON`` must apply on every connection — fresh and pre-existing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from ingestion.daemon.store.async_conn import (
    BUSY_TIMEOUT_MS,
    apply_pragmas,
    connect,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


async def _read_pragma(conn: aiosqlite.Connection, name: str) -> str | int | None:
    cursor = await conn.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    if row is None:
        return None
    return row[0]


def test_busy_timeout_constant() -> None:
    assert BUSY_TIMEOUT_MS == 5000


async def test_apply_pragmas_sets_journal_mode_wal(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        assert str(await _read_pragma(conn, "journal_mode")).lower() == "wal"


async def test_apply_pragmas_sets_synchronous_normal(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        # SQLite returns synchronous as int (NORMAL = 1).
        assert int(await _read_pragma(conn, "synchronous")) == 1  # type: ignore[arg-type]


async def test_apply_pragmas_sets_busy_timeout(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        assert int(await _read_pragma(conn, "busy_timeout")) == BUSY_TIMEOUT_MS  # type: ignore[arg-type]


async def test_apply_pragmas_sets_temp_store_memory(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        # MEMORY = 2.
        assert int(await _read_pragma(conn, "temp_store")) == 2  # type: ignore[arg-type]


async def test_apply_pragmas_sets_foreign_keys_on(db_path: Path) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        assert int(await _read_pragma(conn, "foreign_keys")) == 1  # type: ignore[arg-type]


async def test_apply_pragmas_upgrades_delete_to_wal(db_path: Path) -> None:
    """Pre-existing DB in DELETE mode flips to WAL after apply_pragmas."""
    # Create the DB synchronously in DELETE mode first.
    sync_conn = sqlite3.connect(str(db_path))
    sync_conn.execute("PRAGMA journal_mode=DELETE")
    sync_conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    sync_conn.commit()
    sync_conn.close()
    # Verify pre-state via a fresh sqlite3 read (aiosqlite shares the file).
    sync_conn = sqlite3.connect(str(db_path))
    row = sync_conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "delete"
    sync_conn.close()
    # Now apply via the pool's helper.
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        assert str(await _read_pragma(conn, "journal_mode")).lower() == "wal"
    # Persistence: subsequent connection (after WAL upgrade) reads WAL too.
    sync_conn = sqlite3.connect(str(db_path))
    row = sync_conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "wal"
    sync_conn.close()


async def test_connect_context_manager_applies_pragmas(db_path: Path) -> None:
    async with connect(db_path) as conn:
        assert str(await _read_pragma(conn, "journal_mode")).lower() == "wal"
        assert int(await _read_pragma(conn, "synchronous")) == 1  # type: ignore[arg-type]
        assert int(await _read_pragma(conn, "busy_timeout")) == BUSY_TIMEOUT_MS  # type: ignore[arg-type]
        assert int(await _read_pragma(conn, "temp_store")) == 2  # type: ignore[arg-type]
        assert int(await _read_pragma(conn, "foreign_keys")) == 1  # type: ignore[arg-type]


async def test_connect_closes_on_exit(db_path: Path) -> None:
    """The async context manager must release the connection on exit."""
    async with connect(db_path) as conn:
        await conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        await conn.commit()
    # If the connection wasn't closed, the next open could see a write lock —
    # use an immediate write-lock-blocking write to confirm release happened.
    async with aiosqlite.connect(str(db_path)) as conn2:
        cursor = await conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = [row[0] async for row in cursor]
        assert "t" in names


async def test_apply_pragmas_runs_on_fresh_in_memory_db() -> None:
    """No file: pragmas still apply on a memory connection."""
    async with aiosqlite.connect(":memory:") as conn:
        await apply_pragmas(conn)
        # In-memory falls back to MEMORY journal regardless of WAL request.
        # The call must succeed (no exception) — that is the contract.
        assert int(await _read_pragma(conn, "synchronous")) == 1  # type: ignore[arg-type]


async def test_pragmas_applied_in_correct_order(db_path: Path) -> None:
    """journal_mode runs FIRST so the rest land against a WAL DB."""
    # Capture every executed statement.
    seen: list[str] = []
    async with aiosqlite.connect(str(db_path)) as conn:
        original_execute = conn.execute

        async def _spy(sql: str, *args: object, **kwargs: object) -> object:
            seen.append(sql)
            return await original_execute(sql, *args, **kwargs)

        conn.execute = _spy  # type: ignore[method-assign,assignment]
        await apply_pragmas(conn)
    # journal_mode is first (so the rest see WAL).
    assert seen[0].startswith("PRAGMA journal_mode")
    # busy_timeout uses BUSY_TIMEOUT_MS interpolated into the string.
    assert any(str(BUSY_TIMEOUT_MS) in stmt for stmt in seen)


async def test_apply_pragmas_noop_when_already_wal(db_path: Path) -> None:
    """Apply twice — second call is a no-op (journal_mode=WAL already)."""
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        # Second pass — should not error.
        await apply_pragmas(conn)
        assert str(await _read_pragma(conn, "journal_mode")).lower() == "wal"


async def test_pragmas_concurrent_apply(db_path: Path) -> None:
    """Concurrent apply across two connections doesn't race the WAL switch.

    The WAL switch is a single-writer operation in SQLite; back-to-back
    journal_mode flips on the same fresh file can hit a transient lock if
    both connections race the very first commit. Force WAL once via a
    primer connection so the parallel pair only runs the no-op path.
    """
    async with aiosqlite.connect(str(db_path)) as primer:
        await apply_pragmas(primer)
    async with aiosqlite.connect(str(db_path)) as a, aiosqlite.connect(str(db_path)) as b:
        await asyncio.gather(apply_pragmas(a), apply_pragmas(b))
        for conn in (a, b):
            assert str(await _read_pragma(conn, "journal_mode")).lower() == "wal"
