"""Tests for the pipeline orchestrator using fake adapter and in-memory ports."""

from __future__ import annotations

import pytest
from ingestion.daemon.services.exceptions import AuthenticationFailedError
from ingestion.exceptions import TokenExpiredError
from ingestion.pipeline import run_sync
from ingestion.schemas import (
    ContentType,
    ErrorCode,
    FetchError,
    FetchResult,
    FetchScope,
    NormalizedItem,
    SyncStats,
)

# Import fakes that conftest defines (pytest auto-loads conftest.py)
if True:  # avoid circular import at test collection time
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from conftest import FakeAdapter, InMemoryContentSink, InMemorySyncLogger, InMemorySyncState


def _make_item(external_id: str, title: str = "Test") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform="test",
        content_type=ContentType.ANNOUNCEMENT,
        title=title,
        body="Test body",
        raw_payload={},
    )


async def test_full_sync_cycle(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    items = [_make_item("msg_1"), _make_item("msg_2")]
    result = FetchResult(
        items=items,
        cursor={"last_sync_at": "2026-03-12T10:00:00Z"},
        stats=SyncStats(items_fetched=2),
    )
    adapter = FakeAdapter(result)

    stats, sync_id = await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    assert stats.items_fetched == 2
    assert stats.items_new == 2
    assert stats.items_updated == 0
    assert stats.errors == 0
    assert isinstance(sync_id, str) and sync_id
    assert len(content_sink.items) == 2
    complete_events = [e for e in sync_logger.events if e["type"] == "complete"]
    assert len(complete_events) == 1


async def test_cursor_saved_on_completion(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    cursor_data = {"continuation_token": "abc123"}
    result = FetchResult(
        items=[_make_item("msg_1")],
        cursor=cursor_data,
        stats=SyncStats(items_fetched=1),
    )
    adapter = FakeAdapter(result)

    await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    saved_cursor = await sync_state.get_cursor("user1", "test", "default")
    assert saved_cursor == cursor_data


async def test_cursor_not_saved_when_none(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    result = FetchResult(
        items=[_make_item("msg_1")],
        cursor=None,
        stats=SyncStats(items_fetched=1),
    )
    adapter = FakeAdapter(result)

    await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    saved_cursor = await sync_state.get_cursor("user1", "test", "default")
    assert saved_cursor is None


async def test_existing_items_counted_as_updated(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    # Pre-populate an item
    await content_sink.upsert(_make_item("msg_1"))

    result = FetchResult(
        items=[_make_item("msg_1", title="Updated"), _make_item("msg_2")],
        cursor=None,
        stats=SyncStats(items_fetched=2),
    )
    adapter = FakeAdapter(result)

    stats, _sync_id = await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    assert stats.items_updated == 1
    assert stats.items_new == 1


async def test_token_expired_raises_authentication_failed(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """Slice 8: pipeline now raises AuthenticationFailedError on 401
    after marking the credential expired and logging the failure.
    The typed error is what the scheduler's worker pool pivots on."""

    class TokenExpiredAdapter:
        platform = "test"

        async def fetch(self, credentials, cursor=None, scope=None):
            raise TokenExpiredError("Token expired")

    with pytest.raises(AuthenticationFailedError) as exc_info:
        await run_sync(
            adapter=TokenExpiredAdapter(),
            credentials={"token": "expired"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="user1",
        )

    assert exc_info.value.http_status == 401
    assert exc_info.value.platform == "test"
    assert exc_info.value.user_email == "user1"
    failure_events = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failure_events) == 1


async def test_fetch_errors_logged(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    result = FetchResult(
        items=[_make_item("msg_1")],
        errors=[
            FetchError(
                code=ErrorCode.INSUFFICIENT_SCOPE,
                message="Cannot access assignments",
                scope="assignments",
                recoverable=True,
            )
        ],
        stats=SyncStats(items_fetched=1, errors=1),
    )
    adapter = FakeAdapter(result)

    stats, _sync_id = await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    assert stats.items_fetched == 1
    assert stats.errors == 1
    complete_events = [e for e in sync_logger.events if e["type"] == "complete"]
    assert len(complete_events) == 1


async def test_adapter_items_skipped_propagate_to_sync_stats(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    result = FetchResult(
        items=[_make_item("msg_1")],
        stats=SyncStats(items_fetched=1, items_skipped=2),
    )
    adapter = FakeAdapter(result)

    stats, _sync_id = await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    assert stats.items_fetched == 1
    assert stats.items_skipped == 2


async def test_scope_passed_to_adapter(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    result = FetchResult(items=[], stats=SyncStats())
    adapter = FakeAdapter(result)
    scope = frozenset({FetchScope.MESSAGES})

    await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
        scope=scope,
    )

    assert adapter.fetch_calls[0]["scope"] == scope


async def test_previous_cursor_loaded(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    await sync_state.set_cursor("user1", "test", "default", {"continuation_token": "prev"})

    result = FetchResult(items=[], stats=SyncStats())
    adapter = FakeAdapter(result)

    await run_sync(
        adapter=adapter,
        credentials={"token": "test"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )

    assert adapter.fetch_calls[0]["cursor"] == {"continuation_token": "prev"}
