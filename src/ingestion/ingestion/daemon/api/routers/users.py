"""/api/users, /api/users/{email}.

Users are derived from PlatformCredential rows by grouping on ``user_email``;
each row becomes an entry in ``platforms``. Matches the UI's ``User`` entity
(handoff §9) which expects one object per user with a per-platform summary.
Encrypted payloads never leave the daemon.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.system_users import SYSTEM_USER_EMAILS

router = APIRouter(prefix="/users", tags=["users"])

_MAX_LIMIT = 500


class UserPlatformSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_paused: bool
    is_expired: bool
    last_refreshed_at: str | None
    expired_at: str | None


class UserListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    display_name: str | None
    platforms: dict[str, UserPlatformSummary]
    any_paused: bool
    any_expired: bool


class UserDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    display_name: str | None
    platforms: dict[str, UserPlatformSummary]


class UserStats(BaseModel):
    """Aggregates for the User detail → Overview tab.

    ``cursors`` is a free-form JSON object: each platform maps to a
    scope -> cursor map, where the cursor shape varies per connector
    (Teams' delta token, Moodle's last-fetched id, etc.). Kept loose so
    a connector can evolve its cursor format without a schema migration.
    """

    model_config = ConfigDict(extra="forbid")

    total_items: int
    errors_last_7d: int
    cursors: dict[str, dict[str, Any]]


class UserItemRow(BaseModel):
    """Row in the Contributions tab — one IngestedItem scoped to the user.

    Fields pulled from the JSON `payload` blob (title, course_name,
    published_at) are projected up for the table view. `raw_payload` is
    intentionally omitted — fetch the item detail endpoint for that."""

    model_config = ConfigDict(extra="forbid")

    id: str
    external_id: str
    platform: str
    content_type: str | None
    title: str | None
    course_name: str | None
    source_url: str | None
    published_at: str | None
    ingested_at: str


def _platform_summary(row: Any) -> UserPlatformSummary:
    return UserPlatformSummary(
        is_paused=bool(row.is_paused),
        is_expired=bool(row.is_expired),
        last_refreshed_at=(
            row.last_refreshed_at.isoformat() if row.last_refreshed_at is not None else None
        ),
        expired_at=(row.expired_at.isoformat() if row.expired_at is not None else None),
    )


@router.get("", response_model=list[UserListItem])
async def list_users(
    ctx: ContextDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserListItem]:
    """List users with their per-platform summary. Paginated by user_email."""
    from core.models import PlatformCredential

    grouped: dict[str, dict[str, Any]] = {}
    async for row in (
        PlatformCredential.objects.exclude(user_email__in=SYSTEM_USER_EMAILS)
        .order_by("user_email", "platform")
        .aiterator()
    ):
        bucket = grouped.setdefault(
            row.user_email,
            {
                "email": row.user_email,
                "display_name": row.display_name,
                "platforms": {},
                "any_paused": False,
                "any_expired": False,
            },
        )
        bucket["platforms"][row.platform] = _platform_summary(row)
        bucket["any_paused"] = bucket["any_paused"] or bool(row.is_paused)
        bucket["any_expired"] = bucket["any_expired"] or bool(row.is_expired)
    # Slice after grouping so offset/limit counts users, not rows.
    users = [UserListItem(**bucket) for bucket in grouped.values()]
    _ = ctx  # kept for dependency activation
    return users[offset : offset + limit]


@router.get("/{email}", response_model=UserDetail)
async def user_detail(email: str, ctx: ContextDep) -> UserDetail:
    from core.models import PlatformCredential

    if email in SYSTEM_USER_EMAILS:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "user not found",
        )
    rows = [c async for c in PlatformCredential.objects.filter(user_email=email)]
    if not rows:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "user not found",
        )
    platforms = {row.platform: _platform_summary(row) for row in rows}
    _ = ctx
    return UserDetail(
        email=email,
        display_name=rows[0].display_name,
        platforms=platforms,
    )


def _payload_field(payload: Any, key: str) -> str | None:
    """Defensive pull from the JSONB payload blob — any non-string coerces to None."""
    if not isinstance(payload, dict):
        return None
    v = payload.get(key)
    return v if isinstance(v, str) else None


@router.get("/{email}/stats", response_model=UserStats)
async def user_stats(email: str, ctx: ContextDep) -> UserStats:
    """Aggregates for the User detail header + Overview tab: total items
    attributed to this user, failed-sync count in the last 7 days, and
    current per-platform/per-scope cursors.
    """
    from core.models import IngestedItem, PlatformCredential

    exists = await PlatformCredential.objects.filter(user_email=email).aexists()
    if not exists:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "user not found",
        )

    total_items = await IngestedItem.objects.filter(fetched_by_user_email=email).acount()

    since = (datetime.now(tz=UTC) - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        errors_row = await (
            await conn.execute(
                "SELECT COUNT(*) AS n FROM sync_history "
                "WHERE user_email = ? AND state = 'failed' AND started_at >= ?",
                (email, since),
            )
        ).fetchone()
        cursor_rows = await (
            await conn.execute(
                "SELECT platform, scope, cursor_json FROM sync_cursors WHERE user_email = ?",
                (email,),
            )
        ).fetchall()
    errors_last_7d = int(errors_row["n"]) if errors_row is not None else 0
    cursors: dict[str, dict[str, Any]] = {}
    for row in cursor_rows:
        try:
            cursor = json.loads(row["cursor_json"])
        except (TypeError, ValueError):
            cursor = None
        cursors.setdefault(row["platform"], {})[row["scope"]] = cursor
    return UserStats(
        total_items=total_items,
        errors_last_7d=errors_last_7d,
        cursors=cursors,
    )


@router.get("/{email}/items", response_model=list[UserItemRow])
async def user_items(
    email: str,
    ctx: ContextDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    platform: Annotated[str | None, Query()] = None,
) -> list[UserItemRow]:
    """Items fetched using this user's credentials. The "Contributions"
    tab in the dashboard. Items with no attribution (telegram, legacy
    rows before the `fetched_by_user_email` column landed) are excluded.
    """
    from core.models import IngestedItem

    qs = IngestedItem.objects.filter(fetched_by_user_email=email)
    if platform is not None:
        qs = qs.filter(platform=platform)
    qs = qs.order_by("-created_at")[offset : offset + limit]

    rows: list[UserItemRow] = []
    async for row in qs.aiterator():
        payload = row.payload
        rows.append(
            UserItemRow(
                id=str(row.id),
                external_id=row.external_id,
                platform=row.platform,
                content_type=_payload_field(payload, "content_type"),
                title=_payload_field(payload, "title"),
                course_name=_payload_field(payload, "course_name"),
                source_url=row.source_url,
                published_at=_payload_field(payload, "published_at"),
                ingested_at=row.created_at.isoformat(),
            )
        )
    _ = ctx
    return rows
