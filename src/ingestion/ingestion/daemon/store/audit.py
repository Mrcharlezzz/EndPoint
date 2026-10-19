"""Append-only audit log for control-panel mutations.

Every state-changing operation from the control panel (pause user, reset
cursor, edit config, clear dead-letter, force refresh) writes one row.
``before_json`` and ``after_json`` are snapshots of the affected record so
the UI can render a diff view without re-querying the source of truth.

The table is strictly append-only: no update, no delete. Retention for old
rows is handled by a separate hot/cold tiering job in phase 7.

Both write and read paths accept an optional :class:`AsyncConnectionPool` so
the runtime can route every audit insert through the shared pool, skipping
the per-call ``aiosqlite.connect`` open + pragma round-trip that previously
fired on every control-panel mutation.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
    """Yield a connection from the pool or a per-call ``aiosqlite.connect``.

    Same indirection as :class:`DeadLetterTracker._conn` — keeps the audit
    helpers focused on SQL while allowing pool injection from the runtime.
    """
    if pool is not None:
        async with pool.acquire() as conn:
            yield conn
        return
    async with aiosqlite.connect(db_path) as conn:
        yield conn


async def write_entry(
    db_path: Path,
    *,
    action: str,
    actor: str | None = None,
    target: str | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    pool: AsyncConnectionPool | None = None,
) -> int:
    """Append one audit row. Returns the inserted row id.

    ``actor`` is the admin username (or ``"system"`` for automated actions).
    ``target`` identifies what was mutated — typically a ``user_email`` or a
    dotted config key. ``before`` / ``after`` are optional JSON-serialisable
    snapshots.
    """
    timestamp = datetime.now(tz=UTC).isoformat()
    before_json = (
        json.dumps(before, separators=(",", ":"), default=str) if before is not None else None
    )
    after_json = (
        json.dumps(after, separators=(",", ":"), default=str) if after is not None else None
    )
    async with _conn(db_path, pool) as conn:
        cursor = await conn.execute(
            """
            INSERT INTO audit_log (
                timestamp, actor, action, target, before_json, after_json, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, actor, action, target, before_json, after_json, reason),
        )
        await conn.commit()
        inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("audit insert returned no rowid")
    return inserted_id


async def list_entries(
    db_path: Path,
    *,
    action: str | None = None,
    target: str | None = None,
    actor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read recent audit entries, newest first. Filters are AND-ed.

    ``since`` / ``until`` are ISO timestamps; inclusive of both endpoints.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if actor is not None:
        clauses.append("actor = ?")
        params.append(actor)
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                # ``id DESC`` is a tiebreaker for rows with equal timestamps
                # (wall-clock can jump back via NTP), so diff views remain
                # stable regardless of insert-time clock skew.
                f"""
                SELECT id, timestamp, actor, action, target, before_json, after_json, reason
                FROM audit_log
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            )
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "actor": row["actor"],
                "action": row["action"],
                "target": row["target"],
                "before": _maybe_json(row["before_json"]),
                "after": _maybe_json(row["after_json"]),
                "reason": row["reason"],
            }
        )
    return results


def _maybe_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
