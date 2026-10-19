"""/api/items/{item_id} — full detail for the Contributions drawer.

Returns both `payload` (normalized JSON blob) and `raw_payload` (original
response kept verbatim by the adapter) so the dashboard can render a
full payload inspector. Read-only; item purges live in /api/actions.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception

router = APIRouter(prefix="/items", tags=["items"])


class ItemDetail(BaseModel):
    """Per-item drawer payload.

    ``payload`` and ``raw_payload`` are intentionally typed as
    ``dict[str, Any]`` — the normalized payload's shape varies per
    connector and content type; pinning a sub-schema would force a
    Pydantic model per connector + content type, which the registry
    resolves at runtime, not build time. The dashboard renders these as
    a tree view rather than typed cards, so the loose shape matches the
    consumer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    external_id: str
    platform: str
    course_external_id: str | None
    source_url: str | None
    schema_version: str
    adapter_version: str
    ingestion_mode: str
    processing_status: str
    error_message: str | None
    fetched_by_user_email: str | None
    ingested_at: str
    updated_at: str
    payload: dict[str, Any]
    raw_payload: dict[str, Any]


@router.get("/{item_id}", response_model=ItemDetail)
async def item_detail(item_id: str, ctx: ContextDep) -> ItemDetail:
    from core.models import IngestedItem

    try:
        row = await IngestedItem.objects.aget(id=item_id)
    except IngestedItem.DoesNotExist:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "item not found",
        )
    _ = ctx
    return ItemDetail(
        id=str(row.id),
        external_id=row.external_id,
        platform=row.platform,
        course_external_id=row.course_external_id,
        source_url=row.source_url,
        schema_version=row.schema_version,
        adapter_version=row.adapter_version,
        ingestion_mode=row.ingestion_mode,
        processing_status=row.processing_status,
        error_message=row.error_message,
        fetched_by_user_email=row.fetched_by_user_email,
        ingested_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        payload=row.payload if isinstance(row.payload, dict) else {},
        raw_payload=row.raw_payload if isinstance(row.raw_payload, dict) else {},
    )
