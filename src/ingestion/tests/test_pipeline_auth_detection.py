"""Slice 8 — pipeline 401/403-mid-sync detection tests.

Covers ``ingestion.pipeline.run_sync``'s typed auth-failure path:

* Adapter raises :class:`TokenExpiredError` (401) → pipeline marks the
  credential expired, logs the failure, raises
  :class:`AuthenticationFailedError` with ``http_status=401``.
* Adapter raises :class:`InsufficientScopeError` (403) → same shape
  with ``http_status=403``.
* Adapter raises :class:`IngestionError` with another shape (5xx,
  rate-limit) → propagates as a normal failure (no auth marking).
* Adapter raises an unexpected ``Exception`` → caught, logged, returned
  with errors=1 (NOT raised as AuthenticationFailedError).
* ``token_store=None`` → pipeline still raises
  :class:`AuthenticationFailedError` but skips mark_expired.
* mark_expired idempotency: two concurrent 401 fetches both call
  mark_expired exactly once (atomic CAS in the store; pipeline does
  call mark_expired per-detection but the store de-dupes).
* ``AuthenticationFailedError.__cause__`` chain preserves the original
  IngestionError (used by structlog's exc_info).
* ``AuthenticationFailedError`` carries the platform name + user_email
  in its message for ops drill-downs.
* Translator routes the new exception class to 401 ``unauthorized``
  (slice 8 reviewer-fixer mapped it; the catchall used to swallow
  it as a 500 ``internal_error``).

Plus adjacent backfill on the pipeline's existing happy/error paths
that the coverage mapping wants pinned.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.services.exceptions import AuthenticationFailedError
from ingestion.exceptions import (
    IngestionError,
    InsufficientScopeError,
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)
from ingestion.pipeline import run_sync
from ingestion.schemas import (
    ContentType,
    FetchResult,
    NormalizedItem,
    SyncStats,
)

# Reuse fakes from conftest.py — same trick test_pipeline.py uses.
sys.path.insert(0, str(Path(__file__).parent))
from conftest import InMemoryContentSink, InMemorySyncLogger, InMemorySyncState  # noqa: E402


def _make_item(external_id: str = "x") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform="test",
        content_type=ContentType.ANNOUNCEMENT,
        title="Title",
        body="Body",
        raw_payload={},
    )


class _FakeTokenStore:
    """In-memory token store with mark_expired call tracking."""

    def __init__(self) -> None:
        self.creds: dict[tuple[str, str], dict[str, Any]] = {}
        self.expired: set[tuple[str, str]] = set()
        self.mark_expired_calls: list[tuple[str, str]] = []
        self._lock = asyncio.Lock()

    async def get_credentials(self, user_id: str, platform: str) -> dict[str, Any] | None:
        return self.creds.get((user_id, platform))

    async def update_credentials(
        self, user_id: str, platform: str, credentials: dict[str, Any]
    ) -> None:
        self.creds[(user_id, platform)] = credentials
        self.expired.discard((user_id, platform))

    async def mark_expired(self, user_id: str, platform: str) -> None:
        # Atomic CAS: only the false→true transition fires once. Mirrors
        # the production PostgresTokenStore behaviour (idempotent).
        async with self._lock:
            self.mark_expired_calls.append((user_id, platform))
            self.expired.add((user_id, platform))


class _FailingAdapter:
    """Adapter that raises ``exc`` from fetch."""

    platform = "test"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.fetch_calls: int = 0

    async def fetch(
        self,
        credentials: dict[str, Any],
        cursor: dict[str, Any] | None = None,
        scope: Any = None,
    ) -> FetchResult:
        del credentials, cursor, scope
        self.fetch_calls += 1
        raise self._exc

    async def test_connection(self, credentials: dict[str, Any]) -> bool:
        del credentials
        return True


# ---------------------------------------------------------------------------
# 401 → AuthenticationFailedError + mark_expired
# ---------------------------------------------------------------------------


async def test_401_raises_authentication_failed_and_marks_expired(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """TokenExpiredError → mark expired → raise typed exception."""
    adapter = _FailingAdapter(TokenExpiredError("token rotted"))
    store = _FakeTokenStore()

    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=adapter,  # type: ignore[arg-type]
            credentials={"token": "x"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="alice@x",
            token_store=store,  # type: ignore[arg-type]
        )

    err = ei.value
    assert err.http_status == 401
    assert err.platform == "test"
    assert err.user_email == "alice@x"
    assert "alice@x" in str(err)
    assert "test" in str(err)
    assert store.mark_expired_calls == [("alice@x", "test")]
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1
    assert "401" in failures[0]["error"]


# ---------------------------------------------------------------------------
# 403 → AuthenticationFailedError with status 403
# ---------------------------------------------------------------------------


async def test_403_raises_authentication_failed_with_status_403(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """InsufficientScopeError → mark expired → 403 status."""
    adapter = _FailingAdapter(InsufficientScopeError("messages"))
    store = _FakeTokenStore()

    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=adapter,  # type: ignore[arg-type]
            credentials={"token": "x"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="bob@x",
            token_store=store,  # type: ignore[arg-type]
        )

    assert ei.value.http_status == 403
    assert ei.value.platform == "test"
    assert ei.value.user_email == "bob@x"
    assert store.mark_expired_calls == [("bob@x", "test")]
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1
    assert "403" in failures[0]["error"]


# ---------------------------------------------------------------------------
# Cause chain preserved
# ---------------------------------------------------------------------------


async def test_authentication_failed_error_preserves_cause(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """AuthenticationFailedError.__cause__ is the original
    IngestionError so structlog's exc_info chains correctly."""
    inner = TokenExpiredError("graph token expired at 12:34")
    adapter = _FailingAdapter(inner)
    store = _FakeTokenStore()

    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=adapter,  # type: ignore[arg-type]
            credentials={"token": "x"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="alice@x",
            token_store=store,  # type: ignore[arg-type]
        )

    # __cause__ chain points at the original; cause field carries
    # truncated str(exc).
    assert ei.value.__cause__ is inner
    assert "graph token expired at 12:34" in ei.value.cause


async def test_authentication_failed_error_truncates_long_cause(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """The .cause field is sliced to 200 chars so structured logs
    don't blow up."""
    inner = TokenExpiredError("x" * 500)
    adapter = _FailingAdapter(inner)
    store = _FakeTokenStore()

    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=adapter,  # type: ignore[arg-type]
            credentials={"token": "x"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="alice@x",
            token_store=store,  # type: ignore[arg-type]
        )

    assert len(ei.value.cause) <= 200


# ---------------------------------------------------------------------------
# token_store=None — still raises but skips mark_expired
# ---------------------------------------------------------------------------


async def test_no_token_store_still_raises_typed_exception(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """When the caller didn't supply a token_store the pipeline still
    raises AuthenticationFailedError but skips mark_expired."""
    adapter = _FailingAdapter(TokenExpiredError("x"))

    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=adapter,  # type: ignore[arg-type]
            credentials={"token": "x"},
            content_sink=content_sink,
            sync_state=sync_state,
            sync_logger=sync_logger,
            user_id="alice@x",
            token_store=None,
        )

    assert ei.value.http_status == 401
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Other IngestionError subclasses propagate as failure (no auth marking)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        PlatformUnavailableError("test"),
        RateLimitError(retry_after=60.0),
        IngestionError("generic ingestion error"),
    ],
)
async def test_other_ingestion_errors_propagate_as_failure_not_auth(
    exc: IngestionError,
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """500-class / rate-limit / generic IngestionError must NOT be
    treated as an auth failure: pipeline returns SyncStats(errors=1)
    and never marks the credential expired."""
    adapter = _FailingAdapter(exc)
    store = _FakeTokenStore()

    stats, sync_id = await run_sync(
        adapter=adapter,  # type: ignore[arg-type]
        credentials={"token": "x"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="alice@x",
        token_store=store,  # type: ignore[arg-type]
    )

    assert stats.errors == 1
    assert sync_id
    # Crucially: NOT marked expired.
    assert store.mark_expired_calls == []
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Unexpected non-IngestionError exception → caught at outer except
# ---------------------------------------------------------------------------


async def test_unexpected_exception_caught_returns_error_stats(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """A bare RuntimeError out of the adapter is caught by the
    catch-all `except Exception` branch → SyncStats(errors=1), NOT
    re-raised as AuthenticationFailedError."""
    adapter = _FailingAdapter(RuntimeError("internal explosion"))
    store = _FakeTokenStore()

    stats, _sync_id = await run_sync(
        adapter=adapter,  # type: ignore[arg-type]
        credentials={"token": "x"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="alice@x",
        token_store=store,  # type: ignore[arg-type]
    )

    assert stats.errors == 1
    assert store.mark_expired_calls == []
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1
    assert "Unexpected" in failures[0]["error"]


# ---------------------------------------------------------------------------
# Concurrent 401-mid-sync calls — mark_expired idempotency
# ---------------------------------------------------------------------------


async def test_concurrent_401_calls_mark_expired_idempotent(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
) -> None:
    """Two parallel fetches that both 401 — the token store's atomic
    CAS guarantees the credential ends up expired once. The pipeline
    calls mark_expired per-detection (twice here), the store de-dupes."""

    async def _do_one(adapter: _FailingAdapter, store: _FakeTokenStore) -> bool:
        try:
            await run_sync(
                adapter=adapter,  # type: ignore[arg-type]
                credentials={"token": "x"},
                content_sink=content_sink,
                sync_state=sync_state,
                sync_logger=InMemorySyncLogger(),
                user_id="alice@x",
                token_store=store,  # type: ignore[arg-type]
            )
            return False
        except AuthenticationFailedError:
            return True

    store = _FakeTokenStore()
    a = _FailingAdapter(TokenExpiredError("a"))
    b = _FailingAdapter(TokenExpiredError("b"))
    raised_a, raised_b = await asyncio.gather(_do_one(a, store), _do_one(b, store))

    assert raised_a and raised_b
    # End state: credential is in the expired set exactly once (CAS de-dupes).
    assert ("alice@x", "test") in store.expired
    # Both pipeline runs called mark_expired (the store is the dedupe
    # boundary, not the pipeline).
    assert len(store.mark_expired_calls) == 2


# ---------------------------------------------------------------------------
# Adjacent backfill — empty FetchResult, normalizer exception, sink failure
# ---------------------------------------------------------------------------


async def test_empty_fetch_result_completes_with_zero_stats(
    content_sink: InMemoryContentSink,
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """An empty FetchResult lands as a successful sync with zero items."""

    class _EmptyAdapter:
        platform = "test"

        async def fetch(self, *_args: Any, **_kwargs: Any) -> FetchResult:
            return FetchResult(items=[], stats=SyncStats())

        async def test_connection(self, _credentials: Any) -> bool:
            return True

    stats, sync_id = await run_sync(
        adapter=_EmptyAdapter(),  # type: ignore[arg-type]
        credentials={"token": "x"},
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="alice@x",
    )

    assert stats.items_fetched == 0
    assert stats.items_new == 0
    assert stats.errors == 0
    assert sync_id
    completes = [e for e in sync_logger.events if e["type"] == "complete"]
    assert len(completes) == 1


async def test_content_sink_exception_caught_at_outer_except(
    sync_state: InMemorySyncState,
    sync_logger: InMemorySyncLogger,
) -> None:
    """A ContentSink whose upsert_batch blows up surfaces as
    SyncStats(errors=1) — the catch-all handles it."""

    class _FailingSink:
        async def exists(self, platform: str, external_id: str) -> bool:
            del platform, external_id
            return False

        async def exists_batch(self, platform: str, external_ids: list[str]) -> set[str]:
            del platform, external_ids
            return set()

        async def upsert(self, item: NormalizedItem) -> str:
            del item
            return "x"

        async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
            del items
            raise RuntimeError("disk full")

    class _OneItemAdapter:
        platform = "test"

        async def fetch(self, *_args: Any, **_kwargs: Any) -> FetchResult:
            return FetchResult(items=[_make_item("x")], stats=SyncStats(items_fetched=1))

        async def test_connection(self, _credentials: Any) -> bool:
            return True

    stats, _ = await run_sync(
        adapter=_OneItemAdapter(),  # type: ignore[arg-type]
        credentials={"token": "x"},
        content_sink=_FailingSink(),  # type: ignore[arg-type]
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id="alice@x",
    )

    assert stats.errors == 1
    failures = [e for e in sync_logger.events if e["type"] == "failure"]
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Translator mapping — verify current behaviour for AuthenticationFailedError
# ---------------------------------------------------------------------------


def test_translator_routes_authentication_failed_to_401_unauthorized() -> None:
    """Slice 8 reviewer-fixer added a branch in the translator so
    ``AuthenticationFailedError`` maps to 401 ``unauthorized`` rather
    than the catchall 500 ``internal_error``. The dashboard's auth-
    aware fetch wrapper can branch on the status code without parsing
    message text.

    The 401 mapping is intentionally distinct from the bulk-refresh
    surface (which uses ``TransientFailureError`` -> 502 for non-auth
    refresh failures): 401 is the semantically-correct status for an
    authentication-failed response, mirroring the underlying HTTP
    cause from the platform."""
    from fastapi import HTTPException
    from ingestion.daemon.api.exception_translator import service_error_to_http

    err = AuthenticationFailedError(
        platform="teams",
        user_email="alice@x",
        http_status=401,
        cause="token expired",
    )
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 401
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "unauthorized"
    # The body carries the typed message so an operator pivots from
    # the response without parsing the structured log.
    assert "alice@x" in exc.detail["message"]
    assert "teams" in exc.detail["message"]


def test_authentication_failed_error_chains_to_service_error() -> None:
    """Sanity: the new exception is a ServiceError so ``except
    ServiceError`` catches it in routers."""
    from ingestion.daemon.services.exceptions import ServiceError

    err = AuthenticationFailedError(
        platform="teams",
        user_email="alice@x",
        http_status=401,
        cause="boom",
    )
    assert isinstance(err, ServiceError)
    assert isinstance(err, Exception)


def test_authentication_failed_error_message_contains_drilldown_fields() -> None:
    """The string repr embeds platform + email + status so an operator
    pivots from a log line without parsing the structured fields."""
    err = AuthenticationFailedError(
        platform="moodle",
        user_email="alice@x",
        http_status=403,
        cause="scope missing",
    )
    msg = str(err)
    assert "moodle" in msg
    assert "alice@x" in msg
    assert "403" in msg
    assert "scope missing" in msg


def test_authentication_failed_error_keyword_only_constructor() -> None:
    """The constructor is kw-only — positional construction must fail
    so the call site signature stays self-documenting."""
    with pytest.raises(TypeError):
        # type: ignore[call-arg]
        AuthenticationFailedError("teams", "alice@x", 401, "boom")  # type: ignore[arg-type,misc]
