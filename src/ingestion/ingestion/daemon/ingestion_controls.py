"""Live ingestion policy helpers for daemon-managed adapter fetches.

The daemon's overlay config owns operator policy. Platform adapters stay
focused on platform fetch/normalize concerns; daemon-only recency policy is
applied uniformly after normalization by wrapping ``adapter.fetch()``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ingestion.schemas import ContentType, FetchResult

if TYPE_CHECKING:
    from ingestion.daemon.config import DaemonConfig
    from ingestion.ports import Adapter, ProgressCallback
    from ingestion.schemas import FetchScope, NormalizedItem

_log = logging.getLogger(__name__)


def build_live_adapter(adapter: Adapter, daemon_config: DaemonConfig) -> Adapter:
    """Wrap an adapter with daemon-only live ingestion controls."""
    recency_days = daemon_config.ingestion_controls.recency_days
    if recency_days is None:
        return adapter
    return RecencyFilteringAdapter(adapter, recency_days=recency_days)


class RecencyFilteringAdapter:
    """Apply one daemon-managed post-normalize recency policy to fetches."""

    def __init__(self, delegate: Adapter, *, recency_days: int) -> None:
        self._delegate = delegate
        self._recency_days = recency_days
        self.platform = delegate.platform

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult:
        result = await self._delegate.fetch(
            credentials,
            cursor=cursor,
            scope=scope,
            on_progress=on_progress,
        )
        return apply_recency_filter(result, recency_days=self._recency_days)

    async def test_connection(self, credentials: dict) -> bool:
        return await self._delegate.test_connection(credentials)


def apply_recency_filter(
    result: FetchResult,
    *,
    recency_days: int | None,
    now: datetime | None = None,
) -> FetchResult:
    """Filter normalized items by one rolling cutoff, preserving the cursor."""
    if recency_days is None:
        return result

    cutoff = (now or datetime.now(tz=UTC)) - timedelta(days=recency_days)
    kept_items = []
    skipped_by_scope: dict[str, int] = {}
    available_scopes = frozenset(result.stats.per_scope_stats)

    for item in result.items:
        if item.published_at is None:
            _log.warning(
                "keeping item during recency filter because it is missing published_at: "
                "platform=%s external_id=%s content_type=%s",
                item.platform,
                item.external_id,
                item.content_type.value,
            )
            kept_items.append(item)
            continue
        if item.published_at >= cutoff:
            kept_items.append(item)
            continue

        scope_name = _scope_name_for_item(item, available_scopes)
        if scope_name is not None:
            skipped_by_scope[scope_name] = skipped_by_scope.get(scope_name, 0) + 1

    total_skipped = len(result.items) - len(kept_items)
    if total_skipped == 0:
        return result.model_copy(
            deep=True,
            update={
                "items": kept_items,
                "stats": result.stats.model_copy(
                    deep=True,
                    update={"items_fetched": len(kept_items)},
                ),
            },
        )

    per_scope_stats = {
        name: stat.model_copy(deep=True) for name, stat in result.stats.per_scope_stats.items()
    }
    for scope_name, skipped in skipped_by_scope.items():
        stat = per_scope_stats.get(scope_name)
        if stat is None:
            continue
        kept_in_scope = max(stat.items_fetched - skipped, 0)
        per_scope_stats[scope_name] = stat.model_copy(
            update={
                "items_fetched": kept_in_scope,
                "items_skipped": stat.items_skipped + skipped,
            }
        )

    return result.model_copy(
        deep=True,
        update={
            "items": kept_items,
            "stats": result.stats.model_copy(
                deep=True,
                update={
                    "items_fetched": len(kept_items),
                    "items_skipped": result.stats.items_skipped + total_skipped,
                    "per_scope_stats": per_scope_stats,
                },
            ),
        },
    )


def _scope_name_for_item(
    item: NormalizedItem,
    available_scopes: frozenset[str],
) -> str | None:
    by_content_type = {
        ContentType.RAW_MESSAGE: "messages",
        ContentType.ASSIGNMENT: "assignments",
        ContentType.EVENT: "events",
        ContentType.MATERIAL: "materials",
    }
    scope_name = by_content_type.get(item.content_type)
    if scope_name is None and item.content_type is ContentType.ANNOUNCEMENT:
        scope_name = "forums" if item.platform == "moodle" else None
    if scope_name in available_scopes:
        return scope_name
    if len(available_scopes) == 1:
        return next(iter(available_scopes))
    return None
