"""In-memory fakes the scheduler integration tests compose around.

These are deliberately loose (no protocol adherence enforced by a type check)
because the pipeline's Protocols are ``dict``-untyped and duck-typed. Tests
only need the subset of methods :func:`run_sync` actually calls.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ingestion.schemas import FetchResult, NormalizedItem, SyncStats


@dataclass
class FakeAdapter:
    platform: str = "moodle"
    result_factory: Callable[[], FetchResult] | None = None
    fetch_calls: list[dict[str, Any]] = field(default_factory=list)
    before_return: Awaitable[None] | None = None
    fail_with: Exception | None = None

    async def fetch(
        self,
        credentials: dict[str, Any],
        cursor: dict[str, Any] | None = None,
        scope: Any = None,
    ) -> FetchResult:
        self.fetch_calls.append({"credentials": credentials, "cursor": cursor, "scope": scope})
        if self.before_return is not None:
            await self.before_return
        if self.fail_with is not None:
            raise self.fail_with
        if self.result_factory is not None:
            return self.result_factory()
        return FetchResult(items=[], cursor={"ts": "v1"}, stats=SyncStats())

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        return True


class InMemoryContentSink:
    def __init__(self) -> None:
        self.items: list[NormalizedItem] = []

    async def exists(self, platform: str, external_id: str) -> bool:
        return any(i.platform == platform and i.external_id == external_id for i in self.items)

    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
        # Linear scan over the in-memory items; matches the per-id ``exists``
        # semantics so the pipeline's batched dedup still works for fakes.
        present = {i.external_id for i in self.items if i.platform == platform}
        return {xid for xid in external_ids if xid in present}

    async def upsert(self, item: NormalizedItem) -> str:
        self.items.append(item)
        return f"{item.platform}:{item.external_id}"

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        return [await self.upsert(item) for item in items]


class InMemorySyncStateStore:
    def __init__(self) -> None:
        self.cursors: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict[str, Any] | None:
        return self.cursors.get((user_id, platform, scope))

    async def set_cursor(
        self,
        user_id: str,
        platform: str,
        scope: str,
        cursor: dict[str, Any],
    ) -> None:
        self.cursors[(user_id, platform, scope)] = cursor


class InMemorySyncLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.started: dict[str, tuple[str, str]] = {}
        self.completed: dict[str, SyncStats] = {}
        self.failed: dict[str, str] = {}

    async def log_start(self, user_id: str, platform: str) -> str:
        sync_id = str(uuid.uuid4())
        self.started[sync_id] = (user_id, platform)
        self.events.append(("start", sync_id))
        return sync_id

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        self.completed[sync_id] = stats
        self.events.append(("complete", sync_id))

    async def log_failure(self, sync_id: str, error: str) -> None:
        self.failed[sync_id] = error
        self.events.append(("failure", sync_id))


class InMemoryTokenStore:
    def __init__(self, creds: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.creds: dict[tuple[str, str], dict[str, Any]] = dict(creds or {})
        self.expired: set[tuple[str, str]] = set()

    async def get_credentials(self, user_id: str, platform: str) -> dict[str, Any] | None:
        return self.creds.get((user_id, platform))

    async def update_credentials(
        self, user_id: str, platform: str, credentials: dict[str, Any]
    ) -> None:
        self.creds[(user_id, platform)] = credentials
        self.expired.discard((user_id, platform))

    async def mark_expired(self, user_id: str, platform: str) -> None:
        self.expired.add((user_id, platform))


async def drive_event_loop(count: int = 10) -> None:
    """Yield to the loop ``count`` times so other tasks can make progress."""
    for _ in range(count):
        await asyncio.sleep(0)
