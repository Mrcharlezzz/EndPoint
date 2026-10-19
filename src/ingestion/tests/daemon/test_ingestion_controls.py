from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.ingestion_controls import apply_recency_filter
from ingestion.schemas import ContentType, FetchResult, NormalizedItem, ScopeStats, SyncStats


def _make_item(
    external_id: str,
    *,
    platform: str = "telegram",
    content_type: ContentType = ContentType.RAW_MESSAGE,
    published_at: datetime | None,
) -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform=platform,
        content_type=content_type,
        title=external_id,
        body=external_id,
        published_at=published_at,
        raw_payload={},
    )


def test_apply_recency_filter_drops_old_items_and_preserves_cursor() -> None:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    result = FetchResult(
        items=[
            _make_item("old", published_at=now - timedelta(days=45)),
            _make_item("new", published_at=now - timedelta(days=5)),
        ],
        cursor={"last_update_id": 42},
        stats=SyncStats(
            items_fetched=2,
            per_scope_stats={"messages": ScopeStats(items_fetched=2)},
        ),
    )

    filtered = apply_recency_filter(result, recency_days=30, now=now)

    assert [item.external_id for item in filtered.items] == ["new"]
    assert filtered.cursor == {"last_update_id": 42}
    assert filtered.stats.items_fetched == 1
    assert filtered.stats.items_skipped == 1
    assert filtered.stats.per_scope_stats["messages"].items_fetched == 1
    assert filtered.stats.per_scope_stats["messages"].items_skipped == 1


def test_apply_recency_filter_keeps_items_without_published_at_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    result = FetchResult(
        items=[_make_item("missing-ts", published_at=None)],
        stats=SyncStats(
            items_fetched=1,
            per_scope_stats={"messages": ScopeStats(items_fetched=1)},
        ),
    )

    with caplog.at_level(logging.WARNING):
        filtered = apply_recency_filter(result, recency_days=30, now=now)

    assert [item.external_id for item in filtered.items] == ["missing-ts"]
    assert filtered.stats.items_fetched == 1
    assert filtered.stats.items_skipped == 0
    assert "missing published_at" in caplog.text
    assert "missing-ts" in caplog.text


def test_effective_ingestion_config_uses_global_recency_days() -> None:
    config = DaemonConfig.model_validate({"ingestion_controls": {"recency_days": 21}})

    assert config.ingestion_controls.recency_days == 21


def test_recency_days_must_be_positive() -> None:
    config_data = {"ingestion_controls": {"recency_days": 0}}

    with pytest.raises(ValueError, match="greater than or equal to 1"):
        DaemonConfig.model_validate(config_data)
