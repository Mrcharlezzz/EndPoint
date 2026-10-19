"""/api/logs with level, user_email, sync_id, platform, q, since/until filters."""

from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep

router = APIRouter(prefix="/logs", tags=["logs"])


class LogEntry(BaseModel):
    """One decoded row from the ``logs`` table.

    ``context`` is the parsed ``fields_json`` payload so the wire format
    only carries the decoded context, not both the raw string and the
    parsed dict."""

    model_config = ConfigDict(extra="forbid")

    id: int
    timestamp: str
    level: str
    event: str
    user_email: str | None
    platform: str | None
    sync_id: str | None
    context: dict[str, Any] | None


_MAX_LIMIT = 1000


class LogLevel(enum.StrEnum):
    """Log levels we persist. Matches the canonicalised uppercase form."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@router.get("", response_model=list[LogEntry])
async def list_logs(
    ctx: ContextDep,
    level: LogLevel | None = None,
    user_email: str | None = None,
    sync_id: str | None = None,
    platform: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 200,
) -> list[LogEntry]:
    clauses: list[str] = []
    params: list[Any] = []
    if level is not None:
        clauses.append("level = ?")
        params.append(level.value)
    if user_email:
        clauses.append("user_email = ?")
        params.append(user_email)
    if sync_id:
        clauses.append("sync_id = ?")
        params.append(sync_id)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if q:
        # Case-insensitive search across ``event`` and the raw ``fields_json``
        # payload. Escape the LIKE wildcards so an operator searching for
        # ``100%`` or ``user_id`` doesn't accidentally match everything.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(LOWER(event) LIKE LOWER(?) ESCAPE '\\' "
            "OR LOWER(fields_json) LIKE LOWER(?) ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("timestamp <= ?")
        params.append(until.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                # ORDER BY timestamp matches the ix_logs_timestamp_level index
                # so the filtered list hits an index instead of table-scanning.
                # id is the tiebreaker when two rows share the same timestamp
                # (clock slop on the logger thread).
                f"""
                SELECT id, timestamp, level, event, user_email, platform, sync_id, fields_json
                FROM logs
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            )
        ).fetchall()
    results: list[LogEntry] = []
    for row in rows:
        row_dict = dict(row)
        # Decode once and drop the raw JSON string so the wire format is the
        # parsed context — not both forms — which saves bytes and removes the
        # ambiguity about which one to trust.
        fields_json = row_dict.pop("fields_json", None)
        row_dict["context"] = json.loads(fields_json) if fields_json else None
        results.append(LogEntry(**row_dict))
    return results
