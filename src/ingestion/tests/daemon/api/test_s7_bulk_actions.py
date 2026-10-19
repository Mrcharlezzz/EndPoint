"""Coverage for slice 7's bulk operator-actions endpoints (Part B).

* ``POST /api/actions/users/bulk-pause`` — XOR(platform, user_emails) selector.
* ``POST /api/actions/users/bulk-mark-expired`` — same selector shape.
* ``POST /api/config/overlay/reset-all`` — wipes every overlay key in one go.

Each endpoint emits exactly ONE bulk audit row carrying ``requested_count``
+ ``affected_count`` (or the overlay snapshot for reset-all). Per-item
failures land in ``errors[]`` rather than aborting the batch.

The bulk-pause/bulk-mark-expired tests use the in-memory ``FakeCredPort``
so the slice's service-level logic is isolated from Postgres availability.
The reset-all tests don't need the ORM at all — overlay keys go through
the local SQLite store.
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

from tests.edges._s2_helpers import FakeCredPort, build_client, make_row


@pytest.fixture
def authed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    """Default authed client. No fake cred-port; for tests that don't
    need bulk credential mutation."""
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


def _audit_rows(db_path: Path, *, action: str | None = None) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        if action is not None:
            cursor = conn.execute(
                "SELECT action, target, before_json, after_json, reason "
                "FROM audit_log WHERE action = ? ORDER BY id ASC",
                (action,),
            )
        else:
            cursor = conn.execute(
                "SELECT action, target, before_json, after_json, reason "
                "FROM audit_log ORDER BY id ASC"
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
# POST /api/actions/users/bulk-pause
# ---------------------------------------------------------------------------


def test_bulk_pause_with_platform_selector(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams"),
            make_row(user_email="b@x", platform="teams"),
            make_row(user_email="c@x", platform="moodle"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams", "reason": "incident"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "paused_count": 2,
        "skipped_count": 0,
        "errors": [],
        "requested_count": 2,
    }
    # Side-effect: matched rows flipped.
    assert fake.rows[("a@x", "teams")].is_paused is True
    assert fake.rows[("b@x", "teams")].is_paused is True
    # Untouched rows stayed.
    assert fake.rows[("c@x", "moodle")].is_paused is False


def test_bulk_pause_with_user_emails_selector(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="alice@x", platform="teams"),
            make_row(user_email="alice@x", platform="moodle"),
            make_row(user_email="bob@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": ["alice@x"], "reason": "alice-only"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["paused_count"] == 2
    assert body["skipped_count"] == 0
    # Bob untouched.
    assert fake.rows[("bob@x", "teams")].is_paused is False


def test_bulk_pause_skips_already_paused(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams", is_paused=True),
            make_row(user_email="b@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams"},
    )
    body = response.json()
    assert body["paused_count"] == 1
    assert body["skipped_count"] == 1
    assert body["requested_count"] == 2


def test_bulk_pause_empty_match_returns_zeros(tmp_path: Path, fernet_key: str) -> None:
    """Selector matched no rows -> all three counters = 0, no errors."""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "paused_count": 0,
        "skipped_count": 0,
        "errors": [],
        "requested_count": 0,
    }


def test_bulk_pause_emits_one_audit_row(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams"),
            make_row(user_email="b@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams", "reason": "drill"},
    )
    assert response.status_code == 200
    rows = _audit_rows(_db_path(client), action="user.bulk_pause")
    assert len(rows) == 1
    audit = rows[0]
    assert audit["target"] == "platform:teams"
    assert audit["before"]["selector"] == {"platform": "teams"}
    assert audit["before"]["requested_count"] == 2
    assert audit["after"]["affected_count"] == 2
    assert audit["after"]["error_count"] == 0
    assert audit["reason"] == "drill"


def test_bulk_pause_emits_one_audit_row_for_user_emails(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams"),
            make_row(user_email="b@x", platform="moodle"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": ["a@x", "b@x"], "reason": "drill"},
    )
    assert response.status_code == 200
    rows = _audit_rows(_db_path(client), action="user.bulk_pause")
    assert len(rows) == 1
    assert rows[0]["target"] == "users:2"
    assert rows[0]["before"]["selector"] == {"user_emails": ["a@x", "b@x"]}


def test_bulk_pause_no_selector_returns_422(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-pause",
        json={"reason": "no selector"},
    )
    assert response.status_code == 422


def test_bulk_pause_both_selectors_returns_422(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams", "user_emails": ["a@x"]},
    )
    assert response.status_code == 422


def test_bulk_pause_user_emails_over_cap_returns_422(authed_client: TestClient) -> None:
    """``user_emails`` is capped at 1000 entries via Field(max_length=...)."""
    response = authed_client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": [f"u{i}@x" for i in range(1001)]},
    )
    assert response.status_code == 422


def test_bulk_pause_at_cap_accepted(tmp_path: Path, fernet_key: str) -> None:
    """Exactly 1000 entries lands on the service path (which finds zero
    real credentials in this fixture). Uses the in-memory port to avoid
    a 1000-call Postgres roundtrip."""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    payload = {"user_emails": [f"u{i}@x" for i in range(1000)]}
    response = client.post("/api/actions/users/bulk-pause", json=payload)
    assert response.status_code == 200


def test_bulk_pause_empty_emails_list_treated_as_none(
    authed_client: TestClient,
) -> None:
    """Empty user_emails AND no platform -> 422 (XOR validator)."""
    response = authed_client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": []},
    )
    assert response.status_code == 422


def test_bulk_pause_extra_fields_rejected(authed_client: TestClient) -> None:
    """``extra='forbid'`` on the request body."""
    response = authed_client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams", "rogue": "field"},
    )
    assert response.status_code == 422


def test_bulk_pause_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams"},
    )
    assert response.status_code == 401


def test_bulk_pause_per_item_error_captured(tmp_path: Path, fernet_key: str) -> None:
    """A wedged set_paused() call for one row should NOT abort the batch —
    the error is captured and reported."""

    class FlakyPort(FakeCredPort):
        def __init__(self) -> None:
            super().__init__(
                seed=[
                    make_row(user_email="ok@x", platform="teams"),
                    make_row(user_email="boom@x", platform="teams"),
                ]
            )

        async def set_paused(self, email: str, platform: str, paused: bool) -> int:
            if email == "boom@x":
                raise RuntimeError("simulated DB lock")
            return await super().set_paused(email, platform, paused)

    flaky = FlakyPort()
    client = build_client(tmp_path, fernet_key, credentials=flaky)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["paused_count"] == 1
    # Both rows hit the inner loop — the boom@x failure surfaces in errors[].
    assert len(body["errors"]) == 1
    assert body["errors"][0]["user_email"] == "boom@x"
    assert body["errors"][0]["error_code"] == "RuntimeError"
    # ok@x did flip.
    assert flaky.rows[("ok@x", "teams")].is_paused is True


# ---------------------------------------------------------------------------
# POST /api/actions/users/bulk-mark-expired
# ---------------------------------------------------------------------------


def test_bulk_mark_expired_with_platform(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams"),
            make_row(user_email="b@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams", "reason": "rotation"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["paused_count"] == 2  # named for shape parity (rows flipped)
    assert body["skipped_count"] == 0
    assert fake.rows[("a@x", "teams")].is_expired is True
    assert fake.rows[("b@x", "teams")].is_expired is True


def test_bulk_mark_expired_skips_already_expired(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams", is_expired=True),
            make_row(user_email="b@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams"},
    )
    body = response.json()
    assert body["paused_count"] == 1
    assert body["skipped_count"] == 1


def test_bulk_mark_expired_emits_one_audit_row(tmp_path: Path, fernet_key: str) -> None:
    fake = FakeCredPort(
        seed=[
            make_row(user_email="a@x", platform="teams"),
            make_row(user_email="b@x", platform="teams"),
        ]
    )
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams", "reason": "drill"},
    )
    assert response.status_code == 200
    rows = _audit_rows(_db_path(client), action="user.bulk_mark_expired")
    assert len(rows) == 1
    assert rows[0]["target"] == "platform:teams"
    assert rows[0]["after"]["affected_count"] == 2


def test_bulk_mark_expired_no_selector_returns_422(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"reason": "x"},
    )
    assert response.status_code == 422


def test_bulk_mark_expired_both_selectors_returns_422(
    authed_client: TestClient,
) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams", "user_emails": ["a@x"]},
    )
    assert response.status_code == 422


def test_bulk_mark_expired_user_emails_over_cap_returns_422(
    authed_client: TestClient,
) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"user_emails": [f"u{i}@x" for i in range(1001)]},
    )
    assert response.status_code == 422


def test_bulk_mark_expired_per_item_error_captured(tmp_path: Path, fernet_key: str) -> None:
    class FlakyPort(FakeCredPort):
        def __init__(self) -> None:
            super().__init__(
                seed=[
                    make_row(user_email="ok@x", platform="teams"),
                    make_row(user_email="boom@x", platform="teams"),
                ]
            )

        async def set_expired(self, email: str, platform: str, expired: bool) -> int:
            if email == "boom@x":
                raise OSError("disk full")
            return await super().set_expired(email, platform, expired)

    flaky = FlakyPort()
    client = build_client(tmp_path, fernet_key, credentials=flaky)
    response = client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams"},
    )
    body = response.json()
    assert body["paused_count"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["error_code"] == "OSError"


def test_bulk_mark_expired_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams"},
    )
    assert response.status_code == 401


def test_bulk_mark_expired_extra_fields_rejected(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/actions/users/bulk-mark-expired",
        json={"platform": "teams", "rogue": "field"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/config/overlay/reset-all
# ---------------------------------------------------------------------------


def test_overlay_reset_all_empty_overlay_returns_empty_keys(
    authed_client: TestClient,
) -> None:
    response = authed_client.post("/api/config/overlay/reset-all", json={})
    assert response.status_code == 200
    assert response.json() == {"cleared_keys": []}


def test_overlay_reset_all_clears_existing_keys(authed_client: TestClient) -> None:
    # Seed two overlay keys.
    r1 = authed_client.put(
        "/api/config/overlay/scheduler.global_paused",
        json={"value": True},
    )
    assert r1.status_code == 200
    r2 = authed_client.put(
        "/api/config/overlay/platforms.paused.teams",
        json={"value": True},
    )
    assert r2.status_code == 200

    response = authed_client.post("/api/config/overlay/reset-all", json={})
    assert response.status_code == 200
    cleared = response.json()["cleared_keys"]
    # Returned in sorted order.
    assert cleared == sorted(cleared)
    assert set(cleared) == {"scheduler.global_paused", "platforms.paused.teams"}

    # Overlay is now empty.
    overlay = authed_client.get("/api/config/overlay").json()
    assert overlay == {}


def test_overlay_reset_all_emits_audit_with_snapshot(
    authed_client: TestClient,
) -> None:
    authed_client.put(
        "/api/config/overlay/scheduler.global_paused",
        json={"value": True},
    )
    response = authed_client.post("/api/config/overlay/reset-all", json={})
    assert response.status_code == 200
    rows = _audit_rows(_db_path(authed_client), action="config.overlay_reset_all")
    assert len(rows) == 1
    audit = rows[0]
    # Snapshot of the prior state in `before`.
    assert audit["target"] == "overlay"
    assert "scheduler.global_paused" in audit["before"]["overlay"]
    assert audit["before"]["overlay"]["scheduler.global_paused"] is True
    assert audit["after"]["key_count"] == 0


def test_overlay_reset_all_idempotent_second_call(
    authed_client: TestClient,
) -> None:
    """Second call after the overlay is empty returns ``cleared_keys=[]``
    and emits a fresh audit row (recording the no-op attempt)."""
    authed_client.put(
        "/api/config/overlay/scheduler.global_paused",
        json={"value": True},
    )
    first = authed_client.post("/api/config/overlay/reset-all", json={})
    assert first.json()["cleared_keys"]
    second = authed_client.post("/api/config/overlay/reset-all", json={})
    assert second.status_code == 200
    assert second.json()["cleared_keys"] == []
    # Two audit rows total — the no-op call still records the action
    # attempt (per docstring contract).
    rows = _audit_rows(_db_path(authed_client), action="config.overlay_reset_all")
    assert len(rows) == 2


def test_overlay_reset_all_requires_auth(unauthed_client: TestClient) -> None:
    response = unauthed_client.post("/api/config/overlay/reset-all", json={})
    assert response.status_code == 401
