"""Coverage for slice 7's diagnostic endpoints (Part A).

Three read-only operator-facing endpoints land at:

* ``GET /api/syncs/running`` — every running sync_history row with computed
  ``elapsed_seconds`` + ``items_fetched_so_far`` placeholder.
* ``GET /api/schedule/why-not-running/{email}/{platform}`` — single-pair
  blocked-by diagnostic with the wider vocabulary
  (running/not_due/none on top of _blocked_by's cascade).
* ``GET /api/force-sync-queue`` — pending force_sync_requests joined to
  the audit log for the actor.

All three return Pydantic-shape responses with ``extra="forbid"``. Empty
result is ``items: []`` (NOT 404 — empty steady state). 404 only when a
specifically-requested credential row doesn't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _xrw(client: TestClient) -> TestClient:
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


@pytest.fixture
def authed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = _xrw(TestClient(app))
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


@pytest.fixture
def unauthed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    return _xrw(TestClient(app))


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


def _insert_sync_history(
    db_path: Path,
    *,
    sync_id: str,
    user_email: str = "alice@x",
    platform: str = "teams",
    started_at: str = "2026-04-22T12:00:00+00:00",
    finished_at: str | None = None,
    state: str = "running",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sync_id, user_email, platform, started_at, finished_at, state),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_force_sync(
    db_path: Path,
    *,
    user_email: str = "alice@x",
    platform: str = "teams",
    requested_at: str = "2026-04-22T12:00:00+00:00",
    reason: str | None = "ops",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO force_sync_requests (user_email, platform, requested_at, reason)
            VALUES (?, ?, ?, ?)
            """,
            (user_email, platform, requested_at, reason),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/syncs/running
# ---------------------------------------------------------------------------
#
# ``/running`` is registered BEFORE ``/{sync_id}`` in ``routers/syncs.py`` so
# the wildcard does not shadow the literal route. The pin test
# ``test_running_syncs_route_shadowing_documented`` in
# ``tests/edges/s7_endpoints.py`` guards against a regression that would
# reintroduce the original ordering.


def test_running_syncs_empty_returns_empty_list(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs/running")
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": []}


def test_running_syncs_omits_complete_rows(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="run-1",
        state="running",
        finished_at=None,
        started_at="2026-04-26T10:00:00+00:00",
    )
    _insert_sync_history(
        db,
        sync_id="done-1",
        state="complete",
        finished_at="2026-04-26T10:05:00+00:00",
        started_at="2026-04-26T10:00:00+00:00",
    )
    response = authed_client.get("/api/syncs/running")
    assert response.status_code == 200
    items = response.json()["items"]
    sync_ids = {item["sync_id"] for item in items}
    assert sync_ids == {"run-1"}


def test_running_syncs_returns_required_shape(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="run-shape",
        user_email="bob@x",
        platform="moodle",
        started_at="2026-04-26T10:00:00+00:00",
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {
        "sync_id",
        "user_email",
        "platform",
        "started_at",
        "elapsed_seconds",
        "items_fetched_so_far",
    }
    assert item["sync_id"] == "run-shape"
    assert item["user_email"] == "bob@x"
    assert item["platform"] == "moodle"
    assert item["started_at"] == "2026-04-26T10:00:00+00:00"
    assert isinstance(item["elapsed_seconds"], (int, float))
    assert item["elapsed_seconds"] >= 0.0
    # Placeholder field that's nullable today.
    assert item["items_fetched_so_far"] is None


def test_running_syncs_elapsed_seconds_is_positive_for_past_started(
    authed_client: TestClient,
) -> None:
    db = _db_path(authed_client)
    started = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
    _insert_sync_history(
        db,
        sync_id="elapsed-1",
        started_at=started,
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    item = response.json()["items"][0]
    # ~300 seconds elapsed.
    assert item["elapsed_seconds"] >= 250.0


def test_running_syncs_elapsed_clamped_for_future_started(
    authed_client: TestClient,
) -> None:
    """A row with a future started_at (clock skew) clamps to 0."""
    db = _db_path(authed_client)
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    _insert_sync_history(
        db,
        sync_id="future-1",
        started_at=future,
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    item = response.json()["items"][0]
    assert item["elapsed_seconds"] == 0.0


def test_running_syncs_naive_started_at_treated_as_utc(authed_client: TestClient) -> None:
    """Legacy rows with naive timestamps don't crash the read endpoint."""
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="naive-1",
        started_at="2026-04-26T10:00:00",  # no tz suffix
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["sync_id"] == "naive-1"


def test_running_syncs_handles_unparseable_started_at(authed_client: TestClient) -> None:
    """Unparseable started_at degrades to elapsed=0 rather than crashing."""
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="garbled-1",
        started_at="not-a-timestamp",
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["elapsed_seconds"] == 0.0


def test_running_syncs_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.get("/api/syncs/running")
    assert response.status_code == 401


def test_running_syncs_skips_state_running_with_finished_at(
    authed_client: TestClient,
) -> None:
    """Belt-and-braces: state='running' AND finished_at IS NOT NULL is
    a corrupt row that the orphan-reconciler will reset, but the live
    read endpoint also filters it so dashboards don't render zombies."""
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="zombie-1",
        state="running",
        finished_at="2026-04-26T10:05:00+00:00",
        started_at="2026-04-26T10:00:00+00:00",
    )
    response = authed_client.get("/api/syncs/running")
    assert response.json()["items"] == []


def test_running_syncs_orders_by_started_at_ascending(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="r-late",
        started_at="2026-04-26T11:00:00+00:00",
        state="running",
    )
    _insert_sync_history(
        db,
        sync_id="r-early",
        started_at="2026-04-26T10:00:00+00:00",
        state="running",
    )
    response = authed_client.get("/api/syncs/running")
    sync_ids = [item["sync_id"] for item in response.json()["items"]]
    assert sync_ids == ["r-early", "r-late"]


# ---------------------------------------------------------------------------
# GET /api/schedule/why-not-running/{email}/{platform}
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_why_not_running_404_for_unknown_credential(
    authed_client: TestClient,
) -> None:
    response = authed_client.get("/api/schedule/why-not-running/ghost@x/teams")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"
    assert "ghost@x" in detail["message"]


@pytest.mark.django_db(transaction=True)
def test_why_not_running_eligible_credential_returns_none(
    authed_client: TestClient,
) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/teams")
    assert response.status_code == 200
    body = response.json()
    # Never-run credential -> next_due_at == now() -> surface "none"
    assert body["blocked_by"] == "none"
    assert body["user_email"] == "alice@x.test"
    assert body["platform"] == "teams"
    assert body["reason"]
    # next_due_at is "now" for eligible-with-no-prior runs.
    assert body["next_due_at"] is not None


@pytest.mark.django_db(transaction=True)
def test_why_not_running_paused_credential(authed_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="paused@x.test",
        display_name="Paused",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_paused=True,
    )
    response = authed_client.get("/api/schedule/why-not-running/paused@x.test/moodle")
    body = response.json()
    assert body["blocked_by"] == "paused"
    # Permanent block -> no next_due_at.
    assert body["next_due_at"] is None
    assert "paused" in body["reason"].lower()


@pytest.mark.django_db(transaction=True)
def test_why_not_running_expired_credential(authed_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="expired@x.test",
        display_name="Expired",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_expired=True,
    )
    response = authed_client.get("/api/schedule/why-not-running/expired@x.test/moodle")
    body = response.json()
    assert body["blocked_by"] == "expired"
    assert body["next_due_at"] is None


@pytest.mark.django_db(transaction=True)
def test_why_not_running_running_state(authed_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="busy@x.test",
        display_name="Busy",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="running-id",
        user_email="busy@x.test",
        platform="teams",
        state="running",
        finished_at=None,
    )
    response = authed_client.get("/api/schedule/why-not-running/busy@x.test/teams")
    body = response.json()
    assert body["blocked_by"] == "running"
    # While running, next_due_at is N/A.
    assert body["next_due_at"] is None


@pytest.mark.django_db(transaction=True)
def test_why_not_running_not_due_state(authed_client: TestClient) -> None:
    """Credential is eligible but the interval hasn't elapsed yet."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="recent@x.test",
        display_name="Recent",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    db = _db_path(authed_client)
    # finished_at == now() -> last_success at "now", default interval is 600s
    # so we're not_due.
    just_finished = datetime.now(tz=UTC).isoformat()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, finished_at, state)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "ok-1",
                "recent@x.test",
                "teams",
                just_finished,
                just_finished,
                "complete",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    response = authed_client.get("/api/schedule/why-not-running/recent@x.test/teams")
    body = response.json()
    assert body["blocked_by"] == "not_due"
    # Transient block -> next_due_at returned as a real timestamp.
    assert body["next_due_at"] is not None


@pytest.mark.django_db(transaction=True)
def test_why_not_running_suppressed_with_future_until(
    authed_client: TestClient,
) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="dl@x.test",
        display_name="DL",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(str(_db_path(authed_client)))
    try:
        conn.execute(
            """
            INSERT INTO dead_letter_state (
                user_email, platform, consecutive_failures,
                last_failure_at, suppressed_until
            ) VALUES (?, ?, 3, ?, ?)
            """,
            ("dl@x.test", "teams", future, future),
        )
        conn.commit()
    finally:
        conn.close()
    response = authed_client.get("/api/schedule/why-not-running/dl@x.test/teams")
    body = response.json()
    assert body["blocked_by"] == "suppressed"
    # Suppressed is transient — next_due_at carries the suppression
    # release time.
    assert body["next_due_at"] is not None


@pytest.mark.django_db(transaction=True)
def test_why_not_running_global_paused_overrides_credential_state(
    authed_client: TestClient,
) -> None:
    """Cascade: global pause wins over per-credential expired."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
        is_expired=True,
    )
    # Flip global_paused via overlay.
    overlay = authed_client.put(
        "/api/config/overlay/scheduler.global_paused",
        json={"value": True},
    )
    assert overlay.status_code == 200
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/teams")
    assert response.json()["blocked_by"] == "global_paused"


@pytest.mark.django_db(transaction=True)
def test_why_not_running_emergency_stop(authed_client: TestClient) -> None:
    """Emergency stop is the top-of-stack cause and wins over every other
    state (including credential-level paused/expired)."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
        is_paused=True,
    )
    overlay = authed_client.put(
        "/api/config/overlay/scheduler.emergency_stop_requested",
        json={"value": True},
    )
    assert overlay.status_code == 200
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/teams")
    assert response.json()["blocked_by"] == "emergency_stop"


@pytest.mark.django_db(transaction=True)
def test_why_not_running_drain_mode(authed_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    overlay = authed_client.put(
        "/api/config/overlay/scheduler.drain_mode",
        json={"value": True},
    )
    assert overlay.status_code == 200
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/teams")
    assert response.json()["blocked_by"] == "drain_mode"


@pytest.mark.django_db(transaction=True)
def test_why_not_running_unknown_platform_treated_as_enabled(
    authed_client: TestClient,
) -> None:
    """Schedule API mirrors visibility — unknown platform is ENABLED so
    the row is still rendered even though the scheduler would gate it."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="zoom",  # not in the platform registry
        encrypted_payload=b"\x00",
    )
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/zoom")
    body = response.json()
    # Platform is treated as enabled + not-paused for visibility, so the
    # row falls through to the eligible-per-cred branches.
    assert body["blocked_by"] != "platform_disabled"
    assert body["blocked_by"] != "platform_paused"


@pytest.mark.django_db(transaction=True)
def test_why_not_running_platform_paused(authed_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    overlay = authed_client.put(
        "/api/config/overlay/platforms.paused.teams",
        json={"value": True},
    )
    assert overlay.status_code == 200
    response = authed_client.get("/api/schedule/why-not-running/alice@x.test/teams")
    assert response.json()["blocked_by"] == "platform_paused"


def test_why_not_running_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.get("/api/schedule/why-not-running/x@y/teams")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/force-sync-queue
# ---------------------------------------------------------------------------


def test_force_sync_queue_empty_returns_empty_list(authed_client: TestClient) -> None:
    response = authed_client.get("/api/force-sync-queue")
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": []}


def test_force_sync_queue_returns_pending_rows(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="alice@x",
        platform="moodle",
        requested_at="2026-04-26T10:00:00+00:00",
        reason="manual retry",
    )
    response = authed_client.get("/api/force-sync-queue")
    body = response.json()
    items = body["items"]
    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {
        "user_email",
        "platform",
        "queued_at",
        "reason",
        "requested_by_actor",
    }
    assert item["user_email"] == "alice@x"
    assert item["platform"] == "moodle"
    assert item["queued_at"] == "2026-04-26T10:00:00+00:00"
    assert item["reason"] == "manual retry"
    # No matching audit row -> actor is None.
    assert item["requested_by_actor"] is None


def test_force_sync_queue_orders_by_requested_at_ascending(
    authed_client: TestClient,
) -> None:
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="b@x",
        platform="teams",
        requested_at="2026-04-26T11:00:00+00:00",
    )
    _insert_force_sync(
        db,
        user_email="a@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
    )
    response = authed_client.get("/api/force-sync-queue")
    items = response.json()["items"]
    queued = [i["queued_at"] for i in items]
    assert queued == sorted(queued)


def test_force_sync_queue_joins_audit_actor_when_available(
    authed_client: TestClient,
) -> None:
    """Insert a force-sync row + a matching sync.force_requested audit row;
    the LEFT JOIN should surface the actor."""
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="actor@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
    )
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO audit_log (timestamp, actor, action, target)
            VALUES (?, ?, ?, ?)
            """,
            (
                "2026-04-26T10:00:00+00:00",
                "admin@127.0.0.1/123",
                "sync.force_requested",
                "actor@x/teams",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    response = authed_client.get("/api/force-sync-queue")
    items = response.json()["items"]
    matching = [i for i in items if i["user_email"] == "actor@x"]
    assert matching, "expected actor@x row in queue"
    assert matching[0]["requested_by_actor"] == "admin@127.0.0.1/123"


def test_force_sync_queue_picks_latest_audit_actor(authed_client: TestClient) -> None:
    """Multiple audit rows for the same target -> pick the most recent."""
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="multi@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
    )
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO audit_log (timestamp, actor, action, target) VALUES (?, ?, ?, ?)",
            ("2026-04-26T09:00:00+00:00", "old-actor", "sync.force_requested", "multi@x/teams"),
        )
        conn.execute(
            "INSERT INTO audit_log (timestamp, actor, action, target) VALUES (?, ?, ?, ?)",
            ("2026-04-26T10:00:00+00:00", "new-actor", "sync.force_requested", "multi@x/teams"),
        )
        conn.commit()
    finally:
        conn.close()
    response = authed_client.get("/api/force-sync-queue")
    item = next(i for i in response.json()["items"] if i["user_email"] == "multi@x")
    assert item["requested_by_actor"] == "new-actor"


def test_force_sync_queue_null_reason_serialised_as_null(
    authed_client: TestClient,
) -> None:
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="r@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
        reason=None,
    )
    response = authed_client.get("/api/force-sync-queue")
    item = next(i for i in response.json()["items"] if i["user_email"] == "r@x")
    assert item["reason"] is None


def test_force_sync_queue_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.get("/api/force-sync-queue")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Adjacent backfill: existing /api/syncs endpoints
# ---------------------------------------------------------------------------


def test_list_syncs_empty_returns_empty_list(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_syncs_limit_caps_returned_rows(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    for i in range(5):
        _insert_sync_history(
            db,
            sync_id=f"sid-{i}",
            user_email="alice@x",
            platform="teams",
            started_at=f"2026-04-26T10:0{i}:00+00:00",
            finished_at=f"2026-04-26T10:0{i}:30+00:00",
            state="complete",
        )
    response = authed_client.get("/api/syncs", params={"limit": 2})
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_syncs_limit_too_large_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs", params={"limit": 999999})
    assert response.status_code == 422


def test_list_syncs_filter_by_user_email(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="a-1", user_email="alice@x", platform="teams")
    _insert_sync_history(db, sync_id="b-1", user_email="bob@x", platform="teams")
    response = authed_client.get("/api/syncs", params={"user_email": "bob@x"})
    sync_ids = {row["id"] for row in response.json()}
    assert sync_ids == {"b-1"}


def test_list_syncs_filter_by_platform(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="t-1", platform="teams")
    _insert_sync_history(db, sync_id="m-1", platform="moodle")
    response = authed_client.get("/api/syncs", params={"platform": "moodle"})
    sync_ids = {row["id"] for row in response.json()}
    assert sync_ids == {"m-1"}


def test_list_syncs_filter_by_state(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="ok-1", state="complete")
    _insert_sync_history(db, sync_id="bad-1", state="failed")
    response = authed_client.get("/api/syncs", params={"state": "failed"})
    sync_ids = {row["id"] for row in response.json()}
    assert sync_ids == {"bad-1"}


def test_list_syncs_filter_invalid_state_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs", params={"state": "weird"})
    assert response.status_code == 422


def test_sync_detail_404_for_unknown(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs/no-such-id")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"


def test_sync_detail_returns_history_and_no_profile_when_missing(
    authed_client: TestClient,
) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="solo-1",
        user_email="alice@x",
        platform="teams",
        state="complete",
    )
    response = authed_client.get("/api/syncs/solo-1")
    assert response.status_code == 200
    body = response.json()
    assert body["history"]["id"] == "solo-1"
    assert body["profile"] is None
