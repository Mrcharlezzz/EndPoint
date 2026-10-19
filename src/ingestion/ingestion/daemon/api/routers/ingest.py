"""POST /api/ingest/* — broad-scope ingestion orchestration.

Separate from actions.py so fan-out-everything operations (where the
target is "all credentials in a platform set") don't get tangled with
single-row mutations. Each handler writes one aggregate audit row —
individual force-sync queue entries are visible on the V2 Syncs page.
"""

from __future__ import annotations

from typing import Literal

import aiosqlite
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.force_sync import enqueue_force_sync
from ingestion.daemon.store import audit as audit_store

router = APIRouter(prefix="/ingest", tags=["ingest"])

FULL_RESYNC_CONFIRM = "FULL_RESYNC"
# Telegram is a singleton bot, not per-user content. The full-resync endpoint
# is inherently per-user-credential-scoped (iterates PlatformCredential rows),
# which doesn't match Telegram's data model — drop it from the literal so the
# UI and the fan-out stay coherent.
PlatformLiteral = Literal["teams", "moodle"]


class FullResyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1024)
    platforms: list[PlatformLiteral] = Field(min_length=1)


class FullResyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursors_deleted: int
    syncs_enqueued: int
    credentials_skipped: int
    platforms: list[str]


@router.post("/full_resync", response_model=FullResyncResult)
async def full_resync(
    body: FullResyncBody,
    ctx: ContextDep,
    request: Request,
) -> FullResyncResult:
    """Reset cursors and fan out force-syncs for every credential in the
    selected platforms.

    Destructive: wipes ``sync_cursors`` so the next sync pulls everything
    from scratch. Expired credentials are skipped (the scheduler would
    skip them anyway). Per-platform selection happens in the UI, so
    ``platforms`` is authoritative — pause flags are not consulted here.
    """
    from core.models import PlatformCredential

    if body.confirm != FULL_RESYNC_CONFIRM:
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            f"confirm must equal {FULL_RESYNC_CONFIRM!r}",
        )

    platforms = sorted(set(body.platforms))

    placeholders = ",".join("?" * len(platforms))
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        cursor = await conn.execute(
            f"DELETE FROM sync_cursors WHERE platform IN ({placeholders})",
            platforms,
        )
        cursors_deleted = max(0, cursor.rowcount)
        await conn.commit()

    syncs_enqueued = 0
    credentials_skipped = 0
    per_platform: dict[str, int] = {p: 0 for p in platforms}
    async for row in PlatformCredential.objects.filter(platform__in=platforms).aiterator():
        if row.is_expired:
            credentials_skipped += 1
            continue
        await enqueue_force_sync(
            ctx.config.sqlite_path,
            user_email=row.user_email,
            platform=row.platform,
            reason=body.reason,
        )
        syncs_enqueued += 1
        per_platform[row.platform] += 1

    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor=make_actor(request),
        action="ingest.full_resync",
        target=",".join(platforms),
        after={
            "cursors_deleted": cursors_deleted,
            "syncs_enqueued": syncs_enqueued,
            "credentials_skipped": credentials_skipped,
            "per_platform": per_platform,
            "platforms": platforms,
        },
        reason=body.reason,
    )

    return FullResyncResult(
        cursors_deleted=cursors_deleted,
        syncs_enqueued=syncs_enqueued,
        credentials_skipped=credentials_skipped,
        platforms=platforms,
    )
