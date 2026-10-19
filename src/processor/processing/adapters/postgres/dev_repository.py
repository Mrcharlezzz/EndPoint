# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from processing.adapters.postgres import mapper
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.models.envelope import ProcessingEnvelope


class DevPostgresEntityRepository(PostgresEntityRepository):
    """Dev-only repository that ignores staged or historically published rows."""

    def __init__(self, service_start: datetime) -> None:
        self._service_start = service_start

    def _get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        from core.models.ingested import IngestedItem

        queryset = IngestedItem.objects.filter(
            processing_status="pending",
            created_at__gt=self._service_start,
        ).order_by("created_at")

        batch: list[ProcessingEnvelope] = []
        for item in queryset.iterator():
            if not self._published_at_is_after_service_start(item.payload):
                continue
            batch.append(mapper.orm_to_envelope(item))
            if len(batch) >= limit:
                break
        return batch

    def _published_at_is_after_service_start(self, payload: dict[str, Any]) -> bool:
        published_at_raw = payload.get("published_at")
        if not isinstance(published_at_raw, str) or not published_at_raw:
            return False

        try:
            published_at = datetime.fromisoformat(
                published_at_raw.replace("Z", "+00:00")
            )
        except ValueError:
            return False

        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)

        return published_at > self._service_start
