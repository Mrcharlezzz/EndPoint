"""REST surface: logs, audit, alerts, config, actions.

Focuses on shape/contract rather than persisted semantics — the underlying
stores are covered in their own test modules.
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
    """Default every request to include X-Requested-With so mutation
    routes pass the API's CSRF-style header gate."""
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


def test_logs_endpoint_returns_empty_list(test_client: TestClient) -> None:
    response = test_client.get("/api/logs")
    assert response.status_code == 200
    assert response.json() == []


def _seed_log(
    test_client: TestClient,
    *,
    timestamp: str,
    level: str,
    event: str,
    platform: str | None = None,
    fields_json: str | None = None,
    user_email: str | None = None,
) -> None:
    """Insert a ``logs`` row directly so filter tests can control the data."""
    import sqlite3

    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO logs (
                timestamp, level, event, user_email, platform, sync_id,
                fields_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (timestamp, level, event, user_email, platform, fields_json, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def test_logs_platform_filter_narrows_results(test_client: TestClient) -> None:
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:00+00:00",
        level="INFO",
        event="sync.start",
        platform="teams",
    )
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:01+00:00",
        level="INFO",
        event="sync.start",
        platform="moodle",
    )
    response = test_client.get("/api/logs", params={"platform": "teams"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["platform"] == "teams"


def test_logs_full_text_search_matches_event_and_context(test_client: TestClient) -> None:
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:00+00:00",
        level="INFO",
        event="token.refresh.failed",
        fields_json='{"reason":"network"}',
    )
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:01+00:00",
        level="INFO",
        event="sync.complete",
        fields_json='{"notes":"Token refreshed smoothly"}',
    )
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:02+00:00",
        level="INFO",
        event="unrelated",
        fields_json='{"foo":"bar"}',
    )

    resp = test_client.get("/api/logs", params={"q": "token"})
    assert resp.status_code == 200
    body = resp.json()
    events = {row["event"] for row in body}
    assert events == {"token.refresh.failed", "sync.complete"}


def test_logs_search_escapes_like_wildcards(test_client: TestClient) -> None:
    """A bare ``%`` must not match arbitrary substrings — it's literal text."""
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:00+00:00",
        level="INFO",
        event="cpu.usage",
        fields_json='{"msg":"99% saturated"}',
    )
    _seed_log(
        test_client,
        timestamp="2026-04-19T10:00:01+00:00",
        level="INFO",
        event="cpu.usage",
        fields_json='{"msg":"nominal"}',
    )

    resp = test_client.get("/api/logs", params={"q": "99%"})
    body = resp.json()
    assert len(body) == 1
    assert "99% saturated" in body[0]["context"]["msg"]


def test_audit_endpoint_returns_list(test_client: TestClient) -> None:
    # Login writes an audit entry, so the response should not be empty.
    response = test_client.get("/api/audit")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert any(entry["action"] == "auth.login" for entry in body)


def test_alert_rule_crud(test_client: TestClient) -> None:
    create_resp = test_client.post(
        "/api/alerts/rules",
        json={
            "name": "too-many-failures",
            "metric": "failure_count",
            "comparator": ">",
            "threshold": 5,
            "window_seconds": 300,
            "cooldown_seconds": 60,
        },
    )
    assert create_resp.status_code == 201
    rule = create_resp.json()
    assert rule["name"] == "too-many-failures"
    assert rule["enabled"] is True

    list_resp = test_client.get("/api/alerts/rules")
    assert list_resp.status_code == 200
    assert any(r["id"] == rule["id"] for r in list_resp.json())

    disable_resp = test_client.patch(
        f"/api/alerts/rules/{rule['id']}/enabled",
        json={"enabled": False},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["enabled"] is False

    delete_resp = test_client.delete(f"/api/alerts/rules/{rule['id']}")
    assert delete_resp.status_code == 200


def test_alert_firings_list_endpoint(test_client: TestClient) -> None:
    response = test_client.get("/api/alerts/firings")
    assert response.status_code == 200
    assert response.json() == []


def test_alert_rule_patch_updates_fields(test_client: TestClient) -> None:
    """PATCH /api/alerts/rules/{id} edits name, condition, and scope together."""
    create_resp = test_client.post(
        "/api/alerts/rules",
        json={
            "name": "old-name",
            "metric": "failure_count",
            "comparator": ">",
            "threshold": 5,
            "cooldown_seconds": 60,
        },
    )
    assert create_resp.status_code == 201
    rule_id = create_resp.json()["id"]

    patch_resp = test_client.patch(
        f"/api/alerts/rules/{rule_id}",
        json={
            "name": "new-name",
            "threshold": 10,
            "comparator": ">=",
            "platform": "teams",
        },
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["name"] == "new-name"
    assert body["threshold"] == 10.0
    assert body["comparator"] == ">="
    assert body["platform"] == "teams"
    # enabled must not be flipped by this endpoint.
    assert body["enabled"] is True


def test_alert_rule_patch_404_for_unknown(test_client: TestClient) -> None:
    resp = test_client.patch("/api/alerts/rules/99999", json={"name": "x"})
    assert resp.status_code == 404


def test_alert_firing_detail_returns_full_payload(test_client: TestClient) -> None:
    """GET /api/alerts/firings/{id} returns metric value, users, and raw payload."""
    import json
    import sqlite3

    # Seed a rule + firing directly so the test doesn't need to exercise the
    # engine tick code path.
    create_resp = test_client.post(
        "/api/alerts/rules",
        json={
            "name": "my-rule",
            "metric": "failure_count",
            "comparator": ">",
            "threshold": 5,
        },
    )
    rule_id = create_resp.json()["id"]

    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    payload = {"affected_users": ["bob@x.test", "carol@x.test"], "notes": "cpu spike"}
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO alert_firings (
                rule_id, fired_at, metric_value, user_email, platform,
                state, resolved_at, trigger_context_json, cooldown_bucket
            )
            VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, NULL)
            """,
            (
                rule_id,
                "2026-04-19T12:00:00+00:00",
                9.5,
                "alice@x.test",
                "teams",
                json.dumps(payload),
            ),
        )
        conn.commit()
        firing_id = cur.lastrowid
    finally:
        conn.close()

    resp = test_client.get(f"/api/alerts/firings/{firing_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == firing_id
    assert body["rule_id"] == rule_id
    assert body["rule_name"] == "my-rule"
    assert body["triggering_metric_value"] == 9.5
    # alice comes from the firing row, bob+carol from the trigger payload.
    assert body["affected_users"] == ["alice@x.test", "bob@x.test", "carol@x.test"]
    assert body["raw_event_payload"] == payload
    assert body["state"] == "active"


def test_alert_firing_detail_404_for_unknown(test_client: TestClient) -> None:
    resp = test_client.get("/api/alerts/firings/99999")
    assert resp.status_code == 404


def test_alert_rule_patch_rejects_enabled_field(test_client: TestClient) -> None:
    """Sending ``enabled`` should be a 422 — the dedicated toggle owns that."""
    create_resp = test_client.post(
        "/api/alerts/rules",
        json={
            "name": "r",
            "metric": "failure_count",
            "comparator": ">",
            "threshold": 1,
        },
    )
    rule_id = create_resp.json()["id"]
    resp = test_client.patch(
        f"/api/alerts/rules/{rule_id}",
        json={"enabled": False},
    )
    assert resp.status_code == 422


def test_config_endpoint_returns_effective_config(test_client: TestClient) -> None:
    response = test_client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert "scheduler" in body
    assert "platforms" in body


def test_config_overlay_set_rejects_unknown_key(test_client: TestClient) -> None:
    response = test_client.put(
        "/api/config/overlay/not.a.real.key",
        json={"value": True},
    )
    assert response.status_code == 400


def test_config_overlay_set_and_delete_cycle(test_client: TestClient) -> None:
    set_resp = test_client.put(
        "/api/config/overlay/scheduler.drain_mode",
        json={"value": True},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["value"] is True

    list_resp = test_client.get("/api/config/overlay")
    assert list_resp.json()["scheduler.drain_mode"] is True

    delete_resp = test_client.delete("/api/config/overlay/scheduler.drain_mode")
    assert delete_resp.status_code == 200

    list_after = test_client.get("/api/config/overlay")
    assert "scheduler.drain_mode" not in list_after.json()


def test_drain_action_writes_audit_entry(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/scheduler/drain",
        json={"enabled": True, "reason": "deploy prep"},
    )
    assert response.status_code == 200
    assert response.json()["drain_mode"] is True

    audit = test_client.get("/api/audit", params={"action": "scheduler.drain_set"}).json()
    assert len(audit) >= 1
    assert audit[0]["reason"] == "deploy prep"


def test_clear_dead_letter_writes_audit(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/dead_letter/alice@x/moodle/clear",
        json={"reason": "manual retry"},
    )
    assert response.status_code == 200

    audit = test_client.get("/api/audit", params={"action": "dead_letter.clear"}).json()
    assert len(audit) == 1
    assert audit[0]["target"] == "alice@x/moodle"
