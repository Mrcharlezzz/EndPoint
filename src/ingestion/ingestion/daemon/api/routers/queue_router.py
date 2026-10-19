"""/api/force-sync-queue — diagnostics + cancellation for the force-sync queue.

The force-sync queue (table ``force_sync_requests``) sits between the
``POST /api/actions/users/{email}/{platform}/force_sync`` endpoint and the
scheduler: requests land here, the scheduler reads them on its next tick,
submits the sync, and deletes the row. While a row sits pending an operator
can either watch it via ``GET /api/force-sync-queue`` or cancel it via
``DELETE /api/force-sync-queue/{email}/{platform}``.

The cancel endpoint races the scheduler: if the scheduler picks up the row
between the API request arriving and the DELETE landing, the resulting state
is "sync already running" — we surface that as 409 ``state_conflict`` rather
than a confusing 404 (the row was there a moment ago) or a misleading 200
(we did nothing useful).

Reading the queue does NOT require any audit trail. Cancelling a pending
request DOES — the audit row pins who pulled the rug out from under a
queued sync.
"""

from __future__ import annotations

from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.services.audit_writer import SqliteAuditWriter

router = APIRouter(prefix="/force-sync-queue", tags=["queue"])


class ForceSyncQueueItem(BaseModel):
    """One row from ``force_sync_requests`` decoded for the wire.

    ``requested_by_actor`` is best-effort: the table stores ``reason`` but
    not the actor identity. We surface the ``reason`` field under its
    own column AND the most-recently-written audit row's ``actor`` for
    the matching ``sync.force_requested`` action so the dashboard can
    render "operator X queued this Y minutes ago". The audit join is a
    LEFT JOIN — a row without a matching audit entry (somehow inserted
    out-of-band) still lists with ``requested_by_actor=None``.
    """

    model_config = ConfigDict(extra="forbid")

    user_email: str
    platform: str
    queued_at: str
    reason: str | None
    requested_by_actor: str | None


class ForceSyncQueueList(BaseModel):
    """Wrapper around :class:`ForceSyncQueueItem` for OpenAPI naming."""

    model_config = ConfigDict(extra="forbid")

    items: list[ForceSyncQueueItem]


class CancelForceSyncResult(BaseModel):
    """Outcome of the cancel-pending-force-sync endpoint."""

    model_config = ConfigDict(extra="forbid")

    cancelled: bool
    user_email: str
    platform: str


@router.get("", response_model=ForceSyncQueueList)
async def list_force_sync_queue(ctx: ContextDep) -> ForceSyncQueueList:
    """Every pending force-sync request in submission order.

    Empty list when nothing is queued (NOT 404 — empty is a legitimate
    steady state). The query LEFT JOINs against the audit log so
    operators see who queued each row without a second round-trip.
    """
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        # The LEFT JOIN picks the latest audit row (by id, monotonic
        # autoincrement) whose target string matches the canonical
        # ``{email}/{platform}`` we use everywhere. A row with no
        # matching audit entry surfaces actor=NULL.
        rows = await (
            await conn.execute(
                """
                SELECT
                    f.user_email AS user_email,
                    f.platform   AS platform,
                    f.requested_at AS requested_at,
                    f.reason     AS reason,
                    (
                        SELECT a.actor
                        FROM audit_log a
                        WHERE a.action = 'sync.force_requested'
                          AND a.target = f.user_email || '/' || f.platform
                        ORDER BY a.id DESC
                        LIMIT 1
                    ) AS actor
                FROM force_sync_requests f
                ORDER BY f.requested_at ASC
                """,
            )
        ).fetchall()
    items = [
        ForceSyncQueueItem(
            user_email=str(row["user_email"]),
            platform=str(row["platform"]),
            queued_at=str(row["requested_at"]),
            reason=(str(row["reason"]) if row["reason"] is not None else None),
            requested_by_actor=(str(row["actor"]) if row["actor"] is not None else None),
        )
        for row in rows
    ]
    return ForceSyncQueueList(items=items)


@router.delete("/{email}/{platform}", response_model=CancelForceSyncResult)
async def cancel_pending_force_sync(
    email: Annotated[str, Path(min_length=1)],
    platform: Annotated[str, Path(min_length=1)],
    ctx: ContextDep,
    request: Request,
) -> CancelForceSyncResult:
    """Delete a pending force-sync request before the scheduler picks it up.

    Status codes:

    * 200 + ``cancelled=True`` when a pending row was deleted.
    * 404 ``not_found`` when no pending row exists (never queued, or
      the scheduler already drained it AND the sync is no longer running).
    * 409 ``state_conflict`` when the row is gone but a sync for the
      same (email, platform) pair is currently running — meaning the
      scheduler picked the row up between the request arriving and our
      DELETE landing. Cancelling that already-running sync is a separate
      action (``POST /api/actions/syncs/{sync_id}/cancel``).

    The 404 vs 409 split matters: 404 says "your request never reached
    the queue or was already drained without consequence", 409 says
    "your cancellation lost the race; if you want to abort the in-flight
    work you need a different action".
    """
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        # Snapshot the row before we attempt the delete so the audit
        # ``before`` payload carries operator-useful context regardless
        # of which branch we end up in.
        existing = await (
            await conn.execute(
                """
                SELECT user_email, platform, requested_at, reason
                FROM force_sync_requests
                WHERE user_email = ? AND platform = ?
                """,
                (email, platform),
            )
        ).fetchone()
        if existing is not None:
            cursor = await conn.execute(
                "DELETE FROM force_sync_requests WHERE user_email = ? AND platform = ?",
                (email, platform),
            )
            deleted = (cursor.rowcount or 0) > 0
            await conn.commit()
            if deleted:
                writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
                await writer.write(
                    action="force_sync.cancel_pending",
                    target=f"{email}/{platform}",
                    before=dict(existing),
                    after={"cancelled": True},
                )
                return CancelForceSyncResult(
                    cancelled=True,
                    user_email=email,
                    platform=platform,
                )
            # Race: someone else deleted between SELECT and DELETE — fall
            # through to the no-row branch.

        # No pending row. Check if a sync for the same pair is currently
        # running — that's the scheduler-picked-it-up race. We surface
        # 409 so the operator can choose a different action.
        running = await (
            await conn.execute(
                """
                SELECT 1 FROM sync_history
                WHERE user_email = ? AND platform = ?
                  AND state = 'running' AND finished_at IS NULL
                LIMIT 1
                """,
                (email, platform),
            )
        ).fetchone()
    if running is not None:
        raise_http_exception(
            status.HTTP_409_CONFLICT,
            ErrorCode.STATE_CONFLICT,
            "force-sync request already picked up by scheduler",
            hint="cancel the in-flight sync via POST /api/actions/syncs/{sync_id}/cancel",
        )
    raise_http_exception(
        status.HTTP_404_NOT_FOUND,
        ErrorCode.NOT_FOUND,
        f"no pending force-sync request for {email}/{platform}",
    )


__all__ = [
    "CancelForceSyncResult",
    "ForceSyncQueueItem",
    "ForceSyncQueueList",
    "router",
]
