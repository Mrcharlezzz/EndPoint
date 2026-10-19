"""Coverage for the Phase 5 action endpoints added beyond pause/unpause.

Exercises force_sync, reset_cursor, emergency_stop, mark_expired,
bulk_refresh_teams, purge_item, export_audit, compact_storage, mute_rule.
Destructive-confirm validation is covered explicitly because a typo must
not delete data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _test_with_xrw(client: TestClient) -> TestClient:
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


@pytest.fixture
def test_client(tmp_path: Path, fernet_key: str) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    from ingestion.daemon.config import DaemonConfig

    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = _test_with_xrw(TestClient(app))
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def test_force_sync_enqueues_row(test_client: TestClient) -> None:
    import sqlite3

    response = test_client.post(
        "/api/actions/users/alice@x/moodle/force_sync",
        json={"reason": "manual retry"},
    )
    assert response.status_code == 200
    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT user_email, platform FROM force_sync_requests").fetchall()
    finally:
        conn.close()
    assert ("alice@x", "moodle") in rows


def test_emergency_stop_requires_confirm(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/scheduler/emergency_stop",
        json={"confirm": "not-the-right-word", "reason": "panic"},
    )
    assert response.status_code == 422


def test_emergency_stop_sets_overlay(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/scheduler/emergency_stop",
        json={"confirm": "scheduler", "reason": "panic"},
    )
    assert response.status_code == 200
    overlay = test_client.get("/api/config/overlay").json()
    assert overlay.get("scheduler.emergency_stop_requested") is True


def test_reset_cursor_confirm_mismatch_rejected(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/users/alice@x/moodle/reset_cursor",
        json={"confirm": "wrong", "reason": "stuck sync"},
    )
    assert response.status_code == 422


def test_reset_cursor_deletes_matching_rows(test_client: TestClient) -> None:
    import sqlite3

    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
            VALUES ('bob@x', 'teams', 'default', '{"token":"abc"}', '2026-04-19T00:00:00+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    response = test_client.post(
        "/api/actions/users/bob@x/teams/reset_cursor",
        json={"confirm": "bob@x/teams", "reason": "stuck"},
    )
    assert response.status_code == 200
    assert response.json()["rows_deleted"] == 1


def test_storage_compact_returns_size(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/storage/compact",
        json={"reason": "housekeeping"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["before_bytes"] >= 0
    assert body["after_bytes"] >= 0


def test_audit_export_streams_ndjson(test_client: TestClient) -> None:
    response = test_client.get("/api/actions/audit/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    # Login wrote one audit entry; every entry is one JSON line.
    lines = [line for line in response.text.split("\n") if line]
    assert len(lines) >= 1
    import json

    for line in lines:
        parsed = json.loads(line)
        assert "action" in parsed


def test_mute_rule_flips_enabled(test_client: TestClient) -> None:
    create_resp = test_client.post(
        "/api/alerts/rules",
        json={
            "name": "noisy",
            "metric": "failure_count",
            "comparator": ">",
            "threshold": 1,
            "cooldown_seconds": 60,
        },
    )
    rule_id = create_resp.json()["id"]

    mute = test_client.patch(
        f"/api/actions/alerts/rules/{rule_id}/mute",
        json={"reason": "too loud"},
    )
    assert mute.status_code == 200
    assert mute.json()["enabled"] is False

    audit = test_client.get("/api/audit", params={"action": "alerts.rule_muted"}).json()
    assert any(entry["target"] == "noisy" for entry in audit)


@pytest.mark.django_db(transaction=True)
def test_mark_expired_updates_credential(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="dan@x",
        display_name="Dan",
        platform="moodle",
        encrypted_payload=b"\x00",
    )
    response = test_client.post(
        "/api/actions/users/dan@x/moodle/mark_expired",
        json={"reason": "token revoked upstream"},
    )
    assert response.status_code == 200
    assert response.json()["rows_affected"] == 1


@pytest.mark.django_db(transaction=True)
def test_bulk_refresh_teams_enqueues_all(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    for email in ("a@x", "b@x"):
        PlatformCredential.objects.create(
            user_email=email,
            display_name=email,
            platform="teams",
            encrypted_payload=b"\x00",
        )
    response = test_client.post(
        "/api/actions/teams/refresh_all",
        json={"reason": "rotation"},
    )
    assert response.status_code == 200
    assert response.json()["enqueued"] == 2


# ---- per-credential pause -------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_pause_credential_happy_path(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="p1@x",
        display_name="P1",
        platform="moodle",
        encrypted_payload=b"\x00",
    )
    response = test_client.post(
        "/api/actions/users/p1@x/moodle/pause",
        json={"reason": "triage"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"rows_affected": 1, "rows_total": 1}

    audit = test_client.get("/api/audit", params={"action": "credential.pause"}).json()
    assert any(entry["target"] == "p1@x/moodle" for entry in audit)
    refreshed = PlatformCredential.objects.get(user_email="p1@x", platform="moodle")
    assert refreshed.is_paused is True


@pytest.mark.django_db(transaction=True)
def test_pause_credential_idempotent(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="p2@x",
        display_name="P2",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_paused=True,
    )
    response = test_client.post(
        "/api/actions/users/p2@x/moodle/pause",
        json={"reason": "double-click"},
    )
    assert response.status_code == 200
    # Already paused -> the UPDATE matches zero rows; the endpoint still
    # returns 200 and writes an audit row so operator intent is recorded.
    assert response.json() == {"rows_affected": 0, "rows_total": 1}


@pytest.mark.django_db(transaction=True)
def test_pause_credential_404_when_missing(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/users/ghost@x/moodle/pause",
        json={"reason": "no such row"},
    )
    assert response.status_code == 404


# ---- per-credential unpause -----------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_unpause_credential_happy_path(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="u1@x",
        display_name="U1",
        platform="teams",
        encrypted_payload=b"\x00",
        is_paused=True,
    )
    response = test_client.post(
        "/api/actions/users/u1@x/teams/unpause",
        json={"reason": "resume"},
    )
    assert response.status_code == 200
    assert response.json() == {"rows_affected": 1, "rows_total": 1}

    audit = test_client.get("/api/audit", params={"action": "credential.unpause"}).json()
    assert any(entry["target"] == "u1@x/teams" for entry in audit)
    refreshed = PlatformCredential.objects.get(user_email="u1@x", platform="teams")
    assert refreshed.is_paused is False


@pytest.mark.django_db(transaction=True)
def test_unpause_credential_idempotent(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="u2@x",
        display_name="U2",
        platform="teams",
        encrypted_payload=b"\x00",
        is_paused=False,
    )
    response = test_client.post(
        "/api/actions/users/u2@x/teams/unpause",
        json={"reason": "already open"},
    )
    assert response.status_code == 200
    assert response.json() == {"rows_affected": 0, "rows_total": 1}


@pytest.mark.django_db(transaction=True)
def test_unpause_credential_404_when_missing(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/users/ghost@x/teams/unpause",
        json={"reason": "no such row"},
    )
    assert response.status_code == 404


# ---- sync cancel ----------------------------------------------------------


def _insert_sync_row(
    sqlite_path: Path,
    *,
    sync_id: str,
    state: str = "running",
    user_email: str = "alice@x",
    platform: str = "teams",
) -> None:
    """Seed one ``sync_history`` row via direct SQL.

    The cancel endpoint has no dependency on Django / Postgres, so we stay
    on sqlite3 here to avoid the django_db marker (and to keep the test
    fast: the suite budget is <2s).
    """
    import sqlite3

    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sync_id, user_email, platform, "2026-04-22T00:00:00+00:00", state),
        )
        conn.commit()
    finally:
        conn.close()


def test_cancel_sync_happy_path(test_client: TestClient) -> None:
    import sqlite3

    sqlite_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "11111111-2222-3333-4444-555555555555"
    _insert_sync_row(sqlite_path, sync_id=sync_id, state="running")

    response = test_client.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": sync_id, "reason": "stuck run"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "sync_id": sync_id,
        "previous_state": "running",
        "new_state": "cancelled",
    }

    # The sync row must end in terminal cancelled state.
    conn = sqlite3.connect(str(sqlite_path))
    try:
        row = conn.execute(
            "SELECT state, error, finished_at, stats_json FROM sync_history WHERE id = ?",
            (sync_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    state, error, finished_at, stats_json = row
    assert state == "cancelled"
    assert error == "cancelled by operator"
    assert finished_at is not None
    assert stats_json is None

    audit = test_client.get("/api/audit", params={"action": "sync.cancel"}).json()
    assert any(entry["target"] == sync_id for entry in audit)


def test_cancel_sync_confirm_mismatch_rejected(test_client: TestClient) -> None:
    sqlite_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _insert_sync_row(sqlite_path, sync_id=sync_id, state="running")
    response = test_client.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": "wrong-id", "reason": "typo"},
    )
    assert response.status_code == 422


def test_cancel_sync_404_when_missing(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/syncs/does-not-exist/cancel",
        json={"confirm": "does-not-exist", "reason": "ghost"},
    )
    assert response.status_code == 404


def test_cancel_sync_409_when_terminal(test_client: TestClient) -> None:
    sqlite_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "deadbeef-0000-0000-0000-000000000000"
    _insert_sync_row(sqlite_path, sync_id=sync_id, state="complete")
    response = test_client.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": sync_id, "reason": "too late"},
    )
    assert response.status_code == 409
    # The 409 body must include the current state so the client can render
    # a sensible "already finished" message without a second round-trip.
    # Envelope shape: detail.message carries the human-readable string.
    detail = response.json()["detail"]
    assert detail["code"] == "state_conflict"
    assert "complete" in detail["message"]
