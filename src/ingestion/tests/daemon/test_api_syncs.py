"""Coverage for the /api/syncs list router, especially the new ``kind`` filter.

``sync_history`` now doubles as the backing store for credential-refresh runs
(``sync_kind='credential_refresh'``). Callers that want to render the V2
Syncs page should pass ``kind=content_sync`` and callers that want the V6
Credentials page's "Refresh activity" tab should pass
``kind=credential_refresh``. No kind = everything, preserving the pre-kind
callers' behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


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
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _insert(
    db_path: Path,
    *,
    sync_id: str,
    user_email: str,
    platform: str,
    state: str,
    sync_kind: str,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state, sync_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sync_id,
                user_email,
                platform,
                "2026-04-20T00:00:00+00:00",
                "2026-04-20T00:00:05+00:00",
                state,
                sync_kind,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


def _ids(body: list[dict[str, Any]]) -> set[str]:
    return {item["id"] for item in body}


def test_list_syncs_kind_none_returns_every_row(test_client: TestClient) -> None:
    db = _db_path(test_client)
    _insert(
        db,
        sync_id="c-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="content_sync",
    )
    _insert(
        db,
        sync_id="r-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="credential_refresh",
    )

    response = test_client.get("/api/syncs")
    assert response.status_code == 200
    assert _ids(response.json()) == {"c-1", "r-1"}


def test_list_syncs_kind_content_sync_filters_out_refreshes(test_client: TestClient) -> None:
    db = _db_path(test_client)
    _insert(
        db,
        sync_id="c-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="content_sync",
    )
    _insert(
        db,
        sync_id="r-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="credential_refresh",
    )

    response = test_client.get("/api/syncs", params={"kind": "content_sync"})
    assert response.status_code == 200
    assert _ids(response.json()) == {"c-1"}


def test_list_syncs_kind_credential_refresh_filters_out_content_syncs(
    test_client: TestClient,
) -> None:
    db = _db_path(test_client)
    _insert(
        db,
        sync_id="c-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="content_sync",
    )
    _insert(
        db,
        sync_id="r-1",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="credential_refresh",
    )
    _insert(
        db,
        sync_id="r-2",
        user_email="b@x",
        platform="teams",
        state="failed",
        sync_kind="credential_refresh",
    )

    response = test_client.get("/api/syncs", params={"kind": "credential_refresh"})
    assert response.status_code == 200
    assert _ids(response.json()) == {"r-1", "r-2"}


def test_list_syncs_kind_rejects_unknown_values(test_client: TestClient) -> None:
    """The discriminator is validated by the StrEnum: anything outside the
    known set should 422 rather than silently match nothing."""
    response = test_client.get("/api/syncs", params={"kind": "bogus"})
    assert response.status_code == 422


def test_list_syncs_kind_composes_with_other_filters(test_client: TestClient) -> None:
    """kind=credential_refresh + state=failed returns only failed refreshes."""
    db = _db_path(test_client)
    _insert(
        db,
        sync_id="r-ok",
        user_email="a@x",
        platform="teams",
        state="complete",
        sync_kind="credential_refresh",
    )
    _insert(
        db,
        sync_id="r-bad",
        user_email="b@x",
        platform="teams",
        state="failed",
        sync_kind="credential_refresh",
    )
    _insert(
        db,
        sync_id="c-bad",
        user_email="c@x",
        platform="teams",
        state="failed",
        sync_kind="content_sync",
    )

    response = test_client.get(
        "/api/syncs",
        params={"kind": "credential_refresh", "state": "failed"},
    )
    assert response.status_code == 200
    assert _ids(response.json()) == {"r-bad"}
