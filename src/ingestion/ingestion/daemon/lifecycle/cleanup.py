"""Cascade-delete + TTL prune for daemon-owned tables.

Five tables hold per-user state outside Postgres: ``sync_history``,
``dead_letter_state``, ``sync_cursors``, ``force_sync_requests``, and
``audit_log``. Without explicit cleanup, removing a user from the
presentation_api side leaves daemon rows orphaned forever.

All daemon tables share the SQLite file managed by
:func:`ingestion.daemon.store.local.apply_migrations`. There are no
foreign-key cascades into these tables (the Postgres ``user_email``
column lives in a separate database), so cleanup must be explicit.

Single-transaction semantics: :func:`cascade_delete_user` runs every
DELETE inside one transaction so a partial failure rolls the whole
operation back. The loops use independent passes — a single bad prune
shouldn't block the others.

Audit policy: ``audit_log`` rows for a deleted user are KEPT by
default for compliance. Pass ``include_audit=True`` to drop them too.

Worker-pool cleanup: when a worker pool is supplied, the matching
per-user semaphore is popped from the pool's dict so a re-added user
under the same email starts with a fresh per-user concurrency cap
(addresses the slice-10 deferral pinned in
:mod:`ingestion.daemon.worker`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from ingestion.daemon.worker import WorkerPool

# Default retention windows (days). Operators can override these via
# ``DaemonConfig.lifecycle.retention_days_*`` env / overlay knobs.
DEFAULT_SYNC_HISTORY_RETENTION_DAYS = 30
DEFAULT_DEAD_LETTER_RETENTION_DAYS = 90
DEFAULT_AUDIT_LOG_RETENTION_DAYS = 365

# (channel, payload) -> None. Matches EventBus.publish_threadsafe so
# runtime composition can pass a bound bus method directly. Kept as a
# bare callable rather than a Protocol so tests can substitute a list-
# append closure without ceremony.
type EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class CascadeDeleteResult:
    """Per-table row counts deleted in one cascade-delete pass.

    ``audit_deleted`` is ``0`` unless the caller passed
    ``include_audit=True`` — by default the audit trail outlives the
    user for compliance reasons.
    """

    sync_history_deleted: int
    dead_letter_deleted: int
    sync_cursors_deleted: int
    force_sync_deleted: int
    audit_deleted: int

    @property
    def total(self) -> int:
        return (
            self.sync_history_deleted
            + self.dead_letter_deleted
            + self.sync_cursors_deleted
            + self.force_sync_deleted
            + self.audit_deleted
        )


# Tables wiped by cascade_delete_user. Order is child-first so a future
# FK from sync_history -> sync_cursors (or similar) honors the parent
# row's existence at delete time. Today no FKs exist between these
# tables, but the order is the safer default.
_CASCADE_TABLES: tuple[tuple[str, str], ...] = (
    ("sync_history", "user_email"),
    ("dead_letter_state", "user_email"),
    ("force_sync_requests", "user_email"),
    ("sync_cursors", "user_email"),
)


async def cascade_delete_user(
    db_path: Path,
    *,
    email: str,
    include_audit: bool = False,
    worker_pool: WorkerPool | None = None,
    event_sink: EventSink | None = None,
) -> CascadeDeleteResult:
    """Delete every daemon-owned row for ``email`` in one transaction.

    Returns a :class:`CascadeDeleteResult` carrying per-table counts.
    Unknown email is a no-op — every count is zero, no error raised
    (callers may chain this after a presentation_api delete-user
    response without worrying about a missing daemon-side row).

    ``include_audit=True`` drops ``audit_log`` rows targeting the
    user too. Default is False because the audit trail typically
    outlives the user record for compliance.

    ``worker_pool`` (if supplied) gets ``remove_user`` called on it so
    the per-user semaphore is freed alongside the SQL rows. Closes
    the slice-10 leak pin on
    :class:`ingestion.daemon.worker.WorkerPool._per_user_sems`.

    Emits a structured ``lifecycle.user_deleted`` event when
    ``event_sink`` is supplied, with the affected counts so the
    alert engine and dashboard can react in real time.
    """
    log = get_logger(__name__)
    counts: dict[str, int] = dict.fromkeys(
        ("sync_history", "dead_letter_state", "force_sync_requests", "sync_cursors"), 0
    )
    audit_deleted = 0
    async with aiosqlite.connect(db_path) as conn:
        # One transaction wraps every DELETE so a partial failure rolls
        # back. aiosqlite enters autocommit mode on connection; an
        # explicit BEGIN forces one transaction span. ROLLBACK lives in
        # the except branch — successful path commits at the end.
        await conn.execute("BEGIN")
        try:
            for table, column in _CASCADE_TABLES:
                cursor = await conn.execute(
                    f"DELETE FROM {table} WHERE {column} = ?",  # noqa: S608 — table+column from the static _CASCADE_TABLES tuple
                    (email,),
                )
                counts[table] = max(0, cursor.rowcount)
            if include_audit:
                cursor = await conn.execute(
                    # ``target`` is the conventional user identifier in
                    # the audit log (user_email or user_email/platform).
                    # Match both the bare email and the per-platform
                    # composite so a credential.pause audit row also
                    # gets dropped when the user is wholly removed.
                    "DELETE FROM audit_log WHERE target = ? OR target LIKE ?",
                    (email, f"{email}/%"),
                )
                audit_deleted = max(0, cursor.rowcount)
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            raise

    if worker_pool is not None:
        worker_pool.remove_user(email)

    result = CascadeDeleteResult(
        sync_history_deleted=counts["sync_history"],
        dead_letter_deleted=counts["dead_letter_state"],
        sync_cursors_deleted=counts["sync_cursors"],
        force_sync_deleted=counts["force_sync_requests"],
        audit_deleted=audit_deleted,
    )
    log.info(
        "lifecycle.user_deleted",
        email=email,
        sync_history_deleted=result.sync_history_deleted,
        dead_letter_deleted=result.dead_letter_deleted,
        sync_cursors_deleted=result.sync_cursors_deleted,
        force_sync_deleted=result.force_sync_deleted,
        audit_deleted=result.audit_deleted,
    )
    if event_sink is not None:
        event_sink(
            "lifecycle",
            {
                "event": "lifecycle.user_deleted",
                "email": email,
                "sync_history_deleted": result.sync_history_deleted,
                "dead_letter_deleted": result.dead_letter_deleted,
                "sync_cursors_deleted": result.sync_cursors_deleted,
                "force_sync_deleted": result.force_sync_deleted,
                "audit_deleted": result.audit_deleted,
            },
        )
    return result


async def prune_sync_history(
    db_path: Path,
    *,
    retention_days: int = DEFAULT_SYNC_HISTORY_RETENTION_DAYS,
    batch_size: int = 10_000,
) -> int:
    """Delete finished ``sync_history`` rows older than ``retention_days``.

    Returns the number of rows deleted. Only rows in a terminal state
    (``complete``, ``failed``, ``cancelled``) are eligible — running
    syncs MUST survive the prune even if they outlived the cutoff,
    otherwise a multi-day backfill would lose its history mid-run.

    ``retention_days=0`` is a documented kill switch: every terminal
    row is deleted immediately. Useful for tests and one-off cleanups
    after a botched migration.
    """
    return await _prune_table(
        db_path,
        sql=(
            "DELETE FROM sync_history WHERE id IN ("
            "SELECT id FROM sync_history "
            "WHERE state != 'running' AND finished_at IS NOT NULL "
            "AND finished_at < ? LIMIT ?)"
        ),
        retention_days=retention_days,
        batch_size=batch_size,
    )


async def prune_dead_letter(
    db_path: Path,
    *,
    retention_days: int = DEFAULT_DEAD_LETTER_RETENTION_DAYS,
    batch_size: int = 10_000,
) -> int:
    """Delete cleared ``dead_letter_state`` rows older than ``retention_days``.

    A "cleared" row is one with ``consecutive_failures = 0`` AND no
    suppression window — those rows carry no operational signal but
    still take up space. Active suppressions and rows with non-zero
    failure counters are NEVER pruned, regardless of age, so an
    abandoned-but-still-suppressed user surfaces forever in
    ``/api/errors``.

    No timestamp column is required — the row's age is implicit from
    ``last_failure_at`` (NULL for cleared rows). To track age we
    introduce an implicit ``cleared_since`` filter: rows with
    ``last_failure_at IS NULL`` count as "cleared right now"; the
    prune is conservative and uses ``last_failure_at < cutoff`` with
    NULL excluded, so it only removes rows where the last failure
    itself was old AND the current state is clear. Everything else
    survives until an operator clears it manually.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    total = 0
    async with aiosqlite.connect(db_path) as conn:
        while True:
            cursor = await conn.execute(
                """
                DELETE FROM dead_letter_state
                WHERE rowid IN (
                    SELECT rowid FROM dead_letter_state
                    WHERE consecutive_failures = 0
                      AND suppressed_until IS NULL
                      AND last_failure_at IS NOT NULL
                      AND last_failure_at < ?
                    LIMIT ?
                )
                """,
                (cutoff, batch_size),
            )
            await conn.commit()
            rowcount = max(0, cursor.rowcount)
            if rowcount <= 0:
                break
            total += rowcount
            if rowcount < batch_size:
                break
    return total


async def prune_audit_log(
    db_path: Path,
    *,
    retention_days: int = DEFAULT_AUDIT_LOG_RETENTION_DAYS,
    batch_size: int = 10_000,
) -> int:
    """Delete ``audit_log`` rows older than ``retention_days``.

    Default retention is 1 year — long enough for compliance audits,
    short enough that a multi-year-old daemon doesn't carry millions
    of rows from since-deleted users. Operators with stricter
    requirements can lower the window via the lifecycle config.
    """
    return await _prune_table(
        db_path,
        sql=(
            "DELETE FROM audit_log WHERE id IN ("
            "SELECT id FROM audit_log WHERE timestamp < ? LIMIT ?)"
        ),
        retention_days=retention_days,
        batch_size=batch_size,
    )


async def _prune_table(
    db_path: Path,
    *,
    sql: str,
    retention_days: int,
    batch_size: int,
) -> int:
    """Shared batched-DELETE driver for the timestamp-based prunes.

    Each batch commits independently so a long prune doesn't hold the
    write lock for the whole operation. Stops when a batch returns
    fewer than ``batch_size`` rows (the tail) or zero rows (already
    drained).
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    total = 0
    async with aiosqlite.connect(db_path) as conn:
        while True:
            cursor = await conn.execute(sql, (cutoff, batch_size))
            await conn.commit()
            rowcount = max(0, cursor.rowcount)
            if rowcount <= 0:
                break
            total += rowcount
            if rowcount < batch_size:
                break
    return total


async def cleanup_stale_cursors(
    db_path: Path,
    *,
    platform: str,
    current_scopes: frozenset[str],
    event_sink: EventSink | None = None,
) -> int:
    """Drop ``sync_cursors`` rows for scopes no longer in the active config.

    When an operator removes a scope from a platform's
    ``scope_for_platform`` resolution (or the platform module narrows
    its FetchScope set), the previously-stored cursor lingers
    indefinitely — :class:`ingestion.daemon.scheduler.Scheduler` only
    warns about it (see ``scheduler.scope_vanished``), it doesn't act.

    This function is the explicit cleanup pass: it deletes every
    ``sync_cursors`` row for ``platform`` whose ``scope`` is not in
    ``current_scopes``. The pipeline's "default" bucket is preserved
    only if it appears in ``current_scopes`` — callers that want it
    to survive must include the literal string ``"default"`` (or pass
    a frozenset with all the active scopes plus ``"default"``).

    ``current_scopes=frozenset()`` is the documented kill switch:
    every cursor for the platform is deleted. Use with care.

    Returns the count of rows deleted. Emits a
    ``lifecycle.cursor_pruned`` event per prune pass (one row per
    affected scope) so the alert engine sees the transition.
    """
    log = get_logger(__name__)
    deleted_total = 0
    async with aiosqlite.connect(db_path) as conn:
        # Collect distinct stale scopes first so the event sink can
        # name each scope pruned. Keeps the SQL DELETE simple and
        # avoids a streaming row-by-row delete loop.
        cursor = await conn.execute(
            "SELECT DISTINCT scope FROM sync_cursors WHERE platform = ?",
            (platform,),
        )
        stored_scopes = {str(row[0]) for row in await cursor.fetchall()}
        stale = stored_scopes - set(current_scopes)
        if not stale:
            return 0
        # Param-list the IN clause so the scope strings flow through
        # placeholders (not interpolated SQL).
        placeholders = ",".join("?" for _ in stale)
        delete_cursor = await conn.execute(
            f"DELETE FROM sync_cursors WHERE platform = ? AND scope IN ({placeholders})",  # noqa: S608 — placeholders count is bounded by the stale set
            (platform, *stale),
        )
        deleted_total = max(0, delete_cursor.rowcount)
        await conn.commit()
    for scope_name in stale:
        log.info(
            "lifecycle.cursor_pruned",
            platform=platform,
            scope=scope_name,
        )
        if event_sink is not None:
            event_sink(
                "lifecycle",
                {
                    "event": "lifecycle.cursor_pruned",
                    "platform": platform,
                    "scope": scope_name,
                },
            )
    return deleted_total


# Re-export for typing / convenience. Kept narrow to the two callable
# shapes the lifecycle loops + cascade hook need.
type AsyncPruneFn = Callable[[], Awaitable[int]]
