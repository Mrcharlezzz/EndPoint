from __future__ import annotations

import uuid

import pytest
from ingestion.schemas import (
    ContentType,
    FetchResult,
    FetchScope,
    NormalizedItem,
    SyncStats,
)

# --- Inline fakes for pipeline tests ---


class InMemoryContentSink:
    """Fake ContentSink for testing."""

    def __init__(self) -> None:
        self.items: dict[str, NormalizedItem] = {}

    async def exists(self, platform: str, external_id: str) -> bool:
        return f"{platform}:{external_id}" in self.items

    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
        # Pipeline calls ``exists_batch`` directly per the Protocol; the
        # in-memory check matches per-id ``exists`` semantics.
        return {xid for xid in external_ids if f"{platform}:{xid}" in self.items}

    async def upsert(self, item: NormalizedItem) -> str:
        key = f"{item.platform}:{item.external_id}"
        self.items[key] = item
        return key

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        return [await self.upsert(item) for item in items]


class InMemorySyncState:
    """Fake SyncStateStore for testing."""

    def __init__(self) -> None:
        self.cursors: dict[str, dict] = {}

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None:
        return self.cursors.get(f"{user_id}:{platform}:{scope}")

    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None:
        self.cursors[f"{user_id}:{platform}:{scope}"] = cursor


class InMemorySyncLogger:
    """Fake SyncLogger for testing."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def log_start(self, user_id: str, platform: str) -> str:
        sync_id = str(uuid.uuid4())
        self.events.append({"type": "start", "sync_id": sync_id})
        return sync_id

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        self.events.append({"type": "complete", "sync_id": sync_id, "stats": stats})

    async def log_failure(self, sync_id: str, error: str) -> None:
        self.events.append({"type": "failure", "sync_id": sync_id, "error": error})


# --- Fixtures ---


@pytest.fixture
def content_sink() -> InMemoryContentSink:
    return InMemoryContentSink()


@pytest.fixture
def sync_state() -> InMemorySyncState:
    return InMemorySyncState()


@pytest.fixture
def sync_logger() -> InMemorySyncLogger:
    return InMemorySyncLogger()


@pytest.fixture
def sample_item() -> NormalizedItem:
    return NormalizedItem(
        external_id="teams_msg_abc123",
        platform="teams",
        content_type=ContentType.ANNOUNCEMENT,
        title="Exam postponed",
        body="The midterm exam has been moved to next Friday.",
        course_name="CS101 Algorithms",
        course_external_id="team_cs101",
        author="Prof. Smith",
        raw_payload={"id": "abc123", "body": {"content": "<p>exam moved</p>"}},
    )


@pytest.fixture
def sample_fetch_result(sample_item: NormalizedItem) -> FetchResult:
    return FetchResult(
        items=[sample_item],
        files=[],
        cursor={"last_sync_at": "2026-03-12T10:00:00Z"},
        errors=[],
        stats=SyncStats(items_fetched=1),
    )


class FakeAdapter:
    """Fake adapter for pipeline tests."""

    platform = "test"

    def __init__(self, result: FetchResult) -> None:
        self._result = result
        self.fetch_calls: list[dict] = []

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
    ) -> FetchResult:
        self.fetch_calls.append({"credentials": credentials, "cursor": cursor, "scope": scope})
        return self._result

    async def test_connection(self, credentials: dict) -> bool:
        return True


@pytest.fixture
def fake_adapter(sample_fetch_result: FetchResult) -> FakeAdapter:
    return FakeAdapter(sample_fetch_result)
