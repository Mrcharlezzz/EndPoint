"""/api/actions/* — pause / unpause / mark-expired routes.

Thin layer over :mod:`ingestion.daemon.services.credential_service`.
Routers translate HTTP envelopes (Pydantic request bodies + ``Request``
for the audit actor) into service calls and translate typed service
exceptions to ``HTTPException`` via the central
:func:`service_error_to_http` translator.

Mounts under the same paths the old monolithic ``actions.py`` used so
this is a non-breaking refactor — see the slice URL-stability table.

Bulk endpoints (slice 7):

* ``POST /api/actions/users/bulk-pause`` — pause every credential on a
  platform, OR every credential for a list of users.
* ``POST /api/actions/users/bulk-mark-expired`` — same selector shape;
  flips ``is_expired=True`` on every match.

Body validation enforces the XOR (platform XOR user_emails) at the
422 layer so the service never sees a malformed selector. The
``user_emails`` list is capped at 1000 entries — past that an operator
should use the platform variant or split the call to avoid a slow
single-request scan.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.services import credential_service
from ingestion.daemon.services.audit_writer import SqliteAuditWriter
from ingestion.daemon.services.exceptions import ServiceError
from ingestion.daemon.services.results import (
    BulkOperationResult,
    MarkExpiredResult,
    PauseResult,
)

router = APIRouter(prefix="/actions", tags=["actions"])

# Cap on the user_emails list per request. 1000 is the operational
# upper bound for a single incident-response click; past that the
# call would scan thousands of credential rows under one request and
# timeout the dashboard's fetch wrapper. Operators chunking past 1000
# should use the platform selector instead.
_BULK_USER_EMAILS_LIMIT = 1000


class ActionBody(BaseModel):
    # Cap the audit note to something sane; audit rows are intended for
    # human consumption, not log dumps.
    reason: str | None = Field(default=None, max_length=1024)


@router.post("/users/{email}/pause", response_model=PauseResult)
async def pause_user(
    email: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> PauseResult:
    """Pause every PlatformCredential row for ``email``. Idempotent.

    404 only when the user has no rows at all.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.pause_user(
            ctx.credentials,
            email,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/{email}/unpause", response_model=PauseResult)
async def unpause_user(
    email: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> PauseResult:
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.unpause_user(
            ctx.credentials,
            email,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/{email}/{platform}/pause", response_model=PauseResult)
async def pause_credential(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> PauseResult:
    """Pause one ``PlatformCredential`` row for (email, platform).

    The V2 Syncs page needs per-platform granularity. 404 when the row
    does not exist. Idempotent: already-paused returns
    ``rows_affected=0``.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.pause_credential(
            ctx.credentials,
            email,
            platform,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/{email}/{platform}/unpause", response_model=PauseResult)
async def unpause_credential(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> PauseResult:
    """Inverse of :func:`pause_credential`. 404 on missing row,
    idempotent on already-unpaused."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.unpause_credential(
            ctx.credentials,
            email,
            platform,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post(
    "/users/{email}/{platform}/mark_expired",
    response_model=MarkExpiredResult,
)
async def mark_expired(
    email: str,
    platform: str,
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> MarkExpiredResult:
    """Mark a credential expired so the scheduler skips it until re-auth."""
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            credential_service.mark_credential_expired(
                ctx.credentials,
                email,
                platform,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)


# --- Bulk operations -----------------------------------------------------


class BulkSelectorBody(BaseModel):
    """XOR selector for bulk pause / mark-expired.

    Exactly one of ``platform`` / ``user_emails`` must be set. The
    model_validator enforces the XOR at the pydantic layer so a 422
    fires before the router body even runs — the service-layer call
    never sees a malformed selector.

    ``user_emails`` is capped at 1000 entries per call (see module
    docstring). The cap is enforced via Field's ``max_length`` so the
    422 carries a precise field path.
    """

    model_config = {"extra": "forbid"}

    platform: str | None = Field(default=None, min_length=1)
    user_emails: list[str] | None = Field(
        default=None,
        max_length=_BULK_USER_EMAILS_LIMIT,
    )
    reason: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _enforce_xor(self) -> BulkSelectorBody:
        # Empty list is treated like None so an idle list-input form
        # POST doesn't pass through with both selectors set to falsy.
        has_platform = self.platform is not None
        has_emails = bool(self.user_emails)
        if not has_platform and not has_emails:
            raise ValueError("either platform or user_emails must be provided")
        if has_platform and has_emails:
            raise ValueError("platform and user_emails are mutually exclusive")
        return self


@router.post("/users/bulk-pause", response_model=BulkOperationResult)
async def bulk_pause_users(
    body: BulkSelectorBody,
    ctx: ContextDep,
    request: Request,
) -> BulkOperationResult:
    """Pause every credential matched by the selector.

    Returns a partial-success envelope: rows that flipped state count
    in ``paused_count``; rows that were already paused or that another
    writer flipped concurrently land in ``skipped_count``; per-item
    failures are listed in ``errors`` rather than failing the whole
    request.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.bulk_pause_credentials(
            ctx.credentials,
            platform=body.platform,
            user_emails=body.user_emails,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)


@router.post("/users/bulk-mark-expired", response_model=BulkOperationResult)
async def bulk_mark_expired_users(
    body: BulkSelectorBody,
    ctx: ContextDep,
    request: Request,
) -> BulkOperationResult:
    """Flip ``is_expired=True`` on every credential matched by the selector.

    Same partial-success semantics as :func:`bulk_pause_users`. Already-
    expired rows count as ``skipped_count`` rather than re-emitting the
    expiry side-effects.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await credential_service.bulk_mark_expired(
            ctx.credentials,
            platform=body.platform,
            user_emails=body.user_emails,
            audit_writer=writer,
            reason=body.reason,
        )
    except ServiceError as exc:
        service_error_to_http(exc)
