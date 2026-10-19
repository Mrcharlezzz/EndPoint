"""SQLite-backed :class:`SyncLogger` against ``sync_history`` + ``sync_profiles``.

Each sync run inserts one row at ``log_start``, then updates to "complete" or
"failed" at end. ``log_complete`` also writes a companion row into
``sync_profiles`` with phase-timing aggregates; the detailed event trace
lives in the ``logs`` table (filter by the same sync_id). ``id`` is a UUID4
string so it round-trips through JSON APIs.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.daemon.store.async_conn import connect

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.schemas import SyncStats


STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"

# Discriminator values for sync_history.sync_kind. ``content_sync`` is the
# historical bucket (Moodle/Teams/Telegram pipelines); ``credential_refresh``
# covers the Teams silent-refresh loop so the V6 Credentials page's
# "Refresh activity" tab can query the same table.
KIND_CONTENT_SYNC = "content_sync"
KIND_CREDENTIAL_REFRESH = "credential_refresh"


class SqliteSyncLogger:
    """Append a ``sync_history`` row at start; patch + profile at end.

    ``log_start`` accepts an optional ``sync_kind`` so credential-refresh
    callers can piggyback onto this table. The Protocol (``ingestion.ports
    .SyncLogger``) stays unchanged — content-sync callers keep the historical
    two-arg call and implicitly land in the ``content_sync`` bucket.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def log_start(
        self,
        user_id: str,
        platform: str,
        *,
        sync_kind: str = KIND_CONTENT_SYNC,
    ) -> str:
        sync_id = str(uuid.uuid4())
        started_at = datetime.now(tz=UTC).isoformat()
        async with connect(self._db_path) as conn:
            await conn.execute(
                """
                INSERT INTO sync_history (id, user_email, platform, started_at, state, sync_kind)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sync_id, user_id, platform, started_at, STATE_RUNNING, sync_kind),
            )
            await conn.commit()
        return sync_id

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        finished_at = datetime.now(tz=UTC).isoformat()
        stats_json = json.dumps(stats.model_dump(mode="json"), separators=(",", ":"))
        per_scope_json = json.dumps(
            {k: v.model_dump(mode="json") for k, v in stats.per_scope_stats.items()},
            separators=(",", ":"),
        )
        async with connect(self._db_path) as conn:
            await conn.execute(
                """
                UPDATE sync_history
                SET state = ?, finished_at = ?, stats_json = ?
                WHERE id = ?
                """,
                (STATE_COMPLETE, finished_at, stats_json, sync_id),
            )
            # The profile row mirrors sync_history (user_email, platform) so
            # profile queries don't need to JOIN for common filters. Sourced
            # back from sync_history to avoid requiring callers to re-pass
            # user_email on the completion path.
            async with conn.execute(
                "SELECT user_email, platform FROM sync_history WHERE id = ?",
                (sync_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError(
                    f"log_complete called for sync_id {sync_id!r} with no sync_history row; "
                    "call log_start first"
                )
            await conn.execute(
                """
                INSERT INTO sync_profiles (
                    sync_id, user_email, platform,
                    total_duration_seconds,
                    api_call_duration_seconds,
                    normalization_duration_seconds,
                    items_fetched, items_new, items_updated, error_count,
                    per_scope_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sync_id) DO UPDATE SET
                    total_duration_seconds = excluded.total_duration_seconds,
                    api_call_duration_seconds = excluded.api_call_duration_seconds,
                    normalization_duration_seconds = excluded.normalization_duration_seconds,
                    items_fetched = excluded.items_fetched,
                    items_new = excluded.items_new,
                    items_updated = excluded.items_updated,
                    error_count = excluded.error_count,
                    per_scope_json = excluded.per_scope_json
                """,
                (
                    sync_id,
                    row[0],
                    row[1],
                    stats.duration_seconds,
                    stats.api_call_duration_seconds,
                    stats.normalization_duration_seconds,
                    stats.items_fetched,
                    stats.items_new,
                    stats.items_updated,
                    stats.errors,
                    per_scope_json,
                    finished_at,
                ),
            )
            await conn.commit()

    async def log_failure(self, sync_id: str, error: str) -> None:
        finished_at = datetime.now(tz=UTC).isoformat()
        async with connect(self._db_path) as conn:
            # Reset stats_json defensively: if a prior log_complete wrote stats
            # for this same sync_id (shouldn't happen in a clean flow), leaving
            # a stale payload behind would misreport the outcome.
            await conn.execute(
                """
                UPDATE sync_history
                SET state = ?, finished_at = ?, error = ?, stats_json = NULL
                WHERE id = ?
                """,
                (STATE_FAILED, finished_at, error, sync_id),
            )
            await conn.commit()

    async def reconcile_orphaned_running(self) -> int:
        """Mark rows stuck in 'running' as failed. Returns the count flipped.

        Called once on daemon startup before any worker runs. Any row with
        state='running' and finished_at IS NULL at boot is stale by
        definition — this process has not started a sync yet, so the prior
        process died before patching its row to a terminal state (SIGKILL,
        OOM, power loss, ``docker compose down -t 0``). Graceful SIGTERM is
        already covered by the existing shutdown path; this sweep is the
        safety net for hard kills.
        """
        finished_at = datetime.now(tz=UTC).isoformat()
        async with connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                UPDATE sync_history
                SET state = ?, finished_at = ?, error = ?
                WHERE state = ? AND finished_at IS NULL
                """,
                (
                    STATE_FAILED,
                    finished_at,
                    "interrupted: daemon restart",
                    STATE_RUNNING,
                ),
            )
            await conn.commit()
            return cursor.rowcount
