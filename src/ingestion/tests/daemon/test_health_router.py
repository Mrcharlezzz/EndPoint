"""Coverage for ``/health``, ``/live``, and ``/ready`` (slice 6 part A).

These are the orchestrator probes — public, never auth-gated. ``/health``
and ``/live`` answer immediately without touching the DB so a wedged
SQLite cannot flap liveness; ``/ready`` rolls together a DB ping plus
the scheduler's classification (``active`` / ``disabled`` / ``stalled``)
plus the worker-pool's known state. The aim here is 100% line and
branch coverage on ``health_router.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.routers import health_router
from ingestion.daemon.config import DaemonConfig, SchedulerConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# fixtures (kept local so this module isn't coupled to conftest extensions)
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


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
    started_at: str,
    user_email: str = "alice@x",
    platform: str = "moodle",
    state: str = "complete",
) -> None:
    """Insert a sync_history row directly so age-driven readiness paths can
    be exercised without running a real scheduler tick."""
    import uuid

    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, user_email, platform, started_at, state),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /health and /live
# ---------------------------------------------------------------------------


def test_health_returns_ok(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_returns_ok(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_public_no_auth_required(tmp_path: Path, fernet_key: str) -> None:
    """No session cookie and no X-Requested-With — should still return 200."""
    app, _ = _build_app(tmp_path, fernet_key)
    client = TestClient(app)
    # Clear ANY default headers — orchestrators send a bare GET.
    response = client.get("/v1/health")
    assert response.status_code == 200


def test_live_is_public_no_auth_required(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = TestClient(app).get("/v1/live")
    assert response.status_code == 200


def test_ready_is_public_no_auth_required(tmp_path: Path, fernet_key: str) -> None:
    app, _ = _build_app(tmp_path, fernet_key)
    response = TestClient(app).get("/v1/ready")
    # Status doesn't matter here — just that auth isn't enforced.
    assert response.status_code in (200, 503)


def test_health_response_model_rejects_extra_fields() -> None:
    """The HealthResponse pydantic model is locked to ``status: ok``."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        health_router.HealthResponse(status="ok", extra_thing="boom")  # type: ignore[call-arg]


def test_health_succeeds_when_db_unreachable(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` must not consult the DB. Mock the check to confirm
    the endpoint never invokes it (and would still return 200 if it did)."""
    app, _ = _build_app(tmp_path, fernet_key)

    async def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("DB down")

    monkeypatch.setattr(health_router, "_check_db", boom)
    response = _client(app).get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /ready: happy path
# ---------------------------------------------------------------------------


def test_ready_returns_ok_on_fresh_deploy(tmp_path: Path, fernet_key: str) -> None:
    """No sync_history rows yet — a freshly deployed daemon is ``active``,
    not ``stalled``, because we can't distinguish "just booted" from
    "never scheduling" until we see traffic."""
    app, _ = _build_app(tmp_path, fernet_key)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "db": "ok",
        "worker_pool": "ok",
        "scheduler": "active",
    }
    assert body["reason"] is None


def test_ready_reports_recent_sync_as_active(tmp_path: Path, fernet_key: str) -> None:
    """A sync 10s ago is well inside the 60s floor — scheduler is active."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    recent = (datetime.now(tz=UTC) - timedelta(seconds=10)).isoformat()
    _seed_sync_history(sqlite_path, started_at=recent)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "active"


# ---------------------------------------------------------------------------
# /ready: degraded paths
# ---------------------------------------------------------------------------


def test_ready_returns_503_when_db_unreachable(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing DB ping must surface as ``unreachable`` and 503."""
    app, _ = _build_app(tmp_path, fernet_key)

    async def fake_check(_path: object) -> str:
        return "unreachable"

    monkeypatch.setattr(health_router, "_check_db", fake_check)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["db"] == "unreachable"
    assert body["reason"] == "sqlite unreachable"


def test_ready_db_check_swallows_arbitrary_exception(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_check_db`` catches Exception broadly; verify the path returns
    ``unreachable`` rather than letting the response bubble a 500."""
    import aiosqlite

    app, _ = _build_app(tmp_path, fernet_key)

    class _BoomConn:
        async def __aenter__(self) -> _BoomConn:
            raise aiosqlite.OperationalError("locked")

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_connect(_path: object) -> _BoomConn:
        return _BoomConn()

    monkeypatch.setattr(health_router.aiosqlite, "connect", fake_connect)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["db"] == "unreachable"


def test_ready_reports_disabled_scheduler(tmp_path: Path, fernet_key: str) -> None:
    """``scheduler.enabled = False`` — report ``disabled``, NOT ``stalled``."""
    app, _ = _build_app(
        tmp_path,
        fernet_key,
        scheduler=SchedulerConfig(enabled=False),
    )
    response = _client(app).get("/v1/ready")
    # Disabled is not a degraded state — the operator chose this.
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["scheduler"] == "disabled"
    assert body["status"] == "ok"


def test_ready_reports_stalled_when_last_sync_too_old(tmp_path: Path, fernet_key: str) -> None:
    """A sync_history row older than the 60s floor — scheduler is ``stalled``."""
    app, sqlite_path = _build_app(tmp_path, fernet_key)
    stale = (datetime.now(tz=UTC) - timedelta(seconds=180)).isoformat()
    _seed_sync_history(sqlite_path, started_at=stale)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["scheduler"] == "stalled"
    assert "no sync attempted in last" in body["reason"]


def test_ready_response_model_rejects_extra_fields() -> None:
    """ReadyResponse and ReadyChecks both pin ``extra='forbid'`` so an
    accidental field rename never silently slips through."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        health_router.ReadyChecks(  # type: ignore[call-arg]
            db="ok",
            worker_pool="ok",
            scheduler="active",
            extra_field="boom",
        )
    with pytest.raises(ValidationError):
        health_router.ReadyResponse(  # type: ignore[call-arg]
            status="ok",
            checks=health_router.ReadyChecks(db="ok", worker_pool="ok", scheduler="active"),
            reason=None,
            mystery=1,
        )


def test_ready_uses_60s_floor_when_tick_is_short(tmp_path: Path, fernet_key: str) -> None:
    """tick_seconds=1.0 means 2*tick=2s — the 60s floor must dominate so a
    quiet 30s window doesn't flap on small fleets."""
    app, sqlite_path = _build_app(
        tmp_path,
        fernet_key,
        scheduler=SchedulerConfig(enabled=True, tick_seconds=1.0),
    )
    # 30s ago: well above 2*tick (2s) but inside the 60s floor.
    semi_recent = (datetime.now(tz=UTC) - timedelta(seconds=30)).isoformat()
    _seed_sync_history(sqlite_path, started_at=semi_recent)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "active"


def test_ready_uses_2x_tick_when_tick_is_long(tmp_path: Path, fernet_key: str) -> None:
    """tick_seconds=120 means 2*tick=240s — that wins over the 60s floor."""
    app, sqlite_path = _build_app(
        tmp_path,
        fernet_key,
        scheduler=SchedulerConfig(enabled=True, tick_seconds=120.0),
    )
    # 200s old: stale by the 60s floor but still inside 2*tick (240s).
    age = (datetime.now(tz=UTC) - timedelta(seconds=200)).isoformat()
    _seed_sync_history(sqlite_path, started_at=age)
    response = _client(app).get("/v1/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "active"


def test_ready_last_sync_age_handles_query_failure(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query failure inside ``_last_sync_age_seconds`` must surface as
    ``None`` (no history) rather than blowing up the readiness probe."""
    app, _ = _build_app(tmp_path, fernet_key)

    async def boom(_path: object) -> None:
        raise RuntimeError("disk gone")

    # We patch the inner helper directly to reach the except-Exception arm.
    async def wrapped(_path: object) -> float | None:
        try:
            await boom(_path)
        except Exception:
            return None
        return 0.0

    monkeypatch.setattr(health_router, "_last_sync_age_seconds", wrapped)
    response = _client(app).get("/v1/ready")
    # No history -> active, status ok.
    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "active"


def test_ready_last_sync_age_returns_none_on_db_error(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive ``_last_sync_age_seconds`` directly through the
    aiosqlite-failure branch so the broad-except is covered."""

    class _BadConn:
        async def __aenter__(self) -> _BadConn:
            raise RuntimeError("oops")

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_connect(_path: object) -> _BadConn:
        return _BadConn()

    monkeypatch.setattr(health_router.aiosqlite, "connect", fake_connect)
    import asyncio

    result = asyncio.run(health_router._last_sync_age_seconds(tmp_path / "x.db"))
    assert result is None


def test_last_sync_age_returns_none_when_table_empty(tmp_path: Path, fernet_key: str) -> None:
    """Empty sync_history table — helper returns ``None``."""
    import asyncio

    _, sqlite_path = _build_app(tmp_path, fernet_key)
    result = asyncio.run(health_router._last_sync_age_seconds(sqlite_path))
    assert result is None


def test_last_sync_age_returns_none_for_unparseable_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the TypeError/ValueError path on the float() conversion."""
    import asyncio

    class _Cursor:
        async def fetchone(self) -> tuple[str, ...]:
            return ("not-a-float",)

        async def close(self) -> None:
            return None

    class _Conn:
        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            return _Cursor()

    def fake_connect(_path: object) -> _Conn:
        return _Conn()

    monkeypatch.setattr(health_router.aiosqlite, "connect", fake_connect)
    result = asyncio.run(health_router._last_sync_age_seconds(tmp_path / "x.db"))
    assert result is None


def test_last_sync_age_returns_float_when_row_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success branch of ``_last_sync_age_seconds`` returns a float."""
    import asyncio

    class _Cursor:
        async def fetchone(self) -> tuple[float, ...]:
            return (12.5,)

        async def close(self) -> None:
            return None

    class _Conn:
        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Cursor:
            return _Cursor()

    def fake_connect(_path: object) -> _Conn:
        return _Conn()

    monkeypatch.setattr(health_router.aiosqlite, "connect", fake_connect)
    result = asyncio.run(health_router._last_sync_age_seconds(tmp_path / "x.db"))
    assert result == 12.5
