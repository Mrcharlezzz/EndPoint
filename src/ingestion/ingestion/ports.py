"""Port interfaces (protocols) for the ingestion pipeline.

Defines the abstract contracts that adapters, sinks, stores, and loggers
must satisfy. Django and CLI each provide concrete implementations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ingestion.schemas import FetchResult, FetchScope, NormalizedItem, SyncStats

# Progress callback: (scope/category, detail message)
# e.g. ("messages", "#General: 12 messages")
ProgressCallback = Callable[[str, str], None]


class Adapter(Protocol):
    """Protocol for platform adapters.

    Adapters are stateless: credentials and cursors are passed as arguments.
    """

    platform: str

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult: ...

    async def test_connection(self, credentials: dict) -> bool: ...


class Normalizer(Protocol):
    """Protocol for normalizing raw API responses into NormalizedItems."""

    def normalize(self, raw: dict, **context: object) -> NormalizedItem | None: ...


class TokenStore(Protocol):
    """Read/write platform credentials.

    Django implements with encrypted PlatformConnection model.
    CLI implements with local JSON file.
    """

    async def get_credentials(self, user_id: str, platform: str) -> dict | None: ...
    async def update_credentials(self, user_id: str, platform: str, credentials: dict) -> None: ...
    async def mark_expired(self, user_id: str, platform: str) -> None: ...


class ContentSink(Protocol):
    """Receive normalized items.

    Django implements with ContentItem model + dedup logic.
    CLI implements with JSON file export.

    ``exists_batch`` is a Protocol member every conformant sink must
    implement. Production sinks (``PostgresContentSink``) ship a real
    batched query (one round-trip per platform). Sinks that have not
    migrated to a true batched query can delegate to
    :func:`default_exists_batch` which preserves the per-id loop, but the
    pipeline always calls ``exists_batch`` directly — there is no
    runtime ``hasattr`` fallback.
    """

    async def exists(self, platform: str, external_id: str) -> bool: ...
    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]: ...
    async def upsert(self, item: NormalizedItem) -> str: ...
    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]: ...


async def default_exists_batch(
    sink: ContentSink, platform: str, external_ids: list[str]
) -> set[str]:
    """Per-id loop helper for sinks lacking a native batched query.

    Provided as a delegate target for ``ContentSink`` implementations that
    cannot batch (e.g. file-backed dev sinks). Returns the set of
    external_ids that already exist in the sink. The empty-list
    short-circuit avoids issuing any backend call when there is nothing
    to dedup.

    The pipeline does NOT call this — every sink declares ``exists_batch``
    and the pipeline calls it directly. Use this from inside a sink's
    ``exists_batch`` implementation if a native batched query is not
    practical for that backend.
    """
    if not external_ids:
        return set()
    found: set[str] = set()
    for external_id in external_ids:
        if await sink.exists(platform, external_id):
            found.add(external_id)
    return found


class SyncStateStore(Protocol):
    """Persist sync cursors for incremental fetching."""

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None: ...
    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None: ...


class SyncLogger(Protocol):
    """Audit log for sync operations."""

    async def log_start(self, user_id: str, platform: str) -> str: ...
    async def log_complete(self, sync_id: str, stats: SyncStats) -> None: ...
    async def log_failure(self, sync_id: str, error: str) -> None: ...
