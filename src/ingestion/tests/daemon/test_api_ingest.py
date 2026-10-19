"""Coverage for /api/ingest/full_resync.

Confirms the handler deletes cursor rows for the selected platforms,
enqueues force-sync requests per non-expired credential, skips expired
rows, leaves un-selected platforms untouched, and rejects on typo.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _with_xrw(client: TestClient) -> TestClient:
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
    client = _with_xrw(TestClient(app))
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


def _seed_cursor(db_path: Path, user_email: str, platform: str, scope: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (user_email, platform, scope, json.dumps({"pos": 42})),
        )
        conn.commit()
    finally:
        conn.close()


def _cursor_count(db_path: Path, platform: str | None = None) -> int:
    conn = sqlite3.connect(db_path)
    try:
        if platform is None:
            row = conn.execute("SELECT COUNT(*) FROM sync_cursors").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_cursors WHERE platform = ?",
                (platform,),
            ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _enqueued_pairs(db_path: Path) -> set[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT user_email, platform FROM force_sync_requests").fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]) for r in rows}


def test_full_resync_rejects_wrong_confirm(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "nope",
            "reason": "testing",
            "platforms": ["teams"],
        },
    )
    assert response.status_code == 422


def test_full_resync_rejects_empty_platforms(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "testing",
            "platforms": [],
        },
    )
    assert response.status_code == 422


def test_full_resync_rejects_unknown_platform(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "testing",
            "platforms": ["slack"],
        },
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_full_resync_deletes_cursors_and_enqueues_syncs(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    db = _db_path(test_client)
    _seed_cursor(db, "alice@x", "teams", "channels")
    _seed_cursor(db, "alice@x", "teams", "assignments")
    _seed_cursor(db, "bob@x", "moodle", "default")
    for email, plat in (("alice@x", "teams"), ("bob@x", "moodle")):
        PlatformCredential.objects.create(
            user_email=email,
            display_name=email,
            platform=plat,
            encrypted_payload=b"\x00",
        )

    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "integration rerun",
            "platforms": ["teams", "moodle"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cursors_deleted"] == 3
    assert body["syncs_enqueued"] == 2
    assert body["credentials_skipped"] == 0
    assert body["platforms"] == ["moodle", "teams"]
    assert _cursor_count(db) == 0
    assert _enqueued_pairs(db) == {("alice@x", "teams"), ("bob@x", "moodle")}


@pytest.mark.django_db(transaction=True)
def test_full_resync_skips_expired_credentials(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    db = _db_path(test_client)
    PlatformCredential.objects.create(
        user_email="good@x",
        display_name="Good",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    PlatformCredential.objects.create(
        user_email="stale@x",
        display_name="Stale",
        platform="teams",
        encrypted_payload=b"\x00",
        is_expired=True,
    )

    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "rerun after fix",
            "platforms": ["teams"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["syncs_enqueued"] == 1
    assert body["credentials_skipped"] == 1
    assert _enqueued_pairs(db) == {("good@x", "teams")}


@pytest.mark.django_db(transaction=True)
def test_full_resync_leaves_unselected_platforms_alone(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    db = _db_path(test_client)
    _seed_cursor(db, "alice@x", "teams", "channels")
    _seed_cursor(db, "alice@x", "moodle", "default")
    PlatformCredential.objects.create(
        user_email="alice@x",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    PlatformCredential.objects.create(
        user_email="alice@x",
        display_name="Alice",
        platform="moodle",
        encrypted_payload=b"\x00",
    )

    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "partial rerun",
            "platforms": ["teams"],
        },
    )

    assert response.status_code == 200
    assert _cursor_count(db, "teams") == 0
    # Moodle cursor untouched.
    assert _cursor_count(db, "moodle") == 1
    # Only teams got enqueued.
    assert _enqueued_pairs(db) == {("alice@x", "teams")}


@pytest.mark.django_db(transaction=True)
def test_full_resync_writes_audit_entry(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )

    response = test_client.post(
        "/api/ingest/full_resync",
        json={
            "confirm": "FULL_RESYNC",
            "reason": "full rerun",
            "platforms": ["teams"],
        },
    )
    assert response.status_code == 200

    audit = test_client.get("/api/audit?action=ingest.full_resync").json()
    entries = audit["entries"] if isinstance(audit, dict) and "entries" in audit else audit
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "ingest.full_resync"
    assert entry["target"] == "teams"
    assert entry["reason"] == "full rerun"
