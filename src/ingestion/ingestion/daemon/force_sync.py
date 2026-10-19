"""SQLite helpers for the force-sync request queue.

The API inserts a row; the scheduler reads pending rows every tick and
submits them regardless of the interval-due check. Rows are deleted once
the scheduler has submitted the sync — a second request for the same
(user, platform) is a straight upsert so the operator can retry without
having to wait for the scheduler to pick up the first one.

Each helper accepts an optional :class:`AsyncConnectionPool` so the runtime
can route writes through a shared pool. With no pool the helpers fall back
to a fresh ``aiosqlite.connect`` per call, preserving the existing
behaviour for callers (and tests) that have not adopted the pool yet.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from ingestion.daemon.store.conn_pool import AsyncConnectionPool


@asynccontextmanager
async def _conn(
    db_path: Path,
    pool: AsyncConnectionPool | None,
) -> AsyncIterator[aiosqlite.Connection]:
    if pool is not None:
        async with pool.acquire() as conn:
            yield conn
        return
    async with aiosqlite.connect(db_path) as conn:
        yield conn


async def enqueue_force_sync(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    reason: str | None = None,
    pool: AsyncConnectionPool | None = None,
) -> None:
    """Insert (or replace) a force-sync request for (user, platform)."""
    now = datetime.now(tz=UTC).isoformat()
    async with _conn(db_path, pool) as conn:
        await conn.execute(
            """
            INSERT INTO force_sync_requests (user_email, platform, requested_at, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_email, platform) DO UPDATE SET
                requested_at = excluded.requested_at,
                reason = excluded.reason
            """,
            (user_email, platform, now, reason),
        )
        await conn.commit()


async def list_force_sync_pending(
    db_path: Path,
    *,
    pool: AsyncConnectionPool | None = None,
) -> list[tuple[str, str]]:
    """Return every pending (user_email, platform) force-sync request."""
    async with _conn(db_path, pool) as conn:
        cursor = await conn.execute(
            "SELECT user_email, platform FROM force_sync_requests ORDER BY requested_at"
        )
        rows = await cursor.fetchall()
    return [(row[0], row[1]) for row in rows]


async def force_sync_queue_depth(
    db_path: Path,
    *,
    pool: AsyncConnectionPool | None = None,
) -> int:
    """Return the current row count in ``force_sync_requests``.

    Used by the backpressure check in :mod:`sync_service` to refuse new
    inserts past ``api.max_force_sync_queue_depth``. ``COUNT(*)`` is
    fine here — the table is bounded by the cap itself plus inflight
    drainage by the scheduler, so the index scan cost stays in the
    single-digit milliseconds even at the 10k cap default.
    """
    async with _conn(db_path, pool) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM force_sync_requests")
        row = await cursor.fetchone()
    if row is None:
        return 0
    value = row[0]
    return int(value) if value is not None else 0


async def consume_force_sync(
    db_path: Path,
    user_email: str,
    platform: str,
    *,
    pool: AsyncConnectionPool | None = None,
) -> None:
    """Delete the request row for (user, platform). Called by the scheduler
    after it has submitted the forced sync."""
    async with _conn(db_path, pool) as conn:
        await conn.execute(
            "DELETE FROM force_sync_requests WHERE user_email = ? AND platform = ?",
            (user_email, platform),
        )
        await conn.commit()
