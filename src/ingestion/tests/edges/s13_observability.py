"""Edge + production tests for slice 13 (observability + alert notifier).

Pinned scenarios across the per-sync telemetry, the
:class:`WebhookAlertNotifier`, the SQLite backup loop, and the optional
OpenTelemetry tracing shim.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended) or ``[TEST-COVERAGE]``
(the existing implementation is fine; this just locks the behaviour).
Slow tests (>500ms) carry ``@pytest.mark.slow``.

Edge brainstorm additions (>= 8):

* Stale-notification race: a firing resolved between engine record + first
  POST attempt is skipped via ``_is_resolved`` ahead of the request
  [TEST-COVERAGE]
* Webhook auth header / bearer token NEVER appears in any log record on
  failure paths (defence-in-depth) [TEST-COVERAGE]
* Backup file written into a shared volume during cp — the sqlite
  ``backup`` C-level API yields BETWEEN page batches so a long write on
  the source DB isn't blocked, but the snapshot is still consistent
  [TEST-COVERAGE]
* OTel tracer resolution is lazy + cached — first ``traced_span``
  triggers the import, subsequent calls reuse the singleton
  [TEST-COVERAGE]
* Per-sync telemetry: ``sync_id`` ContextVar must NOT leak across
  ``asyncio.create_task`` (each task runs with the parent's contextvars
  but unbinding inside one task should not affect the other). Pin the
  isolation contract [TEST-COVERAGE]
* Backup loop with ``interval_seconds=0`` → effectively disabled,
  short-circuits with the ``backup_loop.disabled`` log line so an
  operator who set 0 to "turn off" gets the expected behaviour
  [TEST-COVERAGE]
* Webhook constructor: an URL with ``user:pass@`` userinfo embedded
  doesn't leak the credentials via the host log on failure
  [TEST-COVERAGE]
* Default-rule seed under aiosqlite contention: two concurrent boots
  rely on the UNIQUE index to prevent doubles — pin that the impl
  catches the IntegrityError for the racing call [TEST-COVERAGE]
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest
import structlog
from ingestion.daemon.alerts.notifiers.webhook import WebhookAlertNotifier, _safe_host
from ingestion.daemon.alerts.rules import AlertRule, Comparator
from ingestion.daemon.alerts.seed import seed_default_rules
from ingestion.daemon.logging import bind_sync_context
from ingestion.daemon.observability.tracing import (
    reset_tracer_cache_for_tests,
    traced_span,
)
from ingestion.daemon.store.backup import (
    _BACKUP_FILENAME_PREFIX,
    _run_one_backup_pass,
    run_backup_loop,
)
from ingestion.daemon.store.local import apply_migrations

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """A real (non-empty) sqlite database with the daemon's schema."""
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def fast_backoff(request: pytest.FixtureRequest) -> None:
    """Patch the captured ``sleep`` default of ``with_backoff`` so retries
    don't actually wait. Same trick as in ``test_webhook_notifier.py``;
    duplicated here so the edge file is self-contained."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    from ingestion.daemon.reliability import backoff as _b

    kwdefaults = _b.with_backoff.__kwdefaults__
    assert kwdefaults is not None
    original = kwdefaults.get("sleep")
    kwdefaults["sleep"] = _no_sleep

    def _restore() -> None:
        if original is not None:
            kwdefaults["sleep"] = original

    request.addfinalizer(_restore)


@pytest.fixture(autouse=True)
def _reset_tracer_cache() -> None:
    """Each test starts with a fresh tracer cache; the shim caches the
    looked-up tracer at module level so leaking would break later tests."""
    reset_tracer_cache_for_tests()
    yield
    reset_tracer_cache_for_tests()


# ---------------------------------------------------------------------------
# stub HTTP client (mirrors the one in test_webhook_notifier)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return {}


class _StubState:
    def __init__(self, behaviour: list[Any]) -> None:
        self.behaviour: list[Any] = list(behaviour)
        self.calls: list[dict[str, Any]] = []


class _StubClient:
    def __init__(self, *, timeout: float, state: _StubState) -> None:
        self.timeout = timeout
        self._state = state

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, json: Any, headers: dict[str, str]) -> _StubResponse:
        self._state.calls.append({"url": url, "json": json, "headers": dict(headers)})
        nxt = self._state.behaviour.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _stub_factory(behaviour: list[Any]) -> tuple[Any, _StubState]:
    state = _StubState(behaviour)

    def _build(*, timeout: float) -> _StubClient:
        return _StubClient(timeout=timeout, state=state)

    return _build, state


def _make_rule(*, name: str = "r", platform: str | None = None) -> AlertRule:
    return AlertRule(
        id=1,
        name=name,
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0.0,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=platform,
    )


def _capture(level: int = logging.INFO) -> structlog.testing.LogCapture:
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    return cap


# ---------------------------------------------------------------------------
# Edge 1: stale-notification race
# ---------------------------------------------------------------------------


async def test_stale_notification_race_skips_post(sqlite_db: Path, fast_backoff: None) -> None:
    """[TEST-COVERAGE] Engine records the firing, then resolves it (the
    breach went away) before the notifier dispatches. ``_is_resolved``
    sees state='resolved' on the next retry and the POST is skipped.

    Pin: a stub factory that would deliver a 200 records ZERO calls.
    """
    _ = fast_backoff
    fired_at = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    # Seed a resolved firing for rule_id=999 (no rule row needed for the
    # impl's _is_resolved query — it just reads alert_firings).
    conn = sqlite3.connect(sqlite_db)
    try:
        # Need a rule row for the FK. Insert one.
        conn.execute(
            """
            INSERT INTO alert_rules (
                name, enabled, metric, comparator, threshold,
                window_seconds, cooldown_seconds, user_email, platform,
                created_at, updated_at
            )
            VALUES ('stale', 1, 'failure_count', '>', 0.0, NULL, 0, NULL, NULL, ?, ?)
            """,
            (fired_at.isoformat(), fired_at.isoformat()),
        )
        rule_id = conn.execute("SELECT id FROM alert_rules WHERE name = 'stale'").fetchone()[0]
        conn.execute(
            """
            INSERT INTO alert_firings (
                rule_id, fired_at, metric_value, user_email, platform, state,
                resolved_at, trigger_context_json, cooldown_bucket
            )
            VALUES (?, ?, 1.0, NULL, NULL, 'resolved', ?, NULL, NULL)
            """,
            (rule_id, fired_at.isoformat(), fired_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    rule = AlertRule(
        id=rule_id,
        name="stale",
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0.0,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=None,
    )
    factory, state = _stub_factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        db_path=sqlite_db,
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    # No POST attempted — the firing was resolved before the dispatch.
    assert state.calls == []


# ---------------------------------------------------------------------------
# Edge 2: bearer token NEVER appears in any log line on failure
# ---------------------------------------------------------------------------


async def test_bearer_token_never_in_log_on_3xx_4xx_5xx(
    fast_backoff: None,
) -> None:
    """[TEST-COVERAGE] Every failure path (4xx rejection, 5xx final
    failure, network error final failure) MUST NOT echo the configured
    Bearer token. Defence-in-depth — a token leaking via logs is a
    privesc surface."""
    _ = fast_backoff
    secret = "tokn-DO-NOT-LEAK"
    fired_at = datetime(2026, 4, 26, tzinfo=UTC)
    rule = _make_rule()

    # 4xx path
    factory, _ = _stub_factory([_StubResponse(404)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook", token=secret, client_factory=factory
    )
    cap = _capture()
    try:
        await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    for entry in cap.entries:
        for value in entry.values():
            if isinstance(value, str):
                assert secret not in value

    # 5xx path
    factory2, _ = _stub_factory([_StubResponse(503)] * 3)
    notifier2 = WebhookAlertNotifier(
        url="https://example.com/hook", token=secret, client_factory=factory2
    )
    cap2 = _capture()
    try:
        await notifier2.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    for entry in cap2.entries:
        for value in entry.values():
            if isinstance(value, str):
                assert secret not in value

    # Network error path
    factory3, _ = _stub_factory([httpx.ConnectError("e")] * 3)
    notifier3 = WebhookAlertNotifier(
        url="https://example.com/hook", token=secret, client_factory=factory3
    )
    cap3 = _capture()
    try:
        await notifier3.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    for entry in cap3.entries:
        for value in entry.values():
            if isinstance(value, str):
                assert secret not in value


def test_userinfo_in_url_does_not_leak_via_safe_host() -> None:
    """[TEST-COVERAGE] An operator who embedded credentials in the
    URL (``https://user:secret@host``) MUST not see the secret in the
    host log. ``_safe_host`` strips userinfo unconditionally."""
    out = _safe_host("https://user:my-secret@example.com/path?token=abc")
    assert "my-secret" not in out
    assert out == "example.com"


# ---------------------------------------------------------------------------
# Edge 3: write burst during backup -> consistent snapshot
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_backup_under_concurrent_writes_is_consistent(
    populated_db: Path, backup_dir: Path
) -> None:
    """[TEST-COVERAGE] Online-safe SQLite backup: a write burst that
    runs WHILE the backup pass is in flight produces a snapshot that
    passes ``PRAGMA integrity_check``. Without this property the
    backup is useless — the operator can't recover from it."""
    backup_dir.mkdir()

    async def _writer() -> None:
        async with aiosqlite.connect(populated_db) as conn:
            # The schema has audit_log etc — write into one of those
            # tables so we exercise a real shape.
            for i in range(50):
                await conn.execute(
                    """
                    INSERT INTO audit_log (timestamp, actor, action, target)
                    VALUES (?, ?, ?, ?)
                    """,
                    (datetime.now(tz=UTC).isoformat(), "actor", "act", f"t{i}"),
                )
                await asyncio.sleep(0)
            await conn.commit()

    await asyncio.gather(
        _writer(),
        _run_one_backup_pass(
            sqlite_path=populated_db,
            backup_dir=backup_dir,
            retention_count=24,
            event_sink=None,
        ),
    )
    files = [p for p in backup_dir.iterdir() if p.name.startswith(_BACKUP_FILENAME_PREFIX)]
    assert len(files) == 1
    conn = sqlite3.connect(files[0])
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    assert result == ("ok",)


# ---------------------------------------------------------------------------
# Edge 4: OTel tracer resolution is cached (lazy)
# ---------------------------------------------------------------------------


def test_traced_span_resolves_tracer_lazily_then_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] The tracer lookup runs on the first
    ``traced_span`` call, NOT at import time. After the first call, it's
    cached so a hot loop of ``traced_span`` calls doesn't re-import the
    SDK on every iteration."""

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[str] = []

        def start_as_current_span(self, name: str) -> Any:
            self.spans.append(name)

            class _Cm:
                def __enter__(self) -> object:
                    return None

                def __exit__(self, *_e: object) -> None:
                    return None

            return _Cm()

    tracer = _Tracer()
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")
    counter = {"n": 0}

    def _get_tracer(_name: str) -> _Tracer:
        counter["n"] += 1
        return tracer

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    # Three consecutive traced_span calls — get_tracer should run once.
    for name in ("a", "b", "c"):
        with traced_span(name):
            pass
    assert counter["n"] == 1
    assert tracer.spans == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Edge 5: bind_sync_context isolation across asyncio.create_task
# ---------------------------------------------------------------------------


async def test_bind_sync_context_does_not_leak_across_tasks() -> None:
    """[TEST-COVERAGE] Two sibling tasks each set their own sync_id
    via ``bind_sync_context``. Logs emitted from task A must carry
    A's sync_id, not B's, and vice versa — even though both run on
    the same event loop. ``contextvars`` is supposed to give us this
    isolation by default, but pin it explicitly so a future refactor
    that switches to a global doesn't silently break correlation."""
    cap = _capture()
    log = structlog.get_logger("test_isolation")

    async def _emit_with_id(sync_id: str) -> None:
        with bind_sync_context(sync_id=sync_id, user_email=f"{sync_id}@x"):
            # Yield so both tasks interleave.
            await asyncio.sleep(0)
            log.info("event", marker=sync_id)
            await asyncio.sleep(0)

    try:
        await asyncio.gather(
            _emit_with_id("A"),
            _emit_with_id("B"),
        )
    finally:
        structlog.reset_defaults()

    a_events = [e for e in cap.entries if e.get("marker") == "A"]
    b_events = [e for e in cap.entries if e.get("marker") == "B"]
    assert len(a_events) == 1
    assert len(b_events) == 1
    # Each event carries its own sync_id; cross-contamination would
    # show A's logs with sync_id=B or vice versa.
    assert a_events[0].get("sync_id") == "A"
    assert b_events[0].get("sync_id") == "B"
    assert a_events[0].get("user_email") == "A@x"
    assert b_events[0].get("user_email") == "B@x"


# ---------------------------------------------------------------------------
# Edge 6: backup_loop interval_seconds=0 -> disabled
# ---------------------------------------------------------------------------


async def test_backup_loop_interval_zero_short_circuits(
    populated_db: Path, backup_dir: Path
) -> None:
    """[TEST-COVERAGE] ``interval_seconds=0`` is the operator opt-out.
    The loop logs ``backup_loop.disabled`` and returns immediately —
    no directory created, no files written, even after the shutdown
    event is set."""
    cap = _capture()
    try:
        shutdown = asyncio.Event()
        await run_backup_loop(
            shutdown,
            sqlite_path=populated_db,
            backup_dir=backup_dir,
            interval_seconds=0,
        )
    finally:
        structlog.reset_defaults()
    events = [e.get("event") for e in cap.entries]
    assert "backup_loop.disabled" in events
    assert not backup_dir.exists()


async def test_backup_loop_negative_interval_also_short_circuits(
    populated_db: Path, backup_dir: Path
) -> None:
    """[TEST-COVERAGE] A negative interval (operator typo) hits the
    same disabled branch — the impl's check is ``<= 0``."""
    cap = _capture()
    try:
        shutdown = asyncio.Event()
        await run_backup_loop(
            shutdown,
            sqlite_path=populated_db,
            backup_dir=backup_dir,
            interval_seconds=-3600,
        )
    finally:
        structlog.reset_defaults()
    events = [e.get("event") for e in cap.entries]
    assert "backup_loop.disabled" in events


# ---------------------------------------------------------------------------
# Edge 7: concurrent seed under race lands exactly 3 rules
# ---------------------------------------------------------------------------


async def test_three_concurrent_seed_callers_land_three_rules(
    sqlite_db: Path,
) -> None:
    """[TEST-COVERAGE] Three daemons against the same DB race to seed.
    The UNIQUE index on ``alert_rules.name`` rejects the duplicate
    inserts; the impl catches IntegrityError and returns. End state:
    exactly 3 rules, never 9.

    This is the more aggressive cousin of the 2-way race in
    ``test_seed.py``; here we add a third concurrent caller to make
    sure the suppression handles N>2."""
    results = await asyncio.gather(
        seed_default_rules(sqlite_db),
        seed_default_rules(sqlite_db),
        seed_default_rules(sqlite_db),
    )
    combined = sorted(set(results[0]) | set(results[1]) | set(results[2]))
    assert combined == sorted(
        ["dead_letter_burst", "sync_duration_p95_high", "credential_refresh_storm"]
    )
    conn = sqlite3.connect(sqlite_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]
    finally:
        conn.close()
    assert count == 3


# ---------------------------------------------------------------------------
# Edge 8: traced_span body exception propagates while span still closes
# ---------------------------------------------------------------------------


def test_traced_span_with_otel_propagates_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] An exception raised inside the ``traced_span``
    body propagates to the caller — the shim doesn't swallow it. The
    span context manager still exits cleanly (no leaked spans)."""

    closed: list[bool] = []

    class _Span:
        def set_attribute(self, _k: str, _v: Any) -> None:
            return None

    class _Ctx:
        def __enter__(self) -> _Span:
            return _Span()

        def __exit__(self, *_exc: object) -> None:
            closed.append(True)
            return None

    class _Tracer:
        def start_as_current_span(self, _name: str) -> _Ctx:
            return _Ctx()

    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")

    def _get_tracer(_n: str) -> _Tracer:
        return _Tracer()

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    with pytest.raises(RuntimeError, match="boom"), traced_span("foo"):
        raise RuntimeError("boom")
    assert closed == [True]
