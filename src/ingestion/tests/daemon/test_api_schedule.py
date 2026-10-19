"""Coverage for GET /api/schedule/next."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
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
def test_client(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS", "600")
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    store.set_overlay("scheduler.default_interval_seconds", 600)
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


def _insert_complete_sync(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    finished_at: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_history (
                id, user_email, platform, started_at, finished_at, state
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"sid-{user_email}-{platform}-{finished_at}",
                user_email,
                platform,
                finished_at,
                finished_at,
                "complete",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_user_override(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    interval_seconds: int,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO user_schedule_overrides (
                user_email, platform, scope, interval_seconds, updated_at
            ) VALUES (?, ?, 'default', ?, datetime('now'))
            """,
            (user_email, platform, interval_seconds),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_suppression(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    suppressed_until: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO dead_letter_state (
                user_email, platform, consecutive_failures,
                last_failure_at, suppressed_until
            ) VALUES (?, ?, 3, ?, ?)
            """,
            (user_email, platform, suppressed_until, suppressed_until),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.django_db(transaction=True)
def test_schedule_next_returns_item_per_credential(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="moodle",
        encrypted_payload=b"\x00",
    )

    response = test_client.get("/api/schedule/next")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    emails = {item["user_email"] for item in items}
    platforms = {item["platform"] for item in items}
    assert emails == {"alice@x.test"}
    assert platforms == {"teams", "moodle"}


@pytest.mark.django_db(transaction=True)
def test_schedule_next_due_at_is_last_success_plus_interval(
    test_client: TestClient,
) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    _insert_complete_sync(
        _db_path(test_client),
        user_email="alice@x.test",
        platform="teams",
        finished_at=base.isoformat(),
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"] if i["platform"] == "teams")
    # Default interval is 600s per config.SchedulerConfig.
    assert item["interval_seconds"] == 600
    assert item["interval_source"] == "default"
    expected = (base + timedelta(seconds=600)).isoformat()
    assert item["next_due_at"] == expected
    assert item["last_success_at"] == base.isoformat()
    assert item["blocked_by"] is None


@pytest.mark.django_db(transaction=True)
def test_schedule_next_uses_user_override_interval(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="bob@x.test",
        display_name="Bob",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    _insert_user_override(
        _db_path(test_client),
        user_email="bob@x.test",
        platform="teams",
        interval_seconds=120,
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"] if i["user_email"] == "bob@x.test")
    assert item["interval_seconds"] == 120
    assert item["interval_source"] == "user_override"


@pytest.mark.django_db(transaction=True)
def test_schedule_next_reports_expired_as_blocked(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="stale@x.test",
        display_name="Stale",
        platform="teams",
        encrypted_payload=b"\x00",
        is_expired=True,
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"])
    assert item["blocked_by"] == "expired"


@pytest.mark.django_db(transaction=True)
def test_schedule_next_reports_paused_credential(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="paused@x.test",
        display_name="Paused",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_paused=True,
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"])
    assert item["blocked_by"] == "paused"


@pytest.mark.django_db(transaction=True)
def test_schedule_next_reports_suppressed_pair(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="dl@x.test",
        display_name="DL",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    _insert_suppression(
        _db_path(test_client),
        user_email="dl@x.test",
        platform="teams",
        suppressed_until=future,
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"])
    assert item["blocked_by"] == "suppressed"


@pytest.mark.django_db(transaction=True)
def test_schedule_next_reports_platform_paused_via_overlay(
    test_client: TestClient,
) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="alice@x.test",
        display_name="Alice",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    # Flip the overlay.
    overlay_response = test_client.put(
        "/api/config/overlay/platforms.paused.teams",
        json={"value": True},
    )
    assert overlay_response.status_code == 200

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"] if i["platform"] == "teams")
    assert item["blocked_by"] == "platform_paused"


@pytest.mark.django_db(transaction=True)
def test_schedule_next_flags_running_pair(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="busy@x.test",
        display_name="Busy",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    # Insert a running (unfinished) sync_history row directly.
    conn = sqlite3.connect(_db_path(test_client))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, 'running')
            """,
            ("running-sid", "busy@x.test", "teams", "2026-04-22T12:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"] if i["user_email"] == "busy@x.test")
    assert item["running"] is True
    # Still reports blocked_by=null — running is orthogonal to the guard stack.
    assert item["blocked_by"] is None


@pytest.mark.django_db(transaction=True)
def test_schedule_next_running_false_for_idle_credential(
    test_client: TestClient,
) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="idle@x.test",
        display_name="Idle",
        platform="moodle",
        encrypted_payload=b"\x00",
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"])
    assert item["running"] is False


@pytest.mark.django_db(transaction=True)
def test_schedule_next_never_run_uses_now_as_next_due(test_client: TestClient) -> None:
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="newbie@x.test",
        display_name="Newbie",
        platform="moodle",
        encrypted_payload=b"\x00",
    )

    response = test_client.get("/api/schedule/next")
    item = next(i for i in response.json()["items"])
    assert item["last_success_at"] is None
    # next_due_at is "now" — parseable as ISO, close to current time.
    parsed = datetime.fromisoformat(item["next_due_at"])
    delta = abs((datetime.now(tz=UTC) - parsed).total_seconds())
    assert delta < 5.0
