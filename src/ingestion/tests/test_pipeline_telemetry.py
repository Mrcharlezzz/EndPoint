"""Slice 13 part A: per-sync structlog telemetry events emitted from
:func:`ingestion.pipeline.run_sync`.

Pin the contract that the dashboard and alert engine consume:

* ``sync.started``, ``sync.scope_progress`` (one per scope), ``sync.upserting``,
  ``sync.upserted``, ``sync.finished`` fire in that order on a happy path.
* ``sync.upserting`` + ``sync.upserted`` are paired even on an empty fetch so
  dashboards can rely on a 1:1 pairing.
* The terminal ``sync.finished`` event is emitted on every code path
  (success, auth-failure, IngestionError, unexpected exception) with the
  right ``failure_reason`` discriminator.
* Every event carries the ``sync_id`` bound by ``bind_sync_context`` so the
  drill-down in the UI can correlate events back to the originating sync row.

The tests use ``structlog.testing.LogCapture`` to grab the structured events
without round-tripping through the JSON renderer; that keeps the assertions
on the keys we actually pivot on (``sync_id``, ``outcome``,
``failure_reason``) and avoids brittle JSON parsing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import ingestion.pipeline as pipeline_module
import pytest
import structlog
from ingestion.daemon.services.exceptions import AuthenticationFailedError
from ingestion.exceptions import IngestionError, TokenExpiredError
from ingestion.pipeline import run_sync
from ingestion.schemas import (
    ContentType,
    FetchResult,
    NormalizedItem,
    ScopeStats,
    SyncStats,
)

# Reuse the in-memory ports from tests/conftest.py.
sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    FakeAdapter,
    InMemoryContentSink,
    InMemorySyncLogger,
    InMemorySyncState,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _capture_structlog(monkeypatch: pytest.MonkeyPatch) -> structlog.testing.LogCapture:
    """Install a fresh :class:`LogCapture` for the test.

    Tests that call this MUST also call ``structlog.reset_defaults()`` in a
    ``finally`` block so the capture doesn't leak into the next test's
    structlog config.
    """
    cap = structlog.testing.LogCapture()
    structlog.reset_defaults()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "_telemetry",
        structlog.get_logger("test.pipeline.telemetry"),
    )
    return cap


def _events(cap: structlog.testing.LogCapture) -> list[dict[str, Any]]:
    return list(cap.entries)


def _names(cap: structlog.testing.LogCapture) -> list[str]:
    return [e.get("event", "") for e in _events(cap)]


def _make_item(external_id: str, *, platform: str = "test") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform=platform,
        content_type=ContentType.ANNOUNCEMENT,
        title="t",
        body="b",
        raw_payload={},
    )


# ---------------------------------------------------------------------------
# happy path: ordered events with sync_id binding
# ---------------------------------------------------------------------------


async def test_emits_five_events_in_order_on_happy_path(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sync.started`` → ``sync.scope_progress``* → ``sync.upserting`` →
    ``sync.upserted`` → ``sync.finished`` is the contract every consumer
    pivots on. Two scopes mean two ``sync.scope_progress`` events between
    started and upserting."""
    result = FetchResult(
        items=[_make_item("a"), _make_item("b")],
        cursor=None,
        stats=SyncStats(
            items_fetched=2,
            per_scope_stats={
                "messages": ScopeStats(items_fetched=1),
                "files": ScopeStats(items_fetched=1),
            },
        ),
    )
    cap = _capture_structlog(monkeypatch)
    try:
        _, sync_id = await run_sync(
            adapter=FakeAdapter(result),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="u@x",
        )
    finally:
        structlog.reset_defaults()

    names = _names(cap)
    # Slice the relevant per-sync names — other modules may add log lines.
    _SYNC_EVENTS = {
        "sync.started",
        "sync.scope_progress",
        "sync.upserting",
        "sync.upserted",
        "sync.finished",
    }
    relevant = [n for n in names if n in _SYNC_EVENTS]
    assert relevant[0] == "sync.started"
    # Two scope rows, in either order (dict iteration in CPython 3.12 is
    # insertion-ordered but we don't depend on that).
    assert sorted(relevant[1:3]) == ["sync.scope_progress", "sync.scope_progress"]
    assert relevant[3] == "sync.upserting"
    assert relevant[4] == "sync.upserted"
    assert relevant[5] == "sync.finished"

    # Every event carries the bound sync_id so drill-down works.
    for entry in _events(cap):
        if entry.get("event", "").startswith("sync."):
            assert entry.get("sync_id") == sync_id


async def test_finished_event_has_outcome_success_on_happy_path(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sync.finished`` discriminates success vs failure on ``outcome`` —
    pin the success label so a future refactor can't silently flip it."""
    fetch_result = FetchResult(items=[_make_item("a")], stats=SyncStats(items_fetched=1))
    cap = _capture_structlog(monkeypatch)
    try:
        await run_sync(
            adapter=FakeAdapter(fetch_result),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="u@x",
        )
    finally:
        structlog.reset_defaults()

    finished = [e for e in _events(cap) if e.get("event") == "sync.finished"]
    assert len(finished) == 1
    assert finished[0].get("outcome") == "success"
    assert "failure_reason" not in finished[0]
    assert finished[0].get("total_items_fetched") == 1
    assert finished[0].get("total_items_ingested") == 1


# ---------------------------------------------------------------------------
# empty fetch keeps the upserting/upserted pairing
# ---------------------------------------------------------------------------


async def test_empty_fetch_still_pairs_upserting_with_upserted(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch with zero items still emits ``sync.upserting`` + ``sync.upserted``
    so dashboards can pair the two events 1:1 without a special case for the
    empty branch. ``items_to_upsert`` is the discriminator that tells them
    apart."""
    result = FetchResult(items=[], stats=SyncStats(items_fetched=0))
    cap = _capture_structlog(monkeypatch)
    try:
        await run_sync(
            adapter=FakeAdapter(result),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="u@x",
        )
    finally:
        structlog.reset_defaults()

    names = _names(cap)
    assert "sync.upserting" in names
    assert "sync.upserted" in names
    upserting = [e for e in _events(cap) if e.get("event") == "sync.upserting"]
    assert len(upserting) == 1
    assert upserting[0].get("items_to_upsert") == 0


# ---------------------------------------------------------------------------
# failure paths: each has its own failure_reason discriminator
# ---------------------------------------------------------------------------


async def test_auth_failure_emits_finished_with_auth_reason(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 mid-sync ends with ``sync.finished`` outcome=failed and
    failure_reason starting with ``auth_``. The pipeline raises but
    must still emit the terminal event before it does."""

    class TokenExpiredAdapter:
        platform = "test"

        async def fetch(
            self,
            credentials: dict,  # noqa: ARG002
            cursor: dict | None = None,  # noqa: ARG002
            scope: object | None = None,  # noqa: ARG002
        ) -> FetchResult:
            raise TokenExpiredError("token expired")

    cap = _capture_structlog(monkeypatch)
    try:
        with pytest.raises(AuthenticationFailedError):
            await run_sync(
                adapter=TokenExpiredAdapter(),
                credentials={"token": "expired"},
                content_sink=content_sink,
                sync_state=sync_state,
                sync_logger=sync_logger,
                user_id="u@x",
            )
    finally:
        structlog.reset_defaults()

    finished = [e for e in _events(cap) if e.get("event") == "sync.finished"]
    assert len(finished) == 1
    assert finished[0].get("outcome") == "failed"
    # impl emits ``auth_401`` / ``auth_403`` — pin the prefix, not the suffix
    assert str(finished[0].get("failure_reason", "")).startswith("auth_")


async def test_ingestion_error_emits_finished_with_class_reason(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-auth :class:`IngestionError` (e.g. NormalizationError, a future
    typed subclass) gets the exception class name as ``failure_reason``."""

    class _NoiseError(IngestionError):
        pass

    class BoomAdapter:
        platform = "test"

        async def fetch(
            self,
            credentials: dict,  # noqa: ARG002
            cursor: dict | None = None,  # noqa: ARG002
            scope: object | None = None,  # noqa: ARG002
        ) -> FetchResult:
            raise _NoiseError("oops")

    cap = _capture_structlog(monkeypatch)
    try:
        stats, _sync_id = await run_sync(
            adapter=BoomAdapter(),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="u@x",
        )
    finally:
        structlog.reset_defaults()

    assert stats.errors == 1
    finished = [e for e in _events(cap) if e.get("event") == "sync.finished"]
    assert len(finished) == 1
    assert finished[0].get("outcome") == "failed"
    assert finished[0].get("failure_reason") == "_NoiseError"


async def test_unexpected_error_emits_finished_with_unexpected_reason(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanilla :class:`Exception` from inside the pipeline lands in the
    catch-all branch and gets the literal ``failure_reason="unexpected"``
    so operator dashboards can flag it as a bug, not a known-failure mode."""

    class CrashAdapter:
        platform = "test"

        async def fetch(
            self,
            credentials: dict,  # noqa: ARG002
            cursor: dict | None = None,  # noqa: ARG002
            scope: object | None = None,  # noqa: ARG002
        ) -> FetchResult:
            raise RuntimeError("boom")

    cap = _capture_structlog(monkeypatch)
    try:
        stats, _sync_id = await run_sync(
            adapter=CrashAdapter(),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="u@x",
        )
    finally:
        structlog.reset_defaults()

    assert stats.errors == 1
    finished = [e for e in _events(cap) if e.get("event") == "sync.finished"]
    assert len(finished) == 1
    assert finished[0].get("outcome") == "failed"
    assert finished[0].get("failure_reason") == "unexpected"


# ---------------------------------------------------------------------------
# bind_sync_context propagation: every event has sync_id
# ---------------------------------------------------------------------------


async def test_sync_id_bound_on_every_event_via_contextvar(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 13 relies on :func:`bind_sync_context` to auto-merge ``sync_id``
    onto every event inside ``run_sync`` — the explicit kwargs only carry
    fields that are NOT already in the contextvar set. Pin that no event
    accidentally drops the merger."""
    result = FetchResult(
        items=[_make_item("a")],
        stats=SyncStats(items_fetched=1, per_scope_stats={"messages": ScopeStats(items_fetched=1)}),
    )
    cap = _capture_structlog(monkeypatch)
    try:
        _, sync_id = await run_sync(
            adapter=FakeAdapter(result),
            credentials={"token": "t"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="alice@x",
        )
    finally:
        structlog.reset_defaults()

    sync_events = [
        e
        for e in _events(cap)
        if e.get("event")
        in {
            "sync.started",
            "sync.scope_progress",
            "sync.upserting",
            "sync.upserted",
            "sync.finished",
        }
    ]
    assert sync_events, "expected at least one sync.* event"
    for e in sync_events:
        # contextvar merger should have stamped the bind_sync_context fields
        # onto every record without the call site having to pass them.
        assert e.get("sync_id") == sync_id
        assert e.get("user_email") == "alice@x"
        assert e.get("platform") == "test"
