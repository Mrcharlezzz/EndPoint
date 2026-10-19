from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

from ingestion.daemon.services.results import (
    ProcessorErrorItem,
    ProcessorErrorsResponse,
    ProcessorHealthSummary,
    ProcessorLLMSummary,
    ProcessorSummaryResponse,
    ProcessorWindowResetResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.daemon.services.audit_writer import AuditWriter


_WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_baseline(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _effective_window_start(now: datetime, baseline_at: datetime | None) -> datetime:
    last_24_hours = now - _WINDOW
    if baseline_at is None:
        return last_24_hours
    return max(last_24_hours, baseline_at)


async def get_baseline(sqlite_path: Path) -> datetime | None:
    async with aiosqlite.connect(sqlite_path) as conn:
        row = await (
            await conn.execute("SELECT baseline_at FROM processor_dashboard_state WHERE id = 1")
        ).fetchone()
    return None if row is None else _parse_baseline(row[0])


async def build_summary(sqlite_path: Path) -> ProcessorSummaryResponse:
    baseline_at = await get_baseline(sqlite_path)
    now = _now()
    window_start = _effective_window_start(now, baseline_at)
    return await asyncio.to_thread(
        _build_summary_sync,
        window_start,
        now,
        _iso_or_none(baseline_at),
    )


def _build_summary_sync(
    window_start: datetime,
    now: datetime,
    baseline_iso: str | None,
) -> ProcessorSummaryResponse:
    from core.models import ClassificationLog, IngestedItem
    from django.db.models import Max, Sum
    from django.db.models.functions import Coalesce

    processed_count = IngestedItem.objects.filter(
        processing_status="processed",
        processing_completed_at__gte=window_start,
    ).count()
    failed_count = IngestedItem.objects.filter(
        processing_status="failed",
        processing_completed_at__gte=window_start,
    ).count()
    pending_queue = IngestedItem.objects.filter(processing_status="pending").count()
    last_processed_at = IngestedItem.objects.filter(
        processing_status__in=["processed", "failed"],
        processing_completed_at__gte=window_start,
    ).aggregate(last_processed_at=Max("processing_completed_at"))["last_processed_at"]

    llm_qs = ClassificationLog.objects.filter(
        classification_method="llm",
        created_at__gte=window_start,
    )
    usage = llm_qs.aggregate(
        input_tokens=Coalesce(Sum("input_tokens"), 0),
        output_tokens=Coalesce(Sum("output_tokens"), 0),
    )

    llm_failed_count = IngestedItem.objects.filter(
        processing_status="failed",
        processing_completed_at__gte=window_start,
        payload__content_type="raw_message",
    ).count()

    return ProcessorSummaryResponse(
        window_start=window_start.isoformat(),
        window_end=now.isoformat(),
        baseline_at=baseline_iso,
        health=ProcessorHealthSummary(
            processed_count=processed_count,
            failed_count=failed_count,
            pending_queue=pending_queue,
            last_processed_at=_iso_or_none(last_processed_at),
        ),
        llm=ProcessorLLMSummary(
            calls=llm_qs.count(),
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
            failed_count=llm_failed_count,
            unclassified_count=llm_qs.filter(entity_type="raw_message").count(),
        ),
    )


async def list_errors(
    sqlite_path: Path,
    *,
    limit: int = 200,
) -> ProcessorErrorsResponse:
    baseline_at = await get_baseline(sqlite_path)
    now = _now()
    window_start = _effective_window_start(now, baseline_at)
    return await asyncio.to_thread(
        _list_errors_sync,
        window_start,
        now,
        _iso_or_none(baseline_at),
        limit,
    )


def _list_errors_sync(
    window_start: datetime,
    now: datetime,
    baseline_iso: str | None,
    limit: int,
) -> ProcessorErrorsResponse:
    from core.models import IngestedItem

    rows = list(
        IngestedItem.objects.filter(
            processing_status="failed",
            processing_completed_at__gte=window_start,
        )
        .exclude(error_message__isnull=True)
        .exclude(error_message="")
        .order_by("-processing_completed_at")[:limit]
    )

    items: list[ProcessorErrorItem] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        content_type = payload.get("content_type")
        items.append(
            ProcessorErrorItem(
                failed_at=row.processing_completed_at.isoformat(),
                ingested_item_id=str(row.id),
                platform=row.platform,
                fetched_by_user_email=row.fetched_by_user_email,
                course_external_id=row.course_external_id,
                content_type=content_type if isinstance(content_type, str) else None,
                error_message=row.error_message,
            )
        )

    return ProcessorErrorsResponse(
        window_start=window_start.isoformat(),
        window_end=now.isoformat(),
        baseline_at=baseline_iso,
        items=items,
    )


async def reset_window(
    sqlite_path: Path,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> ProcessorWindowResetResult:
    previous_baseline = await get_baseline(sqlite_path)
    baseline_at = _now()
    baseline_iso = baseline_at.isoformat()

    async with aiosqlite.connect(sqlite_path) as conn:
        await conn.execute(
            """
            INSERT INTO processor_dashboard_state (id, baseline_at, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                baseline_at = excluded.baseline_at,
                updated_at = excluded.updated_at
            """,
            (baseline_iso, baseline_iso),
        )
        await conn.commit()

    await audit_writer.write(
        action="processor.window_reset",
        target="processor",
        before={"baseline_at": _iso_or_none(previous_baseline)},
        after={"baseline_at": baseline_iso},
        reason=reason,
    )

    return ProcessorWindowResetResult(
        baseline_at=baseline_iso,
        previous_baseline_at=_iso_or_none(previous_baseline),
    )
