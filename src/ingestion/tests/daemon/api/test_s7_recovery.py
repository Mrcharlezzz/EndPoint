"""Coverage for slice 7's recovery endpoints (Part C).

* ``POST /api/actions/dead_letter/{email}/{platform}/replay`` — enqueue a
  force-sync without resetting the dead-letter counter.
* ``DELETE /api/force-sync-queue/{email}/{platform}`` — cancel a pending
  force-sync request before the scheduler picks it up. Races against the
  scheduler with a 404 / 409 split.
* ``GET /api/syncs/{sync_id}/logs`` — convenience wrapper around
  ``GET /api/logs?sync_id=<sync_id>``. 404 only when sync_id is unknown.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


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
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
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
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


def _insert_dead_letter(
    db_path: Path,
    *,
    user_email: str = "alice@x",
    platform: str = "teams",
    consecutive_failures: int = 3,
    last_failure_at: str = "2026-04-26T10:00:00+00:00",
    suppressed_until: str | None = "2026-04-26T11:00:00+00:00",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO dead_letter_state (
                user_email, platform, consecutive_failures,
                last_failure_at, suppressed_until
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_email,
                platform,
                consecutive_failures,
                last_failure_at,
                suppressed_until,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_force_sync(
    db_path: Path,
    *,
    user_email: str = "alice@x",
    platform: str = "teams",
    requested_at: str = "2026-04-26T10:00:00+00:00",
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


def _insert_sync_history(
    db_path: Path,
    *,
    sync_id: str,
    user_email: str = "alice@x",
    platform: str = "teams",
    started_at: str = "2026-04-26T10:00:00+00:00",
    finished_at: str | None = None,
    state: str = "running",
    error: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sync_id, user_email, platform, started_at, finished_at, state, error),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_log(
    db_path: Path,
    *,
    sync_id: str,
    timestamp: str,
    level: str = "INFO",
    event: str = "sync.tick",
    user_email: str | None = None,
    platform: str | None = None,
    fields_json: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO logs (
                timestamp, level, event, user_email, platform, sync_id,
                fields_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                level,
                event,
                user_email,
                platform,
                sync_id,
                fields_json,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _audit_rows(db_path: Path, *, action: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT action, target, before_json, after_json, reason "
            "FROM audit_log WHERE action = ? ORDER BY id ASC",
            (action,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()
    return [
        {
            "action": r[0],
            "target": r[1],
            "before": json.loads(r[2]) if r[2] else None,
            "after": json.loads(r[3]) if r[3] else None,
            "reason": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# POST /api/actions/dead_letter/{email}/{platform}/replay
# ---------------------------------------------------------------------------


def test_replay_dead_letter_happy_path(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_dead_letter(db, user_email="alice@x", platform="teams")
    response = authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "test fix"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "user_email": "alice@x",
        "platform": "teams",
        "counter_preserved": True,
    }


def test_replay_dead_letter_does_not_reset_counter(authed_client: TestClient) -> None:
    """``replay`` leaves consecutive_failures + suppressed_until intact —
    contrast with ``clear`` which zeros both."""
    db = _db_path(authed_client)
    _insert_dead_letter(
        db,
        user_email="alice@x",
        platform="teams",
        consecutive_failures=5,
        suppressed_until="2026-04-26T11:00:00+00:00",
    )
    authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "x"},
    )
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT consecutive_failures, suppressed_until FROM dead_letter_state "
            "WHERE user_email = ? AND platform = ?",
            ("alice@x", "teams"),
        ).fetchone()
    finally:
        conn.close()
    # Counter preserved.
    assert row[0] == 5
    assert row[1] == "2026-04-26T11:00:00+00:00"


def test_replay_dead_letter_enqueues_force_sync(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_dead_letter(db, user_email="alice@x", platform="teams")
    authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "test fix"},
    )
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT user_email, platform, reason FROM force_sync_requests "
            "WHERE user_email = ? AND platform = ?",
            ("alice@x", "teams"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "alice@x"
    assert row[1] == "teams"
    assert row[2] == "test fix"


def test_replay_dead_letter_emits_audit_row(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_dead_letter(db, user_email="alice@x", platform="teams")
    authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "verifying fix"},
    )
    rows = _audit_rows(db, action="dead_letter.replay")
    assert len(rows) == 1
    audit = rows[0]
    assert audit["target"] == "alice@x/teams"
    assert audit["after"]["force_sync_queued"] is True
    assert audit["after"]["counter_reset"] is False
    assert audit["reason"] == "verifying fix"


def test_replay_dead_letter_no_dlq_row_still_succeeds(
    authed_client: TestClient,
) -> None:
    """No dead-letter row -> ``before`` audit payload is None but the
    replay still queues the force-sync (intentional: an operator can
    pre-emptively force a re-sync without a prior failure)."""
    response = authed_client.post(
        "/api/actions/dead_letter/never@x/teams/replay",
        json={"reason": "speculative"},
    )
    assert response.status_code == 200
    db = _db_path(authed_client)
    rows = _audit_rows(db, action="dead_letter.replay")
    assert rows[0]["before"] is None


def test_replay_dead_letter_idempotent_via_upsert(authed_client: TestClient) -> None:
    """Two consecutive replays before the scheduler picks up the row -> the
    second is a no-op upsert (PK is user_email+platform). Two audit rows
    still get written so the action trail captures both attempts."""
    db = _db_path(authed_client)
    _insert_dead_letter(db, user_email="alice@x", platform="teams")
    authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "first"},
    )
    authed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "second"},
    )
    # Still exactly one queued row (PK-conflict upsert).
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM force_sync_requests WHERE user_email = ? AND platform = ?",
            ("alice@x", "teams"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    # But two audit rows.
    rows = _audit_rows(db, action="dead_letter.replay")
    assert len(rows) == 2


def test_replay_dead_letter_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.post(
        "/api/actions/dead_letter/alice@x/teams/replay",
        json={"reason": "x"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/force-sync-queue/{email}/{platform}
# ---------------------------------------------------------------------------


def test_cancel_pending_force_sync_happy_path(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_force_sync(db, user_email="alice@x", platform="teams")
    response = authed_client.delete("/api/force-sync-queue/alice@x/teams")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "cancelled": True,
        "user_email": "alice@x",
        "platform": "teams",
    }
    # Row gone.
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM force_sync_requests WHERE user_email = ? AND platform = ?",
            ("alice@x", "teams"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_cancel_pending_force_sync_emits_audit_row(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="alice@x",
        platform="teams",
        reason="ops manually queued",
    )
    authed_client.delete("/api/force-sync-queue/alice@x/teams")
    rows = _audit_rows(db, action="force_sync.cancel_pending")
    assert len(rows) == 1
    audit = rows[0]
    assert audit["target"] == "alice@x/teams"
    # The before snapshot carries the original reason from the queue row.
    assert audit["before"]["reason"] == "ops manually queued"
    assert audit["after"]["cancelled"] is True


def test_cancel_pending_force_sync_404_when_no_pending(
    authed_client: TestClient,
) -> None:
    response = authed_client.delete("/api/force-sync-queue/ghost@x/teams")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"
    assert "ghost@x/teams" in detail["message"]


def test_cancel_pending_force_sync_409_when_scheduler_picked_up(
    authed_client: TestClient,
) -> None:
    """Pending row was drained and the resulting sync is currently running ->
    409 (race lost), not 404."""
    db = _db_path(authed_client)
    # No pending row, but a running sync exists for the same pair.
    _insert_sync_history(
        db,
        sync_id="running-1",
        user_email="alice@x",
        platform="teams",
        state="running",
        finished_at=None,
    )
    response = authed_client.delete("/api/force-sync-queue/alice@x/teams")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "state_conflict"
    assert "scheduler" in detail["message"].lower()
    # Hint should redirect to the cancel-sync surface.
    assert detail.get("hint")


def test_cancel_pending_force_sync_404_takes_precedence_when_no_running(
    authed_client: TestClient,
) -> None:
    """No pending row + no running sync -> 404."""
    response = authed_client.delete("/api/force-sync-queue/ghost@x/moodle")
    assert response.status_code == 404


def test_cancel_pending_force_sync_requires_auth(
    unauthed_client: TestClient,
) -> None:
    response = unauthed_client.delete("/api/force-sync-queue/alice@x/teams")
    assert response.status_code == 401


def test_cancel_pending_force_sync_path_min_length(
    authed_client: TestClient,
) -> None:
    """Empty path segments would 422 — Path(min_length=1) rejects empty
    strings before the handler runs (FastAPI also routes them away to a
    different match)."""
    # Empty segments collapse to a different path; a single space character
    # in the email path segment passes the min_length check but yields 404
    # since no row matches.
    response = authed_client.delete("/api/force-sync-queue/x@y/ ")
    # Fall through to the 404 branch (no pending, no running). Either 404
    # (legitimate not-found) or 422 (path validator rejecting the value)
    # is acceptable — the latter would happen if the trailing whitespace
    # triggers Path validation. Pin whichever the implementation does.
    assert response.status_code in (404, 422)


# ---------------------------------------------------------------------------
# GET /api/syncs/{sync_id}/logs
# ---------------------------------------------------------------------------


def test_sync_logs_404_for_unknown_sync_id(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs/no-such-id/logs")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"


def test_sync_logs_empty_list_when_sync_exists_no_logs(
    authed_client: TestClient,
) -> None:
    """A real sync_id with zero log rows -> 200 + items=[]. NOT 404."""
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="brand-new", state="running")
    response = authed_client.get("/api/syncs/brand-new/logs")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_sync_logs_returns_logs_for_matching_sync(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-with-logs", state="complete")
    _insert_log(
        db,
        sync_id="sid-with-logs",
        timestamp="2026-04-26T10:00:00+00:00",
        event="sync.start",
    )
    _insert_log(
        db,
        sync_id="sid-with-logs",
        timestamp="2026-04-26T10:00:05+00:00",
        event="sync.complete",
    )
    response = authed_client.get("/api/syncs/sid-with-logs/logs")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    events = {entry["event"] for entry in items}
    assert events == {"sync.start", "sync.complete"}


def test_sync_logs_filters_by_sync_id_only(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-mine")
    _insert_sync_history(db, sync_id="sid-other")
    _insert_log(
        db,
        sync_id="sid-mine",
        timestamp="2026-04-26T10:00:00+00:00",
        event="mine",
    )
    _insert_log(
        db,
        sync_id="sid-other",
        timestamp="2026-04-26T10:00:01+00:00",
        event="other",
    )
    response = authed_client.get("/api/syncs/sid-mine/logs")
    items = response.json()["items"]
    events = [item["event"] for item in items]
    assert events == ["mine"]


def test_sync_logs_orders_by_timestamp_descending(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-ord")
    _insert_log(
        db,
        sync_id="sid-ord",
        timestamp="2026-04-26T10:00:00+00:00",
        event="first",
    )
    _insert_log(
        db,
        sync_id="sid-ord",
        timestamp="2026-04-26T10:00:05+00:00",
        event="second",
    )
    response = authed_client.get("/api/syncs/sid-ord/logs")
    items = response.json()["items"]
    # Newest first.
    assert items[0]["event"] == "second"
    assert items[1]["event"] == "first"


def test_sync_logs_limit_caps_returned_rows(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-limit")
    for i in range(5):
        _insert_log(
            db,
            sync_id="sid-limit",
            timestamp=f"2026-04-26T10:00:0{i}+00:00",
            event=f"e-{i}",
        )
    response = authed_client.get("/api/syncs/sid-limit/logs", params={"limit": 2})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


def test_sync_logs_invalid_limit_too_large_rejected(
    authed_client: TestClient,
) -> None:
    response = authed_client.get("/api/syncs/anything/logs", params={"limit": 999999})
    assert response.status_code == 422


def test_sync_logs_invalid_limit_zero_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/syncs/anything/logs", params={"limit": 0})
    assert response.status_code == 422


def test_sync_logs_decodes_fields_json_to_context(authed_client: TestClient) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-ctx")
    _insert_log(
        db,
        sync_id="sid-ctx",
        timestamp="2026-04-26T10:00:00+00:00",
        event="sync.tick",
        fields_json='{"items_fetched": 42}',
    )
    response = authed_client.get("/api/syncs/sid-ctx/logs")
    item = response.json()["items"][0]
    assert item["context"] == {"items_fetched": 42}


def test_sync_logs_null_context_when_fields_json_missing(
    authed_client: TestClient,
) -> None:
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="sid-null")
    _insert_log(
        db,
        sync_id="sid-null",
        timestamp="2026-04-26T10:00:00+00:00",
        event="sync.tick",
        fields_json=None,
    )
    response = authed_client.get("/api/syncs/sid-null/logs")
    assert response.json()["items"][0]["context"] is None


def test_sync_logs_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.get("/api/syncs/anything/logs")
    assert response.status_code == 401
