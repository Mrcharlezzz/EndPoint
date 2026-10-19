from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.services import processor_observability
from ingestion.daemon.services.results import (
    ProcessorErrorsResponse,
    ProcessorSummaryResponse,
)

router = APIRouter(prefix="/processor", tags=["processor"])

_MAX_LIMIT = 500


@router.get("/summary", response_model=ProcessorSummaryResponse)
async def processor_summary(ctx: ContextDep) -> ProcessorSummaryResponse:
    return await processor_observability.build_summary(ctx.config.sqlite_path)


@router.get("/errors", response_model=ProcessorErrorsResponse)
async def processor_errors(
    ctx: ContextDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 200,
) -> ProcessorErrorsResponse:
    return await processor_observability.list_errors(ctx.config.sqlite_path, limit=limit)
