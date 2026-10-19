"""/api/actions/* — sync-control routes (force, force_all, dry_run, cancel,
scheduler drain / emergency_stop, bulk teams refresh).

Translates HTTP envelopes to :mod:`ingestion.daemon.services.sync_service`
calls. Maintains the URL paths the old monolithic ``actions.py`` used so
clients remain unaffected.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.api.schemas import OkResponse
from ingestion.daemon.services import sync_service
from ingestion.daemon.services.audit_writer import SqliteAuditWriter
from ingestion.daemon.services.exceptions import ServiceError
from ingestion.daemon.services.results import (
    BulkRefreshResult,
    CancelSyncResult,
    DrainModeResult,
    DryRunResult,
    ForceSyncAllResult,
    ForceSyncResult,
)

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


class DestructiveActionBody(BaseModel):
    """Body schema for actions that delete data or cancel in-flight work.

    ``confirm`` must equal the target identifier exactly. Mismatch
    rejects with 422 so a typo can't fat-finger the action. ``reason``
    is required so every destructive audit row carries operator context.
    """

    confirm: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1024)


class DrainModeBody(ActionBody):
    enabled: bool


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_email: str = Field(min_length=1)
    # Constrained to the three known platforms to avoid round-tripping
    # a typo to the adapter factory only to blow up inside run_sync.
    platform: str = Field(pattern=r"^(teams|moodle|telegram)$")


@router.post("/users/{email}/{platform}/force_sync", response_model=ForceSyncResult)
async def force_sync(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> ForceSyncResult:
    """Queue an immediate sync for (email, platform)."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            sync_service.force_sync_one(
                ctx.config.sqlite_path,
                email=email,
                platform=platform,
                audit_writer=writer,
                reason=body.reason,
                max_queue_depth=ctx.config.api.max_force_sync_queue_depth,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/{email}/force_sync_all", response_model=ForceSyncAllResult)
async def force_sync_all(
    email: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> ForceSyncAllResult:
    """Queue a force-sync for every connected platform this user has."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            sync_service.force_sync_all_for_user(
                ctx.config.sqlite_path,
                port=ctx.credentials,
                email=email,
                audit_writer=writer,
                reason=body.reason,
                max_queue_depth=ctx.config.api.max_force_sync_queue_depth,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/sync/dry_run", response_model=DryRunResult)
async def sync_dry_run(
    body: DryRunBody,
    ctx: ContextDep,
    request: Request,
) -> DryRunResult:
    """Run a single sync cycle with writes suppressed and return counts."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            sync_service.dry_run_sync(
                ctx.config.sqlite_path,
                cipher=ctx.cipher,
                email=body.user_email,
                platform=body.platform,
                audit_writer=writer,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/syncs/{sync_id}/cancel", response_model=CancelSyncResult)
async def cancel_sync(
    sync_id: str,
    body: DestructiveActionBody,
    ctx: ContextDep,
    request: Request,
) -> CancelSyncResult:
    """Cancel an in-flight sync by ``sync_id``."""
    if body.confirm != sync_id:
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            f"confirm must equal {sync_id!r}",
        )
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            sync_service.cancel_sync(
                ctx.config.sqlite_path,
                sync_id=sync_id,
                cancels=ctx.cancels,
                events=ctx.events,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/scheduler/drain", response_model=DrainModeResult)
async def set_drain_mode(
    body: DrainModeBody,
    ctx: ContextDep,
    request: Request,
) -> DrainModeResult:
    """Toggle scheduler.drain_mode in the overlay. Takes effect next tick."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    return await sync_service.set_drain_mode(
        store=ctx.store,
        enabled=body.enabled,
        audit_writer=writer,
        reason=body.reason,
    )


@router.post("/scheduler/emergency_stop", response_model=OkResponse)
async def emergency_stop(
    body: DestructiveActionBody,
    ctx: ContextDep,
    request: Request,
) -> OkResponse:
    """Cancel every in-flight sync and refuse new submissions until the
    scheduler is re-enabled. Requires ``confirm="scheduler"`` and a reason.
    """
    if body.confirm != "scheduler":
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            "confirm must equal 'scheduler' to trigger emergency stop",
        )
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    await sync_service.emergency_stop(
        store=ctx.store,
        audit_writer=writer,
        reason=body.reason,
    )
    return OkResponse(ok=True)


@router.post("/teams/refresh_all", response_model=BulkRefreshResult)
async def bulk_refresh_teams(
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> BulkRefreshResult:
    """Queue a force-sync request for every Teams user. The Playwright
    pool processes the queue one at a time to avoid MS throttling."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    count = await sync_service.bulk_refresh_teams(
        ctx.config.sqlite_path,
        port=ctx.credentials,
        audit_writer=writer,
        reason=body.reason,
    )
    return BulkRefreshResult(enqueued=count)
