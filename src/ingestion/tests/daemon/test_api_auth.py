"""API auth: login issues a cookie; protected endpoints require it."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import COOKIE_NAME, AuthSettings
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _test_with_xrw(client: TestClient) -> TestClient:
    """Default every request to include X-Requested-With so mutation
    routes pass the API's CSRF-style header gate."""
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


@pytest.fixture
def test_client(tmp_path: Path, fernet_key: str) -> TestClient:
    store = open_store(tmp_path / "state.db")
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    from ingestion.daemon.config import DaemonConfig

    config = DaemonConfig(sqlite_path=tmp_path / "state.db")
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    return _test_with_xrw(TestClient(app))


def test_login_wrong_password_returns_401(test_client: TestClient) -> None:
    response = test_client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401


def test_login_correct_password_sets_cookie(test_client: TestClient) -> None:
    response = test_client.post("/api/auth/login", json={"password": "hunter2-strong"})
    assert response.status_code == 200
    assert response.cookies.get(COOKIE_NAME) is not None


def test_unauthenticated_protected_route_returns_401(test_client: TestClient) -> None:
    test_client.cookies.clear()
    response = test_client.get("/api/audit")
    assert response.status_code == 401


def test_authenticated_protected_route_returns_200(test_client: TestClient) -> None:
    test_client.post("/api/auth/login", json={"password": "hunter2-strong"})
    response = test_client.get("/api/audit")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_logout_clears_cookie(test_client: TestClient) -> None:
    test_client.post("/api/auth/login", json={"password": "hunter2-strong"})
    logout = test_client.post("/api/auth/logout")
    assert logout.status_code == 200
    # After logout, the protected route should 401 again.
    test_client.cookies.clear()
    followup = test_client.get("/api/audit")
    assert followup.status_code == 401


def test_tampered_cookie_rejected(test_client: TestClient) -> None:
    test_client.cookies.set(COOKIE_NAME, "not-a-valid-signed-cookie")
    response = test_client.get("/api/audit")
    assert response.status_code == 401


def test_openapi_schema_public(test_client: TestClient) -> None:
    # FastAPI's OpenAPI endpoint is not behind auth by default, so the
    # frontend can fetch the schema without needing a session.
    response = test_client.get("/api/openapi.json")
    assert response.status_code == 200
    body = response.json()
    assert "paths" in body
    assert "/api/auth/login" in body["paths"]


def test_missing_session_secret_at_startup_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ENDPOINT_SESSION_SECRET"):
        AuthSettings(admin_password="hunter2-strong", session_secret="")


def test_missing_admin_password_at_startup_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ENDPOINT_ADMIN_PASSWORD"):
        AuthSettings(admin_password="", session_secret="x")
