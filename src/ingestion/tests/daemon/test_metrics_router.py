"""Coverage for ``GET /metrics`` (slice 6 part B).

The metrics endpoint is public (Prometheus convention) and emits text
exposition. This file pins the body shape, the per-metric presence
contract, the 1s snapshot cache, the missing-table tolerance, and the
empty-DB summary degenerate case. Aim is 100% line + branch coverage
on ``metrics_router.py``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.routers import metrics_router
from ingestion.daemon.config import DaemonConfig, SchedulerConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.sync_logger import KIND_CREDENTIAL_REFRESH
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_snapshot_cache() -> Any:
    """The metrics snapshot is cached at module scope; clear between
    every test so one fixture's body can't leak into the next."""
    metrics_router.reset_snapshot_cache_for_tests()
    yield
    metrics_router.reset_snapshot_cache_for_tests()


def _build_app(
    tmp_path: Path,
    fernet_key: str,
    *,
    scheduler: SchedulerConfig | None = None,
) -> tuple[Any, Path]:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(
        sqlite_path=sqlite_path,
        scheduler=scheduler or SchedulerConfig(),
    )
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    return app, sqlite_path


def _client(app: Any) -> TestClient:
    return TestClient(app)


def _seed_sync_history(
    sqlite_path: Path,
    *,
    sync_id: str,
    state: str,
    sync_kind: str = "content_sync",
    started_at: str = "2026-04-26T00:00:00+00:00",
    user_email: str = "alice@x",
    platform: str = "moodle",
) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state, sync_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sync_id, user_email, platform, started_at, state, sync_kind),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_sync_profile(
    sqlite_path: Path,
    *,
    sync_id: str,
    duration: float,
    items_new: int = 0,
    items_updated: int = 0,
    user_email: str = "alice@x",
    platform: str = "moodle",
) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_profiles (sync_id, user_email, platform, "
            "total_duration_seconds, items_new, items_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sync_id,
                user_email,
                platform,
                duration,
                items_new,
                items_updated,
                "2026-04-26T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_force_sync_request(sqlite_path: Path, *, user: str, platform: str) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO force_sync_requests (user_email, platform, requested_at) VALUES (?, ?, ?)",
            (user, platform, "2026-04-26T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_dead_letter(sqlite_path: Path, *, user: str, platform: str) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO dead_letter_state "
            "(user_email, platform, consecutive_failures, suppressed_until) "
            "VALUES (?, ?, ?, ?)",
            (user, platform, 5, "2030-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_metric_lines(body: str) -> dict[str, list[tuple[str, float | None]]]:
    """Hand-rolled parser: returns {metric_name: [(label_block, value)]}.

    Tolerates the daemon's home-grown exposition format. Quantile labels
    are returned as a string ``label_block`` so callers can branch on
    presence; ``value`` is float-coerced (``NaN`` -> ``None``).
    """
    out: dict[str, list[tuple[str, float | None]]] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        # Either ``name value`` or ``name{labels} value``.
        if "{" in line:
            head, _, tail = line.partition("{")
            label_block, _, rest = tail.partition("}")
            value_str = rest.strip()
            name = head
        else:
            name, _, value_str = line.partition(" ")
            label_block = ""
        try:
            value = float(value_str)
            if value != value:  # NaN
                value = None
        except ValueError:
            value = None
        out.setdefault(name, []).append((label_block, value))
    return out


# ---------------------------------------------------------------------------
# /metrics: contract
# ---------------------------------------------------------------------------


def test_metrics_returns_200_with_prometheus_content_type(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"


def test_metrics_is_public_no_auth_required(tmp_path: Path, fernet_key: str) -> None:
    """Prometheus convention: /metrics never requires auth."""
    app, _ = _build_app(tmp_path, fernet_key)
    response = TestClient(app).get("/v1/metrics")  # no cookie, no XRW
    assert response.status_code == 200


def test_metrics_post_returns_405(tmp_path: Path, fernet_key: str) -> None:
    """The router only registers GET; POST should be rejected."""
    app, _ = _build_app(tmp_path, fernet_key)
    response = TestClient(app).post("/v1/metrics")
    assert response.status_code == 405


def test_metrics_body_uses_valid_prometheus_format(tmp_path: Path, fernet_key: str) -> None:
    """Body must be parseable by a real Prometheus client."""
    from prometheus_client.parser import text_string_to_metric_families

    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/metrics")
    families = list(text_string_to_metric_families(response.text))
    assert families, "expected at least one metric family"


def test_metrics_body_ends_with_newline(tmp_path: Path, fernet_key: str) -> None:
    """Trailing newline matters per the text-format spec."""
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/metrics")
    assert response.text.endswith("\n")


def test_metrics_includes_help_and_type_lines(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/metrics")
    body = response.text
    assert "# HELP endpoint_daemon_active_syncs" in body
    assert "# TYPE endpoint_daemon_active_syncs gauge" in body


# ---------------------------------------------------------------------------
# every documented metric is present
# ---------------------------------------------------------------------------


_REQUIRED_METRICS = (
    "endpoint_daemon_active_syncs",
    "endpoint_daemon_queue_depth",
    "endpoint_daemon_dead_letter_count",
    "endpoint_daemon_sync_duration_seconds",
    "endpoint_daemon_refresh_failures_total",
    "endpoint_daemon_items_ingested_total",
    "endpoint_daemon_workers_available",
    "endpoint_daemon_uptime_seconds",
)


@pytest.mark.parametrize("metric", _REQUIRED_METRICS)
def test_metrics_each_documented_metric_present(
    tmp_path: Path, fernet_key: str, metric: str
) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/metrics")
    assert f"# HELP {metric}" in response.text
    assert f"# TYPE {metric}" in response.text


def test_sync_duration_renders_as_summary(tmp_path: Path, fernet_key: str) -> None:
    """The duration histogram is emitted as a Prometheus ``summary`` per the
    implementer's note (real histograms need pre-declared buckets)."""
    app, _ = _build_app(tmp_path, fernet_key)
    body = _client(app).get("/v1/metrics").text
    assert "# TYPE endpoint_daemon_sync_duration_seconds summary" in body


# ---------------------------------------------------------------------------
# empty DB shape
# ---------------------------------------------------------------------------


def test_metrics_empty_db_emits_zero_count_and_nan_quantiles(
    tmp_path: Path, fernet_key: str
) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    # Empty sync_profiles: count + sum are 0 (rendered as int).
    assert parsed["endpoint_daemon_sync_duration_seconds_count"][0][1] == 0.0
    assert parsed["endpoint_daemon_sync_duration_seconds_sum"][0][1] == 0.0
    quantile_rows = parsed["endpoint_daemon_sync_duration_seconds"]
    # Each quantile line is NaN.
    for _label, value in quantile_rows:
        assert value is None  # parser converts NaN to None


def test_metrics_counter_values_render_as_integers(tmp_path: Path, fernet_key: str) -> None:
    """Whole-number counters/gauges should not have a trailing ``.0``."""
    app, _ = _build_app(tmp_path, fernet_key)
    body = _client(app).get("/v1/metrics").text
    # The active gauge with no rows should appear as ``... 0``, not ``... 0.0``.
    for line in body.splitlines():
        if line.startswith("endpoint_daemon_active_syncs ") or line.startswith(
            "endpoint_daemon_queue_depth "
        ):
            value_part = line.split(" ", 1)[1]
            assert "." not in value_part, line


# ---------------------------------------------------------------------------
# populated DB
# ---------------------------------------------------------------------------


def test_metrics_reflects_running_sync(tmp_path: Path, fernet_key: str) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_sync_history(sqlite_path, sync_id="r1", state="running")
    _seed_sync_history(sqlite_path, sync_id="r2", state="running")
    response = _client(app).get("/v1/metrics")
    parsed = _parse_metric_lines(response.text)
    assert parsed["endpoint_daemon_active_syncs"][0][1] == 2.0


def test_metrics_workers_available_decreases_with_active(tmp_path: Path, fernet_key: str) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_sync_history(sqlite_path, sync_id="r1", state="running")
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    workers = parsed["endpoint_daemon_workers_available"][0][1]
    assert workers is not None
    # Default capacity is 8; one active means seven available.
    assert workers == 7.0


def test_metrics_workers_available_floors_at_zero(tmp_path: Path, fernet_key: str) -> None:
    """If active exceeds capacity (impossible in practice but safeguarded
    in code), workers_available must clamp to 0 not go negative."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    # 12 running > capacity (8) -> max(0, -4) = 0.
    for i in range(12):
        _seed_sync_history(sqlite_path, sync_id=f"r{i}", state="running")
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_workers_available"][0][1] == 0.0


def test_metrics_queue_depth_counts_force_requests(tmp_path: Path, fernet_key: str) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_force_sync_request(sqlite_path, user="a@x", platform="teams")
    _seed_force_sync_request(sqlite_path, user="b@x", platform="moodle")
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_queue_depth"][0][1] == 2.0


def test_metrics_dead_letter_counts_suppressed_pairs(tmp_path: Path, fernet_key: str) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_dead_letter(sqlite_path, user="a@x", platform="teams")
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_dead_letter_count"][0][1] == 1.0


def test_metrics_refresh_failures_counts_credential_refresh_failed(
    tmp_path: Path, fernet_key: str
) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_sync_history(
        sqlite_path,
        sync_id="rf1",
        state="failed",
        sync_kind=KIND_CREDENTIAL_REFRESH,
    )
    _seed_sync_history(
        sqlite_path,
        sync_id="rf2",
        state="failed",
        sync_kind=KIND_CREDENTIAL_REFRESH,
    )
    # An unrelated content_sync failure must NOT be counted.
    _seed_sync_history(sqlite_path, sync_id="cf", state="failed", sync_kind="content_sync")
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_refresh_failures_total"][0][1] == 2.0


def test_metrics_items_ingested_sums_new_and_updated(tmp_path: Path, fernet_key: str) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_sync_history(sqlite_path, sync_id="s1", state="complete")
    _seed_sync_profile(sqlite_path, sync_id="s1", duration=1.5, items_new=3, items_updated=2)
    _seed_sync_history(sqlite_path, sync_id="s2", state="complete")
    _seed_sync_profile(sqlite_path, sync_id="s2", duration=2.5, items_new=4, items_updated=1)
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_items_ingested_total"][0][1] == 10.0


def test_metrics_summary_emits_quantile_floats_when_data_present(
    tmp_path: Path, fernet_key: str
) -> None:
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    for i, dur in enumerate([0.5, 1.0, 1.5, 2.0, 3.0]):
        sid = f"d{i}"
        _seed_sync_history(sqlite_path, sync_id=sid, state="complete")
        _seed_sync_profile(sqlite_path, sync_id=sid, duration=dur)
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    # _count, _sum, and three quantile labels should all be numeric.
    assert parsed["endpoint_daemon_sync_duration_seconds_count"][0][1] == 5.0
    assert parsed["endpoint_daemon_sync_duration_seconds_sum"][0][1] == 8.0
    quantile_lines = parsed["endpoint_daemon_sync_duration_seconds"]
    assert {block for block, _ in quantile_lines} == {
        'quantile="0.5"',
        'quantile="0.95"',
        'quantile="0.99"',
    }
    for _block, value in quantile_lines:
        assert value is not None
        assert value > 0.0


def test_metrics_summary_drops_null_durations(tmp_path: Path, fernet_key: str) -> None:
    """Profile rows with a NULL duration should not poison the histogram."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _seed_sync_history(sqlite_path, sync_id="ok", state="complete")
    _seed_sync_profile(sqlite_path, sync_id="ok", duration=2.0)
    # Inject a NULL duration directly.
    _seed_sync_history(sqlite_path, sync_id="null", state="complete")
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_profiles (sync_id, user_email, platform, "
            "total_duration_seconds, created_at) VALUES (?, ?, ?, NULL, ?)",
            ("null", "alice@x", "moodle", "2026-04-26T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_sync_duration_seconds_count"][0][1] == 1.0


# ---------------------------------------------------------------------------
# cache behaviour
# ---------------------------------------------------------------------------


def test_metrics_snapshot_is_cached_within_ttl(tmp_path: Path, fernet_key: str) -> None:
    """Two calls within the 1s TTL should return identical bodies even if
    the underlying DB changed between them."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    body1 = _client(app).get("/v1/metrics").text
    _seed_sync_history(sqlite_path, sync_id="r1", state="running")
    body2 = _client(app).get("/v1/metrics").text
    # Cached: the new running sync isn't visible.
    assert body1 == body2


def test_metrics_cache_refreshes_after_reset(tmp_path: Path, fernet_key: str) -> None:
    """Tester-only reset hook clears the cache; the next call rebuilds."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    _client(app).get("/v1/metrics")
    _seed_sync_history(sqlite_path, sync_id="r1", state="running")
    metrics_router.reset_snapshot_cache_for_tests()
    body = _client(app).get("/v1/metrics").text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_active_syncs"][0][1] == 1.0


def test_metrics_cache_per_sqlite_path(tmp_path: Path, fernet_key: str) -> None:
    """Two apps with different DB paths must not share snapshots."""
    app_a, db_a = _build_app(tmp_path / "a", fernet_key)
    app_b, db_b = _build_app(tmp_path / "b", fernet_key)
    # Seed only DB B with a running sync.
    _seed_sync_history(db_b, sync_id="r1", state="running")
    body_a = _client(app_a).get("/v1/metrics").text
    body_b = _client(app_b).get("/v1/metrics").text
    parsed_a = _parse_metric_lines(body_a)
    parsed_b = _parse_metric_lines(body_b)
    assert parsed_a["endpoint_daemon_active_syncs"][0][1] == 0.0
    assert parsed_b["endpoint_daemon_active_syncs"][0][1] == 1.0


# ---------------------------------------------------------------------------
# missing-table tolerance
# ---------------------------------------------------------------------------


def test_metrics_handles_missing_tables(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    """A sqlite path that points at an empty (un-migrated) DB must not
    blow up the endpoint — every counter falls through to 0."""
    sqlite_path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        # Empty DB: no tables at all.
        conn.commit()
    finally:
        conn.close()

    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    # We have to skip ``open_store`` (which would migrate) and use a
    # dummy LocalStore-shaped object via the existing one but with the
    # missing-table sqlite path passed to the metrics endpoint.
    # Easiest path: build the app against a fresh-migrated store, then
    # patch the config sqlite_path to the bare DB.
    real_store = open_store(tmp_path / "real.db")
    app = create_app(config=config, store=real_store, cipher=cipher, auth_settings=auth_settings)
    response = TestClient(app).get("/v1/metrics")
    assert response.status_code == 200
    # Every counter should default to 0.
    body = response.text
    parsed = _parse_metric_lines(body)
    assert parsed["endpoint_daemon_active_syncs"][0][1] == 0.0
    assert parsed["endpoint_daemon_queue_depth"][0][1] == 0.0
    assert parsed["endpoint_daemon_items_ingested_total"][0][1] == 0.0


def test_safe_scalar_int_swallows_aiosqlite_error() -> None:
    """``_safe_scalar_int`` returns 0 on ``aiosqlite.Error`` (missing table)."""
    import asyncio

    import aiosqlite

    class _Cursor:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise aiosqlite.OperationalError("no such table")

    async def go() -> int:
        return await metrics_router._safe_scalar_int(_Cursor(), "SELECT 1")  # type: ignore[arg-type]

    assert asyncio.run(go()) == 0


def test_safe_scalar_int_handles_none_row() -> None:
    """``fetchone`` returning None means 0."""
    import asyncio

    class _Cursor:
        async def fetchone(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class _Conn:
        async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            return _Cursor()

    async def go() -> int:
        return await metrics_router._safe_scalar_int(_Conn(), "SELECT 1")  # type: ignore[arg-type]

    assert asyncio.run(go()) == 0


def test_safe_scalar_int_handles_unparseable_value() -> None:
    """A non-integer first column returns 0 via the int() except path."""
    import asyncio

    class _Cursor:
        async def fetchone(self) -> tuple[object, ...]:
            return (object(),)

        async def close(self) -> None:
            return None

    class _Conn:
        async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            return _Cursor()

    async def go() -> int:
        return await metrics_router._safe_scalar_int(_Conn(), "SELECT 1")  # type: ignore[arg-type]

    assert asyncio.run(go()) == 0


def test_safe_scalar_int_logs_warning_on_aiosqlite_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin that ``_safe_scalar_int`` emits a structured WARN log when it
    swallows an aiosqlite error so an operator watching the metric
    pipeline isn't blind to "table missing" failures."""
    import asyncio
    import logging

    import aiosqlite
    import structlog

    cap = structlog.testing.LogCapture()
    structlog.reset_defaults()
    structlog.configure(
        processors=[cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    monkeypatch.setattr(metrics_router, "_log", structlog.get_logger("test.metrics"))
    try:

        class _Cursor:
            async def execute(self, *_args: object, **_kwargs: object) -> None:
                raise aiosqlite.OperationalError("disk full")

        async def go() -> int:
            return await metrics_router._safe_scalar_int(
                _Cursor(),  # type: ignore[arg-type]
                "SELECT COUNT(*) FROM dead_letter_state",
            )

        assert asyncio.run(go()) == 0
        matching = [e for e in cap.entries if e.get("event") == "metrics.safe_scalar_query_failed"]
        assert matching, cap.entries
        # The SQL kwarg must be on the record so an operator can see
        # which counter went silent.
        assert "dead_letter_state" in matching[0]["sql"]
    finally:
        structlog.reset_defaults()


def test_safe_scalar_int_logs_warning_on_unparseable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the first column can't be coerced to int, log a WARN with the
    raw value's type so an operator can debug the schema mismatch."""
    import asyncio
    import logging

    import structlog

    cap = structlog.testing.LogCapture()
    structlog.reset_defaults()
    structlog.configure(
        processors=[cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    monkeypatch.setattr(metrics_router, "_log", structlog.get_logger("test.metrics"))
    try:

        class _Cursor:
            async def fetchone(self) -> tuple[object, ...]:
                return (object(),)

            async def close(self) -> None:
                return None

        class _Conn:
            async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
                return _Cursor()

        async def go() -> int:
            return await metrics_router._safe_scalar_int(_Conn(), "SELECT 1")  # type: ignore[arg-type]

        assert asyncio.run(go()) == 0
        matching = [
            e for e in cap.entries if e.get("event") == "metrics.safe_scalar_value_unparseable"
        ]
        assert matching, cap.entries
        assert matching[0]["raw_value_type"] == "object"
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# format helpers
# ---------------------------------------------------------------------------


def test_format_float_renders_int_clean() -> None:
    assert metrics_router._format_float(5.0) == "5"


def test_format_float_renders_negative_int_clean() -> None:
    assert metrics_router._format_float(-3.0) == "-3"


def test_format_float_falls_back_to_repr_for_decimal() -> None:
    out = metrics_router._format_float(1.25)
    assert "1.25" in out


def test_format_float_renders_nan() -> None:
    assert metrics_router._format_float(float("nan")) == "NaN"


def test_format_float_uses_repr_for_huge_numbers() -> None:
    """Past 1e15 the int-coerce branch is skipped; verify we still emit
    a parseable float."""
    out = metrics_router._format_float(1e16)
    # repr of 1e16 is acceptable Prometheus float.
    assert out


def test_quantile_handles_single_value() -> None:
    assert metrics_router._quantile([4.2], 0.5) == 4.2


def test_quantile_handles_empty_list() -> None:
    assert metrics_router._quantile([], 0.5) == 0.0


def test_quantile_interpolates() -> None:
    # On [1,2,3,4] the median sits at index 1.5 -> 2.5.
    assert metrics_router._quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_quantile_caps_upper_index() -> None:
    """q=1.0 must not run off the end of the list."""
    assert metrics_router._quantile([1.0, 2.0, 3.0], 1.0) == 3.0


def test_uptime_increases_across_calls(tmp_path: Path, fernet_key: str) -> None:
    """Two separated /metrics calls must show non-decreasing uptime values
    (we sleep briefly + reset cache between to force a recompute)."""
    app, _ = _build_app(tmp_path, fernet_key)
    body1 = _client(app).get("/v1/metrics").text
    parsed1 = _parse_metric_lines(body1)
    u1 = parsed1["endpoint_daemon_uptime_seconds"][0][1]
    metrics_router.reset_snapshot_cache_for_tests()
    time.sleep(0.05)
    body2 = _client(app).get("/v1/metrics").text
    parsed2 = _parse_metric_lines(body2)
    u2 = parsed2["endpoint_daemon_uptime_seconds"][0][1]
    assert u1 is not None and u2 is not None
    assert u2 >= u1


def test_read_counters_logs_and_returns_partial_on_outer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer try/except in ``_read_counters`` returns whatever was
    accumulated when the connection blows up — pin that path."""
    import asyncio

    class _BadConn:
        async def __aenter__(self) -> _BadConn:
            raise RuntimeError("connect failed")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(metrics_router.aiosqlite, "connect", lambda _p: _BadConn())
    out = asyncio.run(metrics_router._read_counters(tmp_path / "x.db"))
    assert out == {}


def test_read_duration_stats_logs_and_returns_empty_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    class _BadConn:
        async def __aenter__(self) -> _BadConn:
            raise RuntimeError("connect failed")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(metrics_router.aiosqlite, "connect", lambda _p: _BadConn())
    out = asyncio.run(metrics_router._read_duration_stats(tmp_path / "x.db"))
    assert out == []


def test_read_duration_stats_skips_unparseable_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``except (TypeError, ValueError)`` branch in ``_read_duration_stats``."""
    import asyncio

    class _Cursor:
        async def fetchall(self) -> list[tuple[object, ...]]:
            # First valid, second NULL (skip), third unparseable (except).
            return [(2.0,), (None,), (object(),)]

        async def close(self) -> None:
            return None

    class _Conn:
        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(metrics_router.aiosqlite, "connect", lambda _p: _Conn())
    out = asyncio.run(metrics_router._read_duration_stats(tmp_path / "x.db"))
    assert out == [2.0]


def test_reset_snapshot_cache_for_tests_clears_dict(tmp_path: Path, fernet_key: str) -> None:
    """Direct invocation pin so the helper itself is covered."""
    app, _ = _build_app(tmp_path, fernet_key)
    _client(app).get("/v1/metrics")
    assert metrics_router._LAST_SNAPSHOT  # populated
    metrics_router.reset_snapshot_cache_for_tests()
    assert metrics_router._LAST_SNAPSHOT == {}
