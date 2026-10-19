from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.services import processor_observability
from ingestion.daemon.services.audit_writer import SqliteAuditWriter
from ingestion.daemon.services.exceptions import ServiceError
from ingestion.daemon.services.results import ProcessorWindowResetResult

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionBody(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


@router.post("/processor/window/reset", response_model=ProcessorWindowResetResult)
async def reset_processor_window(
    body: ActionBody,
    ctx: ContextDep,
    request: Request,
) -> ProcessorWindowResetResult:
    writer = SqliteAuditWriter(ctx.config.sqlite_path, make_actor(request))
    try:
        return await with_action_timeout(
            ctx.config.api.action_timeout_seconds,
            processor_observability.reset_window(
                ctx.config.sqlite_path,
                audit_writer=writer,
                reason=body.reason,
            ),
        )
    except ServiceError as exc:
        service_error_to_http(exc)
