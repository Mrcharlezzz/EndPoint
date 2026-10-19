from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from django.utils import timezone
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _test_with_xrw(client: TestClient) -> TestClient:
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
    from ingestion.daemon.config import DaemonConfig

    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = _test_with_xrw(TestClient(app))
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


@pytest.mark.django_db(transaction=True)
def test_processor_summary_aggregates_health_and_llm_usage(
    authed_client: TestClient,
) -> None:
    from core.models import ClassificationLog, IngestedItem

    now = timezone.now()
    processed = IngestedItem.objects.create(
        external_id="teams_msg_processed",
        platform="teams",
        fetched_by_user_email="student@example.edu",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="processed",
        processing_completed_at=now,
    )
    unclassified = IngestedItem.objects.create(
        external_id="teams_msg_unclassified",
        platform="teams",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="processed",
        processing_completed_at=now,
    )
    IngestedItem.objects.create(
        external_id="teams_msg_failed",
        platform="teams",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="failed",
        error_message="Classification call failed: timeout",
        processing_completed_at=now,
    )
    IngestedItem.objects.create(
        external_id="teams_msg_pending",
        platform="teams",
        payload={"content_type": "assignment"},
        raw_payload={},
        processing_status="pending",
    )
    ClassificationLog.objects.create(
        ingested_item=processed,
        entity_type="assignment",
        classification_method="llm",
        input_tokens=100,
        output_tokens=20,
    )
    ClassificationLog.objects.create(
        ingested_item=unclassified,
        entity_type="raw_message",
        classification_method="llm",
        input_tokens=40,
        output_tokens=9,
    )

    response = authed_client.get("/api/processor/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["health"]["processed_count"] == 2
    assert body["health"]["failed_count"] == 1
    assert body["health"]["pending_queue"] == 1
    assert body["health"]["last_processed_at"] is not None
    assert body["llm"]["calls"] == 2
    assert body["llm"]["input_tokens"] == 140
    assert body["llm"]["output_tokens"] == 29
    assert body["llm"]["failed_count"] == 1
    assert body["llm"]["unclassified_count"] == 1


@pytest.mark.django_db(transaction=True)
def test_processor_errors_returns_failed_items_in_window(
    authed_client: TestClient,
) -> None:
    from core.models import IngestedItem

    failed = IngestedItem.objects.create(
        external_id="teams_msg_failed",
        platform="teams",
        fetched_by_user_email="student@example.edu",
        course_external_id="course-123",
        payload={"content_type": "raw_message"},
        raw_payload={},
        processing_status="failed",
        error_message="Classification call failed: timeout",
        processing_completed_at=timezone.now(),
    )

    response = authed_client.get("/api/processor/errors")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ingested_item_id"] == str(failed.id)
    assert item["platform"] == "teams"
    assert item["fetched_by_user_email"] == "student@example.edu"
    assert item["course_external_id"] == "course-123"
    assert item["content_type"] == "raw_message"
    assert item["error_message"] == "Classification call failed: timeout"


def test_reset_processor_window_persists_new_baseline_and_audits(
    authed_client: TestClient,
) -> None:
    response = authed_client.post(
        "/api/actions/processor/window/reset",
        json={"reason": "start fresh"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["baseline_at"] is not None
    assert body["previous_baseline_at"] is None

    conn = sqlite3.connect(str(_db_path(authed_client)))
    try:
        row = conn.execute(
            "SELECT baseline_at FROM processor_dashboard_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == body["baseline_at"]

    audit = authed_client.get(
        "/api/audit",
        params={"action": "processor.window_reset"},
    )
    assert audit.status_code == 200
    assert any(entry["action"] == "processor.window_reset" for entry in audit.json())
