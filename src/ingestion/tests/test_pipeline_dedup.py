"""Pipeline regression tests: batched dedup is a single ``exists_batch`` call.

The N+1 bug pre-S11 issued one ``exists`` call per item — for a 100-item
fetch this round-tripped to Postgres 100 times. The pipeline now calls
``exists_batch`` exactly ONCE per platform present in the fetch (and
short-circuits when there are no items).

This module pins both the call count contract and the dedup correctness:

* New items vs already-existing items split correctly into ``items_new`` /
  ``items_updated``.
* A multi-platform fetch issues one ``exists_batch`` per platform.
* A sink that delegates ``exists_batch`` to the per-id ``default_exists_batch``
  helper still works end-to-end through the pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ingestion.pipeline import run_sync
from ingestion.schemas import (
    ContentType,
    FetchResult,
    NormalizedItem,
    SyncStats,
)

# Reuse the conftest fakes — same shape the existing pipeline tests use.
sys.path.insert(0, str(Path(__file__).parent))
from conftest import FakeAdapter, InMemorySyncLogger, InMemorySyncState  # noqa: E402


def _make_item(external_id: str, *, platform: str = "test") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform=platform,
        content_type=ContentType.ANNOUNCEMENT,
        title="Title",
        body="Body",
        raw_payload={},
    )


class CountingSink:
    """ContentSink that counts every call so we can assert exactly-one-batch.

    Provides a true ``exists_batch`` (one round-trip per platform) plus the
    legacy ``exists`` so the per-item path can be flagged via call counts.
    """

    def __init__(self, existing_keys: set[tuple[str, str]] | None = None) -> None:
        # (platform, external_id) tuples that should report as existing.
        self._existing = existing_keys or set()
        self.exists_calls = 0
        self.exists_batch_calls = 0
        self.exists_batch_args: list[tuple[str, list[str]]] = []
        self.upsert_calls = 0
        self.upsert_batch_calls = 0
        self.upserted: list[NormalizedItem] = []

    async def exists(self, platform: str, external_id: str) -> bool:
        self.exists_calls += 1
        return (platform, external_id) in self._existing

    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
        self.exists_batch_calls += 1
        # Stash the args so callers can assert per-platform partitioning.
        self.exists_batch_args.append((platform, list(external_ids)))
        return {xid for xid in external_ids if (platform, xid) in self._existing}

    async def upsert(self, item: NormalizedItem) -> str:
        self.upsert_calls += 1
        self.upserted.append(item)
        return f"{item.platform}:{item.external_id}"

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        self.upsert_batch_calls += 1
        return [await self.upsert(item) for item in items]


class LegacySinkOnlyExists:
    """Sink whose ``exists_batch`` delegates to ``default_exists_batch``.

    Models the migration path for a backend that cannot batch natively:
    declare ``exists_batch`` per the Protocol but route it through the
    per-id helper. The pipeline still calls ``exists_batch`` once; the
    helper translates that into N ``exists()`` calls under the hood.
    """

    def __init__(self, existing_keys: set[tuple[str, str]] | None = None) -> None:
        self._existing = existing_keys or set()
        self.exists_calls = 0
        self.upserted: list[NormalizedItem] = []

    async def exists(self, platform: str, external_id: str) -> bool:
        self.exists_calls += 1
        return (platform, external_id) in self._existing

    async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
        from ingestion.ports import default_exists_batch

        return await default_exists_batch(self, platform, external_ids)  # type: ignore[arg-type]

    async def upsert(self, item: NormalizedItem) -> str:
        self.upserted.append(item)
        return f"{item.platform}:{item.external_id}"

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        return [await self.upsert(item) for item in items]


# ---------------------------------------------------------------------------
# Regression: exactly-one exists_batch call for a 100-item fetch.
# ---------------------------------------------------------------------------


async def test_n_plus_one_regression_single_batch_call(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """The pipeline must issue exactly ONE ``exists_batch`` call for 100 items."""
    items = [_make_item(f"msg_{i}") for i in range(100)]
    result = FetchResult(
        items=items,
        cursor={"k": "v"},
        stats=SyncStats(items_fetched=100),
    )
    sink = CountingSink()
    await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    # The N+1 bug would have called exists 100 times; we want zero per-item
    # exists() calls and exactly one exists_batch call.
    assert sink.exists_calls == 0
    assert sink.exists_batch_calls == 1
    # All 100 ids landed in the single batch call.
    assert len(sink.exists_batch_args[0][1]) == 100


async def test_dedup_correctness_existing_items_marked_updated(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """Items that exist_batch reports as existing land in items_updated."""
    items = [_make_item(f"msg_{i}") for i in range(5)]
    existing = {("test", "msg_0"), ("test", "msg_2")}
    result = FetchResult(items=items, stats=SyncStats(items_fetched=5))
    sink = CountingSink(existing_keys=existing)

    stats, _ = await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    assert stats.items_updated == 2
    assert stats.items_new == 3
    # All 5 still get upserted (sink is the source-of-truth for the row).
    assert sink.upsert_batch_calls == 1
    assert len(sink.upserted) == 5


async def test_empty_fetch_skips_exists_batch(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """Empty ``fetch_result.items`` short-circuits — no exists_batch call."""
    result = FetchResult(items=[], stats=SyncStats(items_fetched=0))
    sink = CountingSink()
    stats, _ = await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    assert sink.exists_batch_calls == 0
    assert sink.exists_calls == 0
    assert stats.items_fetched == 0


async def test_multi_platform_fetch_groups_by_platform(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """Items from multiple platforms get one exists_batch call per platform."""
    items = [
        _make_item("teams_msg_a", platform="teams"),
        _make_item("teams_msg_b", platform="teams"),
        _make_item("moodle_msg_x", platform="moodle"),
    ]
    result = FetchResult(items=items, stats=SyncStats(items_fetched=3))
    sink = CountingSink()
    await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    # One call per distinct platform present in the fetch.
    assert sink.exists_batch_calls == 2
    seen_platforms = {plat for plat, _ in sink.exists_batch_args}
    assert seen_platforms == {"teams", "moodle"}
    # Per-platform partitioning: the teams call has both teams ids.
    teams_call = next(args for plat, args in sink.exists_batch_args if plat == "teams")
    assert sorted(teams_call) == ["teams_msg_a", "teams_msg_b"]
    moodle_call = next(args for plat, args in sink.exists_batch_args if plat == "moodle")
    assert moodle_call == ["moodle_msg_x"]


async def test_default_exists_batch_helper_via_pipeline(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """A sink whose ``exists_batch`` delegates to the helper still works end-to-end.

    This documents the supported migration path for backends that cannot
    batch natively: implement ``exists_batch`` via :func:`default_exists_batch`,
    which loops the per-id ``exists()`` under the hood. The pipeline calls
    ``exists_batch`` exactly once and the helper accounts for the per-id calls.
    """
    items = [_make_item(f"msg_{i}") for i in range(3)]
    existing = {("test", "msg_0"), ("test", "msg_1")}
    result = FetchResult(items=items, stats=SyncStats(items_fetched=3))
    sink = LegacySinkOnlyExists(existing_keys=existing)

    stats, _ = await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    # Helper loops per-id: 3 items → 3 exists() calls under the hood.
    assert sink.exists_calls == 3
    assert stats.items_updated == 2
    assert stats.items_new == 1


async def test_default_exists_batch_short_circuits_empty() -> None:
    """``default_exists_batch`` returns empty without touching the sink."""
    from ingestion.ports import default_exists_batch

    sink = LegacySinkOnlyExists()
    result = await default_exists_batch(sink, "test", [])  # type: ignore[arg-type]
    assert result == set()
    assert sink.exists_calls == 0


async def test_default_exists_batch_iterates_per_id() -> None:
    """``default_exists_batch`` calls ``exists`` once per id and folds matches."""
    from ingestion.ports import default_exists_batch

    sink = LegacySinkOnlyExists(existing_keys={("test", "a"), ("test", "c")})
    result = await default_exists_batch(sink, "test", ["a", "b", "c"])  # type: ignore[arg-type]
    assert result == {"a", "c"}
    assert sink.exists_calls == 3


async def test_pipeline_with_legacy_sink_marks_existing_correctly(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """End-to-end: pipeline + legacy sink + dedup correctness."""
    items = [_make_item(f"msg_{i}") for i in range(4)]
    existing = {("test", "msg_3")}
    result = FetchResult(items=items, stats=SyncStats(items_fetched=4))
    sink = LegacySinkOnlyExists(existing_keys=existing)

    stats, _ = await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    assert stats.items_updated == 1
    assert stats.items_new == 3
    # Every item still flows through upsert.
    assert len(sink.upserted) == 4


async def test_existing_id_count_drives_items_updated(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """Spot-check: items_updated == count of ids the batch reported as existing."""
    items = [_make_item(f"msg_{i}") for i in range(10)]
    existing = {("test", f"msg_{i}") for i in range(7)}
    result = FetchResult(items=items, stats=SyncStats(items_fetched=10))
    sink = CountingSink(existing_keys=existing)

    stats, _ = await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    assert stats.items_updated == 7
    assert stats.items_new == 3
    assert sink.exists_batch_calls == 1


@pytest.mark.parametrize("size", [1, 10, 50, 200])
async def test_exactly_one_batch_call_at_various_sizes(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    size: int,
) -> None:
    items = [_make_item(f"msg_{i}") for i in range(size)]
    result = FetchResult(items=items, stats=SyncStats(items_fetched=size))
    sink = CountingSink()

    await run_sync(
        adapter=FakeAdapter(result),
        credentials={"token": "t"},
        content_sink=sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="user1",
    )
    # One call per platform; one platform here → one call.
    assert sink.exists_batch_calls == 1
    assert len(sink.exists_batch_args[0][1]) == size
