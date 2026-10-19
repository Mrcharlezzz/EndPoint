"""/api/actions/audit/export — streamed NDJSON export of the audit log.

Lifted bit-for-bit from the old monolithic ``actions.py`` (the S1A
landing). The ``_AUDIT_EXPORT_FILTERS`` allowlist and the streaming
generator are unchanged so existing tests that import the constant
keep matching the same SQL fragments.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ingestion.daemon.api.deps import ContextDep

router = APIRouter(prefix="/actions", tags=["actions"])


# Fixed allowlist of audit-export filters. Maps the public query
# parameter name to the SQL fragment used in the WHERE clause. Both
# halves are constant strings owned by this module; user input never
# reaches the SQL text.
_AUDIT_EXPORT_FILTERS: dict[str, str] = {
    "action": "action = ?",
    "target": "target = ?",
    "actor": "actor = ?",
}


@router.get("/audit/export")
async def export_audit(
    ctx: ContextDep,
    action: str | None = None,
    target: str | None = None,
    actor: str | None = None,
) -> StreamingResponse:
    """Stream the full audit log as newline-delimited JSON.

    The regular /api/audit endpoint caps at 500 rows; the export
    variant streams the entire filtered set, so the UI can hand it to
    the browser as a file download. Filters mirror /api/audit exactly.
    """
    # Build the WHERE clause from a fixed allowlist so SQL fragments
    # are never derived from caller input — only values flow through
    # ``?``.
    filter_values: dict[str, str] = {}
    if action is not None:
        filter_values["action"] = action
    if target is not None:
        filter_values["target"] = target
    if actor is not None:
        filter_values["actor"] = actor
    clauses: list[str] = [_AUDIT_EXPORT_FILTERS[name] for name in filter_values]
    params: list[Any] = list(filter_values.values())
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = (
        "SELECT id, timestamp, actor, action, target, before_json, "
        "after_json, reason FROM audit_log "
        + where
        + (" " if where else "")
        + "ORDER BY timestamp DESC, id DESC"
    )

    async def _stream() -> Any:
        async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            async for row in cursor:
                payload = {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "target": row["target"],
                    "before": (json.loads(row["before_json"]) if row["before_json"] else None),
                    "after": json.loads(row["after_json"]) if row["after_json"] else None,
                    "reason": row["reason"],
                }
                yield (json.dumps(payload, separators=(",", ":")) + "\n").encode()

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.ndjson"'},
    )
