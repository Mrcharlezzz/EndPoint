"""/api/audit filtered read."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.schemas import AuditEntry
from ingestion.daemon.store import audit as audit_store

router = APIRouter(prefix="/audit", tags=["audit"])

_MAX_LIMIT = 500


@router.get("", response_model=list[AuditEntry])
async def list_audit(
    ctx: ContextDep,
    action: str | None = None,
    target: str | None = None,
    actor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 100,
) -> list[AuditEntry]:
    rows = await audit_store.list_entries(
        ctx.config.sqlite_path,
        action=action,
        target=target,
        actor=actor,
        since=since.isoformat() if since is not None else None,
        until=until.isoformat() if until is not None else None,
        limit=limit,
    )
    return [AuditEntry(**row) for row in rows]
