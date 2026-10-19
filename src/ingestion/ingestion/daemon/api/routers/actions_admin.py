"""/api/actions/admin/* — admin-gated, deeply-destructive actions.

Currently only the Fernet rotation endpoint lives here. Kept apart
from ``actions_maintenance`` because rotation mutates the encryption
key itself rather than credentials or syncs — losing it leaves the
on-disk ciphertexts unreadable. The dashboard surfaces this behind
an extra confirm dialog.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.services import admin_service
from ingestion.daemon.services.audit_writer import SqliteAuditWriter
from ingestion.daemon.services.exceptions import ServiceError

router = APIRouter(prefix="/actions/admin", tags=["actions"])


class FernetRotationBody(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


class FernetRotationResponse(BaseModel):
    """Per-row counts from the rotation pass.

    Field names mirror :class:`RotationResult` so the same shape is
    documented in OpenAPI as the underlying dataclass.
    """

    model_config = ConfigDict(extra="forbid")

    total_rows: int
    rotated: int
    already_rotated: int
    raced: int
    failed: int


@router.post("/fernet-rotate", response_model=FernetRotationResponse)
async def fernet_rotate(
    body: FernetRotationBody,
    ctx: ContextDep,
    request: Request,
) -> FernetRotationResponse:
    """Re-encrypt every credential row to the new Fernet primary.

    The new key MUST already be in the daemon's environment as
    ``ENDPOINT_FERNET_KEY_NEW``. Returns per-row counts so the
    caller can confirm a clean rotation (``failed=0``) before
    flipping ``ENDPOINT_FERNET_KEY`` to drop the old key.

    Idempotent: a second call after a partial rotation only touches
    rows still on the old key. The per-row UPDATE is keyed on the
    pre-rotation ciphertext so a concurrent refresh-loop write
    landing mid-rotation lands as ``raced`` rather than overwriting
    the fresh payload.
    """
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        result = await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            admin_service.trigger_fernet_rotation(
                current_cipher=ctx.cipher,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)
    return FernetRotationResponse(
        total_rows=result.total_rows,
        rotated=result.rotated,
        already_rotated=result.already_rotated,
        raced=result.raced,
        failed=result.failed,
    )
