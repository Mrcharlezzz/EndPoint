"""Per-user schedule-override REST surface at ``/api/config/overrides``.

Covers the full CRUD contract: list (empty + populated), get-one (existing
and 404), upsert (create + update), delete (existing + 404), and body
validation (422 on malformed payloads). All paths are pure-SQLite so no
``django_db`` marker is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _with_xrw(client: TestClient) -> TestClient:
    """Mutations require X-Requested-With to pass the CSRF-style header gate."""
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


def test_list_overrides_empty(test_client: TestClient) -> None:
    response = test_client.get("/api/config/overrides")
    assert response.status_code == 200
    assert response.json() == []


def test_list_overrides_after_put(test_client: TestClient) -> None:
    put_resp = test_client.put(
        "/api/config/overrides/alice@x",
        json={"platform": "moodle", "interval_seconds": 300},
    )
    assert put_resp.status_code == 200

    list_resp = test_client.get("/api/config/overrides")
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == 1
    assert rows[0]["user_email"] == "alice@x"
    assert rows[0]["platform"] == "moodle"
    assert rows[0]["scope"] == "default"
    assert rows[0]["interval_seconds"] == 300
    assert rows[0]["updated_at"]  # non-empty ISO timestamp


def test_get_override_404_when_missing(test_client: TestClient) -> None:
    response = test_client.get("/api/config/overrides/ghost@x")
    assert response.status_code == 404


def test_get_override_returns_all_rows_for_email(test_client: TestClient) -> None:
    test_client.put(
        "/api/config/overrides/bob@x",
        json={"platform": "moodle", "interval_seconds": 600},
    )
    test_client.put(
        "/api/config/overrides/bob@x",
        json={"platform": "teams", "scope": "urgent", "interval_seconds": 60},
    )

    response = test_client.get("/api/config/overrides/bob@x")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    platforms = sorted(r["platform"] for r in rows)
    assert platforms == ["moodle", "teams"]


def test_put_creates_new_override(test_client: TestClient) -> None:
    response = test_client.put(
        "/api/config/overrides/carol@x",
        json={"platform": "telegram", "interval_seconds": 120},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user_email"] == "carol@x"
    assert body["platform"] == "telegram"
    assert body["scope"] == "default"
    assert body["interval_seconds"] == 120


def test_put_updates_existing_override(test_client: TestClient) -> None:
    test_client.put(
        "/api/config/overrides/dave@x",
        json={"platform": "moodle", "interval_seconds": 600},
    )
    update_resp = test_client.put(
        "/api/config/overrides/dave@x",
        json={"platform": "moodle", "interval_seconds": 90},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["interval_seconds"] == 90

    # Still exactly one row for (dave@x, moodle, default) after the update.
    rows = test_client.get("/api/config/overrides/dave@x").json()
    assert len(rows) == 1
    assert rows[0]["interval_seconds"] == 90


def test_delete_existing_override(test_client: TestClient) -> None:
    test_client.put(
        "/api/config/overrides/erin@x",
        json={"platform": "moodle", "interval_seconds": 300},
    )
    delete_resp = test_client.delete("/api/config/overrides/erin@x")
    assert delete_resp.status_code == 204

    # Confirm gone.
    assert test_client.get("/api/config/overrides/erin@x").status_code == 404


def test_delete_nonexistent_override_returns_404(test_client: TestClient) -> None:
    response = test_client.delete("/api/config/overrides/ghost@x")
    assert response.status_code == 404


def test_put_malformed_body_returns_422(test_client: TestClient) -> None:
    # Missing required ``platform``.
    response = test_client.put(
        "/api/config/overrides/frank@x",
        json={"interval_seconds": 300},
    )
    assert response.status_code == 422


def test_put_rejects_non_positive_interval(test_client: TestClient) -> None:
    response = test_client.put(
        "/api/config/overrides/gina@x",
        json={"platform": "moodle", "interval_seconds": 0},
    )
    assert response.status_code == 422
