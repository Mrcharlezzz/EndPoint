"""/api/actions/* — operator-maintenance routes.

Covers reset_cursor, purge_item, compact_storage, clear_dead_letter,
mute_rule, force-refresh-teams. Each route translates HTTP envelope to
:mod:`ingestion.daemon.services.maintenance_service` and maps typed
exceptions to HTTP responses via :func:`service_error_to_http`.

The audit-export route lives in a dedicated module
(``actions_audit.py``) — it streams NDJSON instead of returning a
typed response and pulls a fixed allowlist of filters, so it doesn't
fit the same shape as the rest of the maintenance surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.api.schemas import OkResponse
from ingestion.daemon.services import maintenance_service
from ingestion.daemon.services.audit_writer import SqliteAuditWriter
from ingestion.daemon.services.exceptions import ServiceError
from ingestion.daemon.services.results import (
    CompactStorageResult,
    ForceRefreshResult,
    MuteRuleResult,
    PurgeItemResult,
    ReplayDeadLetterResult,
    ResetCursorResult,
)

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


class DestructiveActionBody(BaseModel):
    confirm: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1024)


class ResetCursorBody(DestructiveActionBody):
    # Scope defaults to the Phase 1 'default' scope. Teams syncs write
    # per-scope cursors (channels, assignments); clearing them all is
    # the common case so we default to every scope.
    scope: str | None = None


class PurgeItemBody(DestructiveActionBody):
    pass


class MuteRuleBody(ActionBody):
    enabled: bool = False


@router.post("/users/{email}/{platform}/reset_cursor", response_model=ResetCursorResult)
async def reset_cursor(
    email: str,
    platform: str,
    body: ResetCursorBody,
    ctx: ContextDep,
    request: Request,
) -> ResetCursorResult:
    """Delete sync_cursors rows for (email, platform, [scope])."""
    target = f"{email}/{platform}"
    if body.confirm != target:
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            f"confirm must equal {target!r}",
        )
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.reset_cursor(
                ctx.config.sqlite_path,
                email=email,
                platform=platform,
                scope=body.scope,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/content/{item_id}/purge", response_model=PurgeItemResult)
async def purge_item(
    item_id: str,
    body: PurgeItemBody,
    ctx: ContextDep,
    request: Request,
) -> PurgeItemResult:
    """Soft-delete a normalized content item. Requires
    ``confirm="item:<id>"`` and a reason."""
    expected = f"item:{item_id}"
    if body.confirm != expected:
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            f"confirm must equal {expected!r}",
        )
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.purge_item(
                item_id=item_id,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/storage/compact", response_model=CompactStorageResult)
async def compact_storage(
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> CompactStorageResult:
    """Run SQLite VACUUM on the local state database."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.compact_storage(
                ctx.config.sqlite_path,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/dead_letter/{email}/{platform}/clear", response_model=OkResponse)
async def clear_dead_letter(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> OkResponse:
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.clear_dead_letter(
                ctx.config.sqlite_path,
                email=email,
                platform=platform,
                threshold=ctx.config.scheduler.dead_letter_threshold,
                cooldown_seconds=ctx.config.scheduler.dead_letter_cooldown_seconds,
                audit_writer=writer,
                reason=body.reason,
                event_bus=ctx.events,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)
    return OkResponse(ok=True)


@router.post(
    "/dead_letter/{email}/{platform}/replay",
    response_model=ReplayDeadLetterResult,
)
async def replay_dead_letter(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> ReplayDeadLetterResult:
    """Enqueue a force-sync without resetting the dead-letter counter.

    Distinct from :func:`clear_dead_letter`. Use ``replay`` when you
    want to TEST whether a fix took effect (the counter survives so a
    still-broken pair re-suppresses on next failure); use ``clear``
    when you've verified the underlying issue and want a fresh streak.

    Idempotent: a second call before the scheduler picks up the row
    is a free upsert (force_sync_requests has ``ON CONFLICT DO UPDATE``);
    a second call after the scheduler picked up the first one queues
    another force-sync. Either way one audit row lands per request.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.replay_dead_letter(
                ctx.config.sqlite_path,
                email=email,
                platform=platform,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.patch("/alerts/rules/{rule_id}/mute", response_model=MuteRuleResult)
async def mute_rule(
    rule_id: int,
    body: MuteRuleBody,
    ctx: ContextDep,
    request: Request,
) -> MuteRuleResult:
    """Alias for ``PATCH /api/alerts/rules/{id}/enabled`` with the
    semantics inverted. Sets ``enabled=False`` by default; pass
    ``enabled=true`` to un-mute."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.mute_rule(
                ctx.config.sqlite_path,
                rule_id=rule_id,
                enabled=body.enabled,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/{email}/teams/refresh", response_model=ForceRefreshResult)
async def force_refresh_teams(
    email: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> ForceRefreshResult:
    """Trigger one silent Teams refresh for ``email`` out-of-band.

    503 when refresh is disabled. 502 on transient failure (so the
    caller knows the refresh didn't land without re-reading the audit
    log).
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            maintenance_service.force_refresh_teams(
                config=ctx.config,
                cipher=ctx.cipher,
                user_refresh_locks=ctx.user_refresh_locks,
                email=email,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)
