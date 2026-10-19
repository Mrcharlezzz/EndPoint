"""SQLite helpers for scheduler decisions: last-success, interval overrides.

Both tables the scheduler reads (``sync_history`` for last-success timestamps
and ``user_schedule_overrides`` for per-user cadence) live in the same
aiosqlite file as everything else. These helpers are narrow on purpose so
the scheduler module stays focused on orchestration.

Bulk variants: the original ``last_success_at_bulk`` and
``user_interval_overrides_bulk`` materialise every row in memory before
returning, which is fine for the daemon's current 100-200 user range but
spikes RAM linearly past 10k. The ``*_batched`` variants yield batches
under a single transaction — callers iterate page-by-page and the helper
holds one connection for the whole pass so the batches read a snapshot
rather than a live table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


async def last_success_at(
    db_path: Path,
    user_email: str,
    platform: str,
) -> datetime | None:
    """Most recent completed sync for this (user, platform), or ``None``."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT finished_at FROM sync_history
            WHERE user_email = ? AND platform = ? AND state = 'complete'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (user_email, platform),
        )
        row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return datetime.fromisoformat(row[0])


async def last_success_at_bulk(
    db_path: Path,
) -> dict[tuple[str, str], datetime]:
    """Most recent complete sync for every (user, platform) in one query.

    Equivalent to calling :func:`last_success_at` for every pair, but with
    a single SELECT and a single connection — the scheduler calls this
    once per tick instead of issuing N queries inside the candidate loop.
    Pairs that never completed a sync are simply absent from the dict.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, MAX(finished_at)
            FROM sync_history
            WHERE state = 'complete' AND finished_at IS NOT NULL
            GROUP BY user_email, platform
            """,
        )
        rows = await cursor.fetchall()
    out: dict[tuple[str, str], datetime] = {}
    for user_email, platform, finished_at in rows:
        if finished_at is None:
            continue
        out[(str(user_email), str(platform))] = datetime.fromisoformat(finished_at)
    return out


async def user_interval_overrides_bulk(
    db_path: Path,
    scope: str = "default",
) -> dict[tuple[str, str], int]:
    """Every per-user interval override for ``scope`` in one query.

    Equivalent to calling :func:`user_interval_override` for every pair;
    keys are (user_email, platform). Pairs without an override are
    absent from the dict so the scheduler can fall back to the platform
    or global default.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, interval_seconds
            FROM user_schedule_overrides
            WHERE scope = ?
            """,
            (scope,),
        )
        rows = await cursor.fetchall()
    return {
        (str(user_email), str(platform)): int(interval_seconds)
        for user_email, platform, interval_seconds in rows
    }


# Default batch size for the paginated variants. Picked to keep each batch
# under a few MB of memory for typical row sizes while limiting the number
# of round-trips on a 10k-row scan.
_BATCH_SIZE = 500


async def last_success_at_bulk_batched(
    db_path: Path,
    *,
    batch_size: int = _BATCH_SIZE,
) -> AsyncIterator[dict[tuple[str, str], datetime]]:
    """Yield the same data as :func:`last_success_at_bulk` in batches.

    Holds one connection for the whole iteration so every batch reads a
    consistent snapshot — a row inserted after the first batch is yielded
    will not appear in a later batch. Callers iterate with ``async for``
    and accumulate or process batch-by-batch:

        async for chunk in last_success_at_bulk_batched(db_path):
            for (user, platform), ts in chunk.items():
                ...

    The non-batched variant remains for callers that need the full
    materialised dict (small daemons today). Past 10k users the batched
    iterator keeps memory bounded.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, MAX(finished_at)
            FROM sync_history
            WHERE state = 'complete' AND finished_at IS NOT NULL
            GROUP BY user_email, platform
            ORDER BY user_email, platform
            """,
        )
        while True:
            rows = await cursor.fetchmany(batch_size)
            if not rows:
                break
            chunk: dict[tuple[str, str], datetime] = {}
            for user_email, platform, finished_at in rows:
                if finished_at is None:
                    continue
                chunk[(str(user_email), str(platform))] = datetime.fromisoformat(finished_at)
            if chunk:
                yield chunk


async def user_interval_overrides_bulk_batched(
    db_path: Path,
    scope: str = "default",
    *,
    batch_size: int = _BATCH_SIZE,
) -> AsyncIterator[dict[tuple[str, str], int]]:
    """Yield the same data as :func:`user_interval_overrides_bulk` in batches.

    Same snapshot semantics as :func:`last_success_at_bulk_batched`: one
    connection per pagination call, so concurrent INSERT / DELETEs against
    ``user_schedule_overrides`` mid-iteration are not visible until the
    iterator is restarted.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, interval_seconds
            FROM user_schedule_overrides
            WHERE scope = ?
            ORDER BY user_email, platform
            """,
            (scope,),
        )
        while True:
            rows = await cursor.fetchmany(batch_size)
            if not rows:
                break
            chunk = {
                (str(user_email), str(platform)): int(interval_seconds)
                for user_email, platform, interval_seconds in rows
            }
            if chunk:
                yield chunk


async def stored_cursor_scopes(
    db_path: Path,
) -> dict[tuple[str, str], set[str]]:
    """Every scope that has a persisted cursor, grouped by (user, platform).

    Used by the scheduler to detect scopes that have been removed from
    the current ``scope_for_platform`` configuration: the old cursor
    lingers in ``sync_cursors`` but no new run will ever advance it. We
    log a warning so operators notice the dead-weight row; actual
    cleanup is deferred until the scheduler grows real per-scope
    buckets (tracked as a known limitation in Phase 2's exit criteria).
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT user_email, platform, scope FROM sync_cursors",
        )
        rows = await cursor.fetchall()
    out: dict[tuple[str, str], set[str]] = {}
    for user_email, platform, scope in rows:
        out.setdefault((str(user_email), str(platform)), set()).add(str(scope))
    return out


async def sync_state_by_id(
    db_path: Path,
    sync_id: str,
) -> str | None:
    """Outcome of a specific sync run by primary key.

    Returns one of ``'running'``, ``'complete'``, ``'failed'`` or ``None`` if
    the row is missing. Keyed on ``sync_id`` to avoid the race that
    ``ORDER BY started_at DESC LIMIT 1`` has under clock skew or
    overlapping retries: the caller must know which run it wants to
    inspect.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT state FROM sync_history WHERE id = ?",
            (sync_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


async def user_interval_override(
    db_path: Path,
    user_email: str,
    platform: str,
    scope: str = "default",
) -> int | None:
    """Per-user sync interval in seconds, or ``None`` if no override."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT interval_seconds FROM user_schedule_overrides
            WHERE user_email = ? AND platform = ? AND scope = ?
            """,
            (user_email, platform, scope),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return int(row[0])


async def set_user_interval_override(
    db_path: Path,
    user_email: str,
    platform: str,
    interval_seconds: int,
    scope: str = "default",
) -> None:
    """Write (or replace) a per-user interval override."""
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be >= 1")
    now = datetime.now(tz=UTC).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO user_schedule_overrides (
                user_email, platform, scope, interval_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_email, platform, scope) DO UPDATE SET
                interval_seconds = excluded.interval_seconds,
                updated_at = excluded.updated_at
            """,
            (user_email, platform, scope, interval_seconds, now),
        )
        await conn.commit()


async def clear_user_interval_override(
    db_path: Path,
    user_email: str,
    platform: str,
    scope: str = "default",
) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            DELETE FROM user_schedule_overrides
            WHERE user_email = ? AND platform = ? AND scope = ?
            """,
            (user_email, platform, scope),
        )
        await conn.commit()


async def list_all_overrides(db_path: Path) -> list[dict[str, str | int]]:
    """Every row in ``user_schedule_overrides``. Read-only, ordered for stable UI.

    Used by the config-overrides REST surface so the dashboard can render the
    full table in one request. Ordering is (user_email, platform, scope) so a
    user with multiple platform/scope combinations renders grouped together.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, scope, interval_seconds, updated_at
            FROM user_schedule_overrides
            ORDER BY user_email, platform, scope
            """,
        )
        rows = await cursor.fetchall()
    return [
        {
            "user_email": str(user_email),
            "platform": str(platform),
            "scope": str(scope),
            "interval_seconds": int(interval_seconds),
            "updated_at": str(updated_at),
        }
        for user_email, platform, scope, interval_seconds, updated_at in rows
    ]


async def list_overrides_for_email(db_path: Path, user_email: str) -> list[dict[str, str | int]]:
    """Every override row for a single user across platforms and scopes.

    The table's PK is composite ``(user_email, platform, scope)`` so one
    email can legitimately have N rows. The router returns the list as-is
    rather than collapsing to a single object — each combination is an
    independently editable policy.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, scope, interval_seconds, updated_at
            FROM user_schedule_overrides
            WHERE user_email = ?
            ORDER BY platform, scope
            """,
            (user_email,),
        )
        rows = await cursor.fetchall()
    return [
        {
            "user_email": str(user_email_),
            "platform": str(platform),
            "scope": str(scope),
            "interval_seconds": int(interval_seconds),
            "updated_at": str(updated_at),
        }
        for user_email_, platform, scope, interval_seconds, updated_at in rows
    ]


async def get_override_row(
    db_path: Path,
    user_email: str,
    platform: str,
    scope: str = "default",
) -> dict[str, str | int] | None:
    """Single override row keyed on the full composite PK, or ``None``."""
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT user_email, platform, scope, interval_seconds, updated_at
            FROM user_schedule_overrides
            WHERE user_email = ? AND platform = ? AND scope = ?
            """,
            (user_email, platform, scope),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "user_email": str(row[0]),
        "platform": str(row[1]),
        "scope": str(row[2]),
        "interval_seconds": int(row[3]),
        "updated_at": str(row[4]),
    }


async def clear_overrides_for_email(db_path: Path, user_email: str) -> int:
    """Delete every override row for an email. Returns rows deleted.

    Used by ``DELETE /api/config/overrides/{email}`` to wipe all of a
    user's scheduling policies in one call. Zero return means no-op so
    the router can map that to 404.
    """
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "DELETE FROM user_schedule_overrides WHERE user_email = ?",
            (user_email,),
        )
        await conn.commit()
        return cursor.rowcount or 0
