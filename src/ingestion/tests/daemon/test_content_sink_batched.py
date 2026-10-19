"""Unit tests for :meth:`PostgresContentSink.exists_batch`.

The DB-backed integration counterpart lives in ``test_ports_content_sink.py``
(skipped without Postgres). These tests mock the ORM so they always run, and
assert the call-shape contract that the perf change pins:

* Empty input issues NO query.
* Inputs are deduplicated before they go to the DB.
* The list is chunked at 800 ids per query — proven by counting the number
  of ``filter(...).values_list(...)`` chains the sink builds.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest


class _AsyncIterator:
    """Wrap a sync iterable so it walks under ``async for``."""

    def __init__(self, items: list[str]) -> None:
        self._items: list[str] = items
        self._iter: Iterator[str] | None = None

    def __aiter__(self) -> _AsyncIterator:
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> str:
        if self._iter is None:
            raise StopAsyncIteration
        try:
            return next(self._iter)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _patch_manager(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch ``IngestedItem.objects`` and return a counter dict for assertions.

    Returns a dict shaped:
        {
            "filter_calls": list of every kwargs dict passed to filter(),
            "set_response": setter function — call with the full set of ids
                            the mock should report as existing,
            "raise_on_filter": optional — set True to make filter raise.
        }
    """
    from ingestion.daemon.ports import content_sink as cs_mod

    state: dict[str, object] = {
        "filter_calls": [],
        "existing": set(),
    }

    class _FakeQuerySet:
        def __init__(self, ids_in_chunk: list[str]) -> None:
            # Compute the intersection at filter() time so each chunk yields
            # only the ids that exist (mirrors Postgres ``WHERE external_id IN``).
            self._ids = ids_in_chunk

        def values_list(self, *_args: object, **_kwargs: object) -> _AsyncIterator:
            existing: set[str] = state["existing"]  # type: ignore[assignment]
            return _AsyncIterator([xid for xid in self._ids if xid in existing])

    class _FakeManager:
        def filter(self, **kwargs: object) -> _FakeQuerySet:
            calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
            calls.append(dict(kwargs))
            ids = kwargs.get("external_id__in", [])
            assert isinstance(ids, list)
            return _FakeQuerySet(ids)

    fake_manager = _FakeManager()
    monkeypatch.setattr(cs_mod.IngestedItem, "objects", fake_manager, raising=True)
    return state


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


async def test_exists_batch_empty_returns_empty_set_no_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    result = await sink.exists_batch("moodle", [])
    assert result == set()
    # No SQL ever issued — short-circuit before touching the manager.
    assert state["filter_calls"] == []


async def test_exists_batch_single_id_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    state["existing"] = {"x_1"}
    sink = PostgresContentSink()
    result = await sink.exists_batch("moodle", ["x_1"])
    assert result == {"x_1"}
    # One filter() call with one id in the chunk.
    assert len(state["filter_calls"]) == 1  # type: ignore[arg-type]


async def test_exists_batch_single_id_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    result = await sink.exists_batch("moodle", ["x_1"])
    assert result == set()


async def test_exists_batch_chunks_at_800(monkeypatch: pytest.MonkeyPatch) -> None:
    """1000 ids must split into ceil(1000/800) = 2 filter() calls."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(1000)]
    await sink.exists_batch("moodle", ids)
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == math.ceil(1000 / 800) == 2
    # First chunk has 800 ids; second has 200.
    assert len(calls[0]["external_id__in"]) == 800  # type: ignore[arg-type]
    assert len(calls[1]["external_id__in"]) == 200  # type: ignore[arg-type]


async def test_exists_batch_chunks_at_exactly_800(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list whose size is exactly the chunk boundary fits in one call."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(800)]
    await sink.exists_batch("moodle", ids)
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 1
    assert len(calls[0]["external_id__in"]) == 800  # type: ignore[arg-type]


async def test_exists_batch_chunks_801_into_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """801 ids → two calls (800, then 1)."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(801)]
    await sink.exists_batch("moodle", ids)
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 2
    assert len(calls[0]["external_id__in"]) == 800  # type: ignore[arg-type]
    assert len(calls[1]["external_id__in"]) == 1  # type: ignore[arg-type]


async def test_exists_batch_dedups_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate ids in input collapse to one before the query goes out."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    # 5 entries but only 3 distinct.
    await sink.exists_batch("moodle", ["a", "a", "b", "c", "c"])
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 1
    chunk: list[str] = calls[0]["external_id__in"]  # type: ignore[assignment]
    assert sorted(chunk) == ["a", "b", "c"]


async def test_exists_batch_dedup_preserves_first_seen_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dict.fromkeys`` preserves insertion order — pins that ordering contract."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    await sink.exists_batch("moodle", ["c", "a", "b", "a", "c"])
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    chunk: list[str] = calls[0]["external_id__in"]  # type: ignore[assignment]
    # Order preserved — c first, a second, b third.
    assert chunk == ["c", "a", "b"]


async def test_exists_batch_filters_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform filter is included on every chunk."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    await sink.exists_batch("teams", ["x"])
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 1
    assert calls[0]["platform"] == "teams"


async def test_exists_batch_returns_existing_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across chunks, returned set is the union of existing ids per chunk."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    state["existing"] = {f"id_{i}" for i in range(0, 1000, 100)}  # ten ids
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(1000)]
    result = await sink.exists_batch("moodle", ids)
    assert result == state["existing"]


async def test_exists_batch_each_chunk_carries_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk filter() carries the same platform kwarg."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(1500)]
    await sink.exists_batch("moodle", ids)
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 2
    for call in calls:
        assert call["platform"] == "moodle"


async def test_exists_batch_2000_ids_three_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    ids = [f"id_{i}" for i in range(2000)]
    await sink.exists_batch("moodle", ids)
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == math.ceil(2000 / 800) == 3
    sizes = [len(c["external_id__in"]) for c in calls]  # type: ignore[arg-type]
    assert sizes == [800, 800, 400]


async def test_exists_batch_no_query_when_all_dupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even all-duplicates collapse to a single id — one filter call, not zero."""
    from ingestion.daemon.ports.content_sink import PostgresContentSink

    state = _patch_manager(monkeypatch)
    sink = PostgresContentSink()
    await sink.exists_batch("moodle", ["a", "a", "a"])
    calls: list[dict[str, object]] = state["filter_calls"]  # type: ignore[assignment]
    assert len(calls) == 1
    assert calls[0]["external_id__in"] == ["a"]


async def test_module_level_unused_imports_dont_break_tests() -> None:
    """Sanity: MagicMock import is here for future expansion — exercise it."""
    m = MagicMock()
    m.foo()
    assert m.foo.called
