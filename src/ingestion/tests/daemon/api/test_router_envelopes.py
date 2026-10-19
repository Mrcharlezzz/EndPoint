"""Pin the ErrorEnvelope shape across every public router endpoint.

For each router file we either trigger a 404 / 409 / 422 / 401 or
assert the existing response shape mirrors the canonical envelope:

    {"detail": {"code": "<known>", "message": "<text>", ...}}

Where ``code`` MUST be one of the values in :class:`ErrorCode`,
``message`` MUST be non-empty, and when an inbound ``X-Request-ID`` is
supplied the envelope MUST carry it back under ``detail.request_id``.

Also pins the structural invariant from slice 4 part B: every
``APIRoute`` instance declares an explicit ``response_model``. The three
exceptions are documented and pre-existing (Prometheus exposition,
NDJSON streaming, 204 no-body delete); they're whitelisted with a
clear comment so the next person who adds an untyped route knows it
needs to land on this list.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.error_envelope import ErrorCode
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_KNOWN_CODES = {member.value for member in ErrorCode}


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def authed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    """Authenticated TestClient. Uses a fresh tmp SQLite + the in-process
    create_app factory; auth login leaves the cookie attached so every
    follow-up request is admin-authenticated.

    Swaps in an in-memory ``FakeCredPort`` so the credential-routing
    paths don't reach Postgres. Tests that need a real Postgres-backed
    flow are tagged ``django_db`` and live elsewhere; this file is the
    pure-shape pin for the envelope contract."""
    from tests.edges._s2_helpers import FakeCredPort

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
    # Replace the default Django-ORM-backed credential port with the
    # in-memory fake. Empty seed list -> every credential lookup misses,
    # which is what triggers the 404 envelope path we exercise here.
    from dataclasses import replace as dc_replace

    app.state.api_context = dc_replace(app.state.api_context, credentials=FakeCredPort())
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = client.post("/api/auth/login", json={"password": "hunter2-strong"})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def unauthed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    """TestClient with NO login performed — for testing the 401/403
    paths on unauthenticated endpoints."""
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


def _assert_envelope_shape(detail: Any, *, request_id: str | None = None) -> None:
    """Common envelope-shape contract: detail is a dict with a known
    code, non-empty message, optional hint, optional request_id."""
    assert isinstance(detail, dict), f"detail not a dict: {detail!r}"
    assert detail["code"] in _KNOWN_CODES, f"unknown code {detail['code']!r}"
    assert isinstance(detail["message"], str)
    assert len(detail["message"]) > 0
    if request_id is not None:
        assert detail.get("request_id") == request_id, (
            f"request_id mismatch: expected {request_id!r}, got {detail.get('request_id')!r}"
        )


# ---------------------------------------------------------------------------
# Per-router 404 / 409 / 422 sweeps
# ---------------------------------------------------------------------------


def test_actions_pause_404_envelope_for_missing_user(authed_client: TestClient) -> None:
    """``actions_pause``: pause non-existent user -> 404 envelope.
    ServiceError translation through the central translator."""
    response = authed_client.post(
        "/api/actions/users/ghost@x/pause",
        json={"reason": "x"},
    )
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_actions_sync_404_envelope_for_missing_sync(authed_client: TestClient) -> None:
    """``actions_sync``: cancel non-existent sync -> 404 envelope."""
    response = authed_client.post(
        "/api/actions/syncs/missing-id/cancel",
        json={"confirm": "missing-id", "reason": "x"},
    )
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_actions_sync_422_envelope_on_confirm_mismatch(authed_client: TestClient) -> None:
    """``actions_sync``: confirm-mismatch -> 422 envelope from
    ``raise_http_exception`` (NOT FastAPI's pydantic 422)."""
    # Insert a real sync row so we hit the confirm check, not the 404.
    sqlite_path = authed_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "11111111-2222-3333-4444-555555555555"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (sync_id, "alice@x", "teams", "2026-04-26T00:00:00+00:00", "running"),
        )
        conn.commit()
    finally:
        conn.close()

    response = authed_client.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": "wrong-id", "reason": "typo"},
    )
    assert response.status_code == 422
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "validation_error"


def test_actions_sync_emergency_stop_422_envelope(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/actions/scheduler/emergency_stop",
        json={"confirm": "wrong", "reason": "panic"},
    )
    assert response.status_code == 422
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "validation_error"


def test_actions_maintenance_purge_422_envelope_on_confirm_mismatch(
    authed_client: TestClient,
) -> None:
    """``actions_maintenance``: purge with bad confirm -> 422 envelope."""
    item_id = "00000000-0000-0000-0000-000000000000"
    response = authed_client.post(
        f"/api/actions/content/{item_id}/purge",
        json={"confirm": "wrong", "reason": "x"},
    )
    assert response.status_code == 422
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "validation_error"


def test_actions_maintenance_alert_rule_404_envelope(authed_client: TestClient) -> None:
    """``actions_maintenance``: mute on missing rule_id -> 404 envelope."""
    response = authed_client.patch(
        "/api/actions/alerts/rules/9999/mute",
        json={"reason": "x"},
    )
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_alerts_404_envelope_on_missing_rule(authed_client: TestClient) -> None:
    """``alerts_router``: PATCH non-existent rule -> 404 envelope."""
    response = authed_client.patch(
        "/api/alerts/rules/9999/enabled",
        json={"enabled": False},
    )
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_alerts_firing_404_envelope(authed_client: TestClient) -> None:
    """``alerts_router``: GET non-existent firing -> 404 envelope."""
    response = authed_client.get("/api/alerts/firings/9999")
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_config_overlay_400_envelope_for_unknown_key(authed_client: TestClient) -> None:
    """``config_router``: PUT unknown key -> 400 envelope.
    Validation error via ``raise_http_exception``."""
    response = authed_client.put(
        "/api/config/overlay/this.is.not.a.real.key",
        json={"value": 1},
    )
    assert response.status_code == 400
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "validation_error"


def test_config_overlay_404_envelope_for_unset_key(authed_client: TestClient) -> None:
    """``config_router``: DELETE unset key -> 404 envelope."""
    response = authed_client.delete("/api/config/overlay/scheduler.global_paused")
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_config_schedule_overrides_404_envelope(authed_client: TestClient) -> None:
    """``config_router``: GET overrides for unknown email -> 404 envelope."""
    response = authed_client.get("/api/config/overrides/ghost@x")
    assert response.status_code == 404
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "not_found"


def test_config_schedule_overrides_422_envelope_on_zero_interval(
    authed_client: TestClient,
) -> None:
    """``config_router``: PUT with interval=0 trips Pydantic's built-in
    422. Post fix #4 the body is the unified envelope shape (NOT
    FastAPI's default list of error dicts) — the
    ``RequestValidationError`` exception handler translates the list
    into ``code: validation_error`` + a single message.
    """
    response = authed_client.put(
        "/api/config/overrides/alice@x",
        json={"platform": "teams", "scope": "default", "interval_seconds": 0},
    )
    assert response.status_code == 422
    body = response.json()
    # Envelope shape: detail is a dict, not a list.
    _assert_envelope_shape(body["detail"])
    assert body["detail"]["code"] == "validation_error"


def test_ingest_full_resync_422_envelope_on_confirm_mismatch(
    authed_client: TestClient,
) -> None:
    """``ingest``: full_resync with bad confirm -> 422 envelope."""
    response = authed_client.post(
        "/api/ingest/full_resync",
        json={"confirm": "wrong", "reason": "x", "platforms": ["teams"]},
    )
    assert response.status_code == 422
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "validation_error"


def test_auth_router_401_envelope_for_bad_password(unauthed_client: TestClient) -> None:
    """``auth_router``: login with wrong password -> 401 envelope.
    Goes through ``raise_http_exception`` so the body matches the shape."""
    response = unauthed_client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401
    _assert_envelope_shape(response.json()["detail"])
    assert response.json()["detail"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# request_id propagation through the envelope
# ---------------------------------------------------------------------------


def test_inbound_request_id_propagates_to_envelope(authed_client: TestClient) -> None:
    """Inbound ``X-Request-ID`` (32-char hex) survives middleware and
    lands in the envelope's ``request_id`` field. Pin via a 404 trigger
    on a router that uses ``raise_http_exception`` directly (alerts).
    """
    rid = "deadbeef" * 4  # 32-char hex, passes inbound validation
    response = authed_client.patch(
        "/api/alerts/rules/9999/enabled",
        json={"enabled": False},
        headers={"X-Request-ID": rid},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    _assert_envelope_shape(detail, request_id=rid)
    # Request-ID middleware also echoes on response header.
    assert response.headers.get("x-request-id") == rid


def test_inbound_request_id_propagates_via_translator_path(
    authed_client: TestClient,
) -> None:
    """Same propagation, but via the ``service_error_to_http`` path
    (404 from a service-layer ``UserNotFoundError``)."""
    rid = "1234abcd" * 4
    response = authed_client.post(
        "/api/actions/users/ghost@x/pause",
        json={"reason": "x"},
        headers={"X-Request-ID": rid},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    _assert_envelope_shape(detail, request_id=rid)
    assert response.headers.get("x-request-id") == rid


# ---------------------------------------------------------------------------
# Structural invariant: every APIRoute declares an explicit response_model
# ---------------------------------------------------------------------------


# Documented exceptions to the response_model invariant. Each entry is
# whitelisted because the route's response is intentionally not a typed
# Pydantic model — Prometheus text exposition, NDJSON streaming, or
# HTTP 204 No Content. Adding a new untyped route should require a code
# review touching THIS list, not the enforcement test.
_RESPONSE_MODEL_EXCEPTIONS: set[tuple[str, frozenset[str]]] = {
    # /metrics returns Prometheus text exposition format, not JSON.
    # Slice 5: each route is mounted twice (canonical /v1/ + legacy /api/).
    ("/v1/metrics", frozenset({"GET"})),
    ("/api/metrics", frozenset({"GET"})),
    # actions/audit/export streams NDJSON (StreamingResponse).
    ("/v1/actions/audit/export", frozenset({"GET"})),
    ("/api/actions/audit/export", frozenset({"GET"})),
    # DELETE config/overrides/{email} returns 204 No Content (no body).
    ("/v1/config/overrides/{email}", frozenset({"DELETE"})),
    ("/api/config/overrides/{email}", frozenset({"DELETE"})),
}


def test_every_api_route_declares_response_model(authed_client: TestClient) -> None:
    """Every ``APIRoute`` registered on the app declares a
    ``response_model``, except for the documented exceptions
    (text exposition, streaming, 204 no content).

    Catches a regression where someone adds a new dict-returning route
    and forgets to pin the wire shape — the OpenAPI doc would show
    ``Any`` and the dashboard's TypeScript codec would break silently.
    """
    app = authed_client.app
    apiroutes = [r for r in app.routes if isinstance(r, APIRoute)]
    untyped: list[tuple[str, frozenset[str]]] = []
    for route in apiroutes:
        methods = frozenset(route.methods or ())
        if route.response_model is None:
            untyped.append((route.path, methods))
    # Filter out the documented exceptions; anything left is a bug.
    unexpected = [
        (path, methods)
        for path, methods in untyped
        if (path, methods) not in _RESPONSE_MODEL_EXCEPTIONS
    ]
    assert unexpected == [], (
        "Routes missing response_model not in whitelist: "
        f"{unexpected}. Either add a Pydantic response_model or extend "
        "_RESPONSE_MODEL_EXCEPTIONS in this test with a justification."
    )


def test_response_model_count_proportion_above_ninety_percent(
    authed_client: TestClient,
) -> None:
    """At least 90% of APIRoute instances declare a ``response_model``.
    Soft floor that catches a wide regression (e.g. removing typing
    from many routes); the hard test above already pins individual
    routes."""
    app = authed_client.app
    apiroutes = [r for r in app.routes if isinstance(r, APIRoute)]
    total = len(apiroutes)
    typed = sum(1 for r in apiroutes if r.response_model is not None)
    proportion = typed / total
    assert proportion >= 0.9, (
        f"only {typed}/{total} APIRoutes have response_model ({proportion:.1%}); expected >=90%"
    )


# ---------------------------------------------------------------------------
# Prove the envelope JSON serialises through FastAPI's pipeline
# ---------------------------------------------------------------------------


def test_envelope_serialises_as_strict_json_with_known_keys(
    authed_client: TestClient,
) -> None:
    """End-to-end: trigger an envelope-emitting endpoint and parse the
    raw response body. The wire shape is exactly:

        {"detail": {"code": str, "message": str, "request_id"?: str, "hint"?: str}}

    No extras allowed; no surprise ``null`` values for absent optionals."""
    response = authed_client.patch("/api/alerts/rules/9999/enabled", json={"enabled": False})
    assert response.status_code == 404
    body = json.loads(response.content)
    assert set(body) == {"detail"}
    detail = body["detail"]
    allowed = {"code", "message", "hint", "request_id"}
    assert set(detail).issubset(allowed)
    assert "code" in detail and "message" in detail
    # exclude_none=True drops absent optionals — they must NOT be present
    # as ``null`` keys.
    if "hint" in detail:
        assert detail["hint"] is not None
    if "request_id" in detail:
        assert detail["request_id"] is not None
