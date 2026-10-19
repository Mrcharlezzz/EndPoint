"""/api/errors surface.

Backs the dashboard's Errors page (handoff §7.6). Three feeds unify into
one endpoint:

    * Dead-letter state rows (user/platform with consecutive_failures set
      or ``suppressed_until`` in the future).
    * Recent failed sync_history rows.
    * Expired credentials (``is_expired=True``), which are the authentic
      "user needs to re-auth" signal.

The page is ranked by recency so the operator sees the freshest pain
first. Clearing an error is done via the appropriate action endpoint
(/api/actions/dead_letter/.../clear, /api/actions/users/.../teams/refresh,
etc.); this router is read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep

router = APIRouter(prefix="/errors", tags=["errors"])

_MAX_LIMIT = 500


class ErrorItem(BaseModel):
    """One entry in the merged errors feed.

    Three ``kind`` values are emitted: ``dead_letter``, ``failed_sync``,
    ``expired_credential``. Fields are optional across kinds because each
    source carries a different subset (e.g. ``sync_id`` is only on
    failed_sync rows)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    user_email: str | None = None
    platform: str | None = None
    occurred_at: str | None = None
    consecutive_failures: int | None = None
    suppressed_until: str | None = None
    sync_id: str | None = None
    started_at: str | None = None
    error: str | None = None


class ErrorCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dead_letter: int
    failed_syncs: int
    expired_credentials: int


class ErrorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ErrorItem]
    counts: ErrorCounts


@router.get("", response_model=ErrorsResponse)
async def list_errors(
    ctx: ContextDep,
    user_email: str | None = None,
    platform: str | None = None,
    since: datetime | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 200,
) -> ErrorsResponse:
    """Return the merged error feed.

    ``kind`` narrows to one source when set: ``"dead_letter"``,
    ``"failed_sync"``, or ``"expired_credential"``. Default mixes all
    three, newest first.
    """
    want_dead_letter = kind is None or kind == "dead_letter"
    want_failed = kind is None or kind == "failed_sync"
    want_expired = kind is None or kind == "expired_credential"

    dead_letter: list[ErrorItem] = []
    failed_syncs: list[ErrorItem] = []
    if want_dead_letter or want_failed:
        async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
            conn.row_factory = aiosqlite.Row
            if want_dead_letter:
                dead_letter = await _fetch_dead_letter(
                    conn, user_email=user_email, platform=platform, limit=limit
                )
            if want_failed:
                failed_syncs = await _fetch_failed_syncs(
                    conn,
                    user_email=user_email,
                    platform=platform,
                    since=since,
                    limit=limit,
                )
    expired: list[ErrorItem] = []
    if want_expired:
        expired = await _fetch_expired_credentials(
            user_email=user_email,
            platform=platform,
            limit=limit,
        )

    merged = dead_letter + failed_syncs + expired
    # Sort by the event's own timestamp field. Each row carries its own
    # ``occurred_at`` so the mixed feed stays chronologically coherent.
    merged.sort(key=lambda r: r.occurred_at or "", reverse=True)
    return ErrorsResponse(
        items=merged[:limit],
        counts=ErrorCounts(
            dead_letter=len(dead_letter),
            failed_syncs=len(failed_syncs),
            expired_credentials=len(expired),
        ),
    )


async def _fetch_dead_letter(
    conn: aiosqlite.Connection,
    *,
    user_email: str | None,
    platform: str | None,
    limit: int,
) -> list[ErrorItem]:
    clauses: list[str] = ["(consecutive_failures > 0 OR suppressed_until IS NOT NULL)"]
    params: list[Any] = []
    if user_email:
        clauses.append("user_email = ?")
        params.append(user_email)
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"""
            SELECT user_email, platform, consecutive_failures, last_failure_at, suppressed_until
            FROM dead_letter_state
            WHERE {" AND ".join(clauses)}
            ORDER BY last_failure_at DESC
            LIMIT ?
            """,
            params,
        )
    ).fetchall()
    return [
        ErrorItem(
            kind="dead_letter",
            user_email=row["user_email"],
            platform=row["platform"],
            consecutive_failures=row["consecutive_failures"],
            suppressed_until=row["suppressed_until"],
            occurred_at=row["last_failure_at"],
        )
        for row in rows
    ]


async def _fetch_failed_syncs(
    conn: aiosqlite.Connection,
    *,
    user_email: str | None,
    platform: str | None,
    since: datetime | None,
    limit: int,
) -> list[ErrorItem]:
    # Hide failed rows that a later ``complete`` row has already resolved
    # on the same (user_email, platform). "Is Teams currently failing?"
    # should answer "no" if Teams synced successfully since the failure,
    # otherwise transient failures never clear from the operator's view.
    clauses: list[str] = [
        "f.state = 'failed'",
        """NOT EXISTS (
            SELECT 1 FROM sync_history AS later
            WHERE later.user_email = f.user_email
              AND later.platform = f.platform
              AND later.state = 'complete'
              AND COALESCE(later.finished_at, later.started_at)
                    > COALESCE(f.finished_at, f.started_at)
        )""",
    ]
    params: list[Any] = []
    if user_email:
        clauses.append("f.user_email = ?")
        params.append(user_email)
    if platform:
        clauses.append("f.platform = ?")
        params.append(platform)
    if since is not None:
        clauses.append("f.started_at >= ?")
        params.append(since.isoformat())
    params.append(limit)
    rows = await (
        await conn.execute(
            f"""
            SELECT f.id, f.user_email, f.platform, f.started_at, f.finished_at, f.error
            FROM sync_history AS f
            WHERE {" AND ".join(clauses)}
            ORDER BY COALESCE(f.finished_at, f.started_at) DESC
            LIMIT ?
            """,
            params,
        )
    ).fetchall()
    return [
        ErrorItem(
            kind="failed_sync",
            sync_id=row["id"],
            user_email=row["user_email"],
            platform=row["platform"],
            started_at=row["started_at"],
            # ``error`` may carry a stack trace. Truncate so the feed
            # stays bounded; the full message lives in /api/logs or
            # /api/syncs/{id}.
            error=_truncate(row["error"], 500),
            occurred_at=row["finished_at"] or row["started_at"],
        )
        for row in rows
    ]


async def _fetch_expired_credentials(
    *,
    user_email: str | None,
    platform: str | None,
    limit: int,
) -> list[ErrorItem]:
    from core.models import PlatformCredential

    qs = PlatformCredential.objects.filter(is_expired=True).order_by("-expired_at")
    if user_email:
        qs = qs.filter(user_email=user_email)
    if platform:
        qs = qs.filter(platform=platform)
    items: list[ErrorItem] = []
    async for row in qs.aiterator():
        items.append(
            ErrorItem(
                kind="expired_credential",
                user_email=row.user_email,
                platform=row.platform,
                occurred_at=(row.expired_at.isoformat() if row.expired_at is not None else None),
            )
        )
        if len(items) >= limit:
            break
    return items


def _truncate(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...[truncated]"
