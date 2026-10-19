"""Coverage for the new read-surface routers: credentials, errors, settings.

The credentials + expired-credential paths go through the Django ORM and
carry ``pytest.mark.django_db``. Pure-SQLite paths (dead_letter,
failed syncs, settings) don't.
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


def test_settings_about_returns_daemon_info(test_client: TestClient) -> None:
    response = test_client.get("/api/settings/about")
    assert response.status_code == 200
    body = response.json()
    assert "daemon_version" in body
    assert "python_version" in body
    assert "platform" in body
    # New fields from the V11 Settings About extension.
    assert "build_commit" in body
    assert "postgres_host" in body
    assert "worker_pool_size" in body
    assert isinstance(body["worker_pool_size"], int)


def test_settings_about_reads_build_commit_from_env(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-set ENDPOINT_BUILD_COMMIT wins over any fallback."""
    from ingestion.daemon.api.routers import settings as settings_module

    monkeypatch.setattr(settings_module, "_build_commit_cache", (False, None))
    monkeypatch.setenv("ENDPOINT_BUILD_COMMIT", "deadbeef1234")

    response = test_client.get("/api/settings/about")
    assert response.status_code == 200
    assert response.json()["build_commit"] == "deadbeef1234"


def test_settings_about_postgres_host_masks_password(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL with embedded credentials must surface only the hostname."""
    monkeypatch.setenv(
        "ENDPOINT_DAEMON__DB__URL",
        "postgresql://admin:supersecret@db.internal:5432/shared_db",
    )

    response = test_client.get("/api/settings/about")
    body = response.json()
    assert body["postgres_host"] == "db.internal"
    # Password must not leak into any field.
    for value in body.values():
        if isinstance(value, str):
            assert "supersecret" not in value


def test_settings_about_postgres_host_empty_when_unset(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENDPOINT_DAEMON__DB__URL", raising=False)
    response = test_client.get("/api/settings/about")
    assert response.json()["postgres_host"] == ""


def test_settings_retention_reflects_config(test_client: TestClient) -> None:
    response = test_client.get("/api/settings/retention")
    assert response.status_code == 200
    body = response.json()
    assert body["log_retention_days"] >= 0
    assert "log_to_sqlite" in body
    assert "alert_tick_seconds" in body


def test_settings_storage_counts_zero_for_fresh_db(test_client: TestClient) -> None:
    response = test_client.get("/api/settings/storage")
    assert response.status_code == 200
    body = response.json()
    assert body["row_counts"]["logs"] == 0
    # login wrote one audit entry
    assert body["row_counts"]["audit_log"] >= 1
    assert body["file_size_bytes"] > 0


@pytest.mark.django_db(transaction=True)
def test_errors_endpoint_empty_on_fresh_db(test_client: TestClient) -> None:
    # django_db: the default (mixed) feed also queries expired credentials
    # via the ORM, so touching Postgres requires the marker.
    response = test_client.get("/api/errors")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["counts"]["dead_letter"] == 0
    assert body["counts"]["failed_syncs"] == 0


def test_errors_endpoint_surfaces_failed_sync(test_client: TestClient) -> None:
    """Insert a failed sync_history row directly and confirm /api/errors
    surfaces it with the right kind + truncated error string."""
    import sqlite3

    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s-1",
                "alice@x.test",
                "teams",
                "2026-04-19T00:00:00+00:00",
                "2026-04-19T00:00:10+00:00",
                "failed",
                "boom",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = test_client.get("/api/errors", params={"kind": "failed_sync"})
    assert response.status_code == 200
    body = response.json()
    assert any(item["kind"] == "failed_sync" and item["sync_id"] == "s-1" for item in body["items"])


def test_errors_kind_filter_narrows_feed(test_client: TestClient) -> None:
    response = test_client.get("/api/errors", params={"kind": "dead_letter"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def _insert_sync_row(
    db_path: str,
    *,
    sync_id: str,
    user_email: str,
    platform: str,
    state: str,
    started_at: str,
    finished_at: str | None,
    error: str | None = None,
) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sync_id, user_email, platform, started_at, finished_at, state, error),
        )
        conn.commit()
    finally:
        conn.close()


def test_errors_feed_hides_failed_sync_when_later_complete_exists(
    test_client: TestClient,
) -> None:
    """A failed sync resolved by a later complete on the same (user, platform)
    should NOT appear in the errors feed. Otherwise transient failures stick
    to the Errors page forever even after Teams/Moodle recovers."""
    db_path = str(test_client.app.state.api_context.config.sqlite_path)  # type: ignore[attr-defined]
    _insert_sync_row(
        db_path,
        sync_id="old-fail",
        user_email="eris@x.test",
        platform="teams",
        state="failed",
        started_at="2026-04-22T15:25:00+00:00",
        finished_at="2026-04-22T15:26:00+00:00",
        error="transient blip",
    )
    _insert_sync_row(
        db_path,
        sync_id="later-ok",
        user_email="eris@x.test",
        platform="teams",
        state="complete",
        started_at="2026-04-22T16:09:00+00:00",
        finished_at="2026-04-22T16:10:00+00:00",
    )

    response = test_client.get("/api/errors", params={"kind": "failed_sync"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert not any(item["sync_id"] == "old-fail" for item in items)


def test_errors_feed_keeps_failed_sync_with_no_later_complete(
    test_client: TestClient,
) -> None:
    db_path = str(test_client.app.state.api_context.config.sqlite_path)  # type: ignore[attr-defined]
    _insert_sync_row(
        db_path,
        sync_id="still-failing",
        user_email="eris@x.test",
        platform="moodle",
        state="failed",
        started_at="2026-04-22T16:00:00+00:00",
        finished_at="2026-04-22T16:00:10+00:00",
        error="upstream 500",
    )

    response = test_client.get("/api/errors", params={"kind": "failed_sync"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["sync_id"] == "still-failing" for item in items)


def test_errors_feed_keeps_failed_sync_newer_than_complete(
    test_client: TestClient,
) -> None:
    """If a sync completes then fails again, the new failure is live."""
    db_path = str(test_client.app.state.api_context.config.sqlite_path)  # type: ignore[attr-defined]
    _insert_sync_row(
        db_path,
        sync_id="earlier-ok",
        user_email="eris@x.test",
        platform="teams",
        state="complete",
        started_at="2026-04-22T15:00:00+00:00",
        finished_at="2026-04-22T15:00:10+00:00",
    )
    _insert_sync_row(
        db_path,
        sync_id="newer-fail",
        user_email="eris@x.test",
        platform="teams",
        state="failed",
        started_at="2026-04-22T16:00:00+00:00",
        finished_at="2026-04-22T16:00:10+00:00",
        error="new problem",
    )

    response = test_client.get("/api/errors", params={"kind": "failed_sync"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["sync_id"] == "newer-fail" for item in items)


def test_errors_feed_complete_on_one_platform_does_not_clear_another(
    test_client: TestClient,
) -> None:
    """A Moodle success must NOT hide a Teams failure — the filter is
    scoped per (user_email, platform)."""
    db_path = str(test_client.app.state.api_context.config.sqlite_path)  # type: ignore[attr-defined]
    _insert_sync_row(
        db_path,
        sync_id="teams-fail",
        user_email="eris@x.test",
        platform="teams",
        state="failed",
        started_at="2026-04-22T15:25:00+00:00",
        finished_at="2026-04-22T15:26:00+00:00",
        error="teams went down",
    )
    _insert_sync_row(
        db_path,
        sync_id="moodle-ok",
        user_email="eris@x.test",
        platform="moodle",
        state="complete",
        started_at="2026-04-22T16:09:00+00:00",
        finished_at="2026-04-22T16:10:00+00:00",
    )

    response = test_client.get("/api/errors", params={"kind": "failed_sync"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["sync_id"] == "teams-fail" for item in items)


@pytest.mark.django_db(transaction=True)
def test_credentials_endpoint_returns_django_rows(test_client: TestClient) -> None:  # noqa: F811
    """Insert a PlatformCredential and confirm the list endpoint returns it
    with the expected summary shape."""
    from datetime import UTC, datetime

    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="bob@x.test",
        display_name="Bob",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_paused=False,
        is_expired=True,
        expired_at=datetime.now(tz=UTC),
    )
    response = test_client.get("/api/credentials")
    assert response.status_code == 200
    body = response.json()
    assert any(
        row["user_email"] == "bob@x.test" and row["platform"] == "moodle" and row["is_expired"]
        for row in body
    )


@pytest.mark.django_db(transaction=True)
def test_credentials_detail_reports_unreadable_payload(test_client: TestClient) -> None:
    """A garbage ``encrypted_payload`` should report payload_readable=False
    rather than 500-ing the detail endpoint."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="carol@x.test",
        display_name="Carol",
        platform="telegram",
        encrypted_payload=b"not-a-fernet-token",
    )
    response = test_client.get("/api/credentials/carol@x.test/telegram")
    assert response.status_code == 200
    body = response.json()
    assert body["payload_readable"] is False
    assert body["payload_keys"] == []
