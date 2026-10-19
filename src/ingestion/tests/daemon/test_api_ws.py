"""WebSocket /ws/logs: auth gate and event fan-out.

TestClient runs the app in an anyio portal thread. We publish into the
shared :class:`EventBus` via ``portal.call`` so the publish lands on the
server's running loop, which is where the subscribing queue lives.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.pubsub import EventBus
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _test_with_xrw(client: TestClient) -> TestClient:
    """Default every request to include X-Requested-With so mutation
    routes pass the API's CSRF-style header gate."""
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


@pytest.fixture
def client_and_bus(tmp_path: Path, fernet_key: str) -> tuple[TestClient, EventBus]:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-at-least-32b-long-for-hmac",
        session_ttl_seconds=3600,
    )
    from ingestion.daemon.config import DaemonConfig

    config = DaemonConfig(sqlite_path=sqlite_path)
    bus = EventBus()
    app = create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
        events=bus,
    )
    return _test_with_xrw(TestClient(app)), bus


def _publish_via_portal(ws: object, bus: EventBus, channel: str, event: dict) -> None:
    """Hop into the server loop to call ``bus.publish``. Retries briefly
    because the subscribe queue is registered lazily inside the handler."""
    portal = ws.portal  # type: ignore[attr-defined]

    for _ in range(20):
        subs = portal.call(bus.subscriber_count, channel)
        if subs > 0:
            break
        time.sleep(0.01)
    portal.call(bus.publish, channel, event)


def test_unauthenticated_ws_rejected(client_and_bus: tuple[TestClient, EventBus]) -> None:
    client, _bus = client_and_bus
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/logs"):
        pass


def test_authenticated_ws_receives_published_event(
    client_and_bus: tuple[TestClient, EventBus],
) -> None:
    client, bus = client_and_bus
    client.post("/api/auth/login", json={"password": "hunter2-strong"})

    with client.websocket_connect("/ws/logs") as ws:
        ws.send_json({})  # no filter, accept everything
        _publish_via_portal(ws, bus, "logs", {"event": "sync.started", "level": "INFO"})
        received = ws.receive_json()
        assert received["event"] == "sync.started"


def test_ws_filter_blocks_non_matching_events(
    client_and_bus: tuple[TestClient, EventBus],
) -> None:
    client, bus = client_and_bus
    client.post("/api/auth/login", json={"password": "hunter2-strong"})

    with client.websocket_connect("/ws/logs") as ws:
        ws.send_json({"level": "ERROR"})
        # Confirm the subscriber is wired before publishing.
        portal = ws.portal  # type: ignore[attr-defined]
        for _ in range(20):
            if portal.call(bus.subscriber_count, "logs") > 0:
                break
            time.sleep(0.01)
        portal.call(bus.publish, "logs", {"event": "ignore_me", "level": "INFO"})
        portal.call(bus.publish, "logs", {"event": "catch_me", "level": "ERROR"})
        received = ws.receive_json()
        assert received["event"] == "catch_me"


def test_cancel_sync_emits_ws_event(
    client_and_bus: tuple[TestClient, EventBus],
) -> None:
    """/api/actions/syncs/{id}/cancel fans out a ``sync.cancelled`` envelope
    on the ``/ws/syncs`` channel so subscribers pick it up without a
    refetch. Envelope shape mirrors ``sync.started`` / ``sync.finished`` /
    ``sync.failed``."""
    import sqlite3

    client, _bus = client_and_bus
    client.post("/api/auth/login", json={"password": "hunter2-strong"})

    sqlite_path = client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "ws-cancel-1111-2222-3333-444444444444"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sync_id, "ws@x", "teams", "2026-04-22T00:00:00+00:00", "running"),
        )
        conn.commit()
    finally:
        conn.close()

    with client.websocket_connect("/ws/syncs") as ws:
        ws.send_json({})  # no filter
        # Wait for the subscriber to register before the endpoint publishes.
        portal = ws.portal  # type: ignore[attr-defined]
        for _ in range(20):
            if portal.call(_bus.subscriber_count, "syncs") > 0:
                break
            time.sleep(0.01)
        response = client.post(
            f"/api/actions/syncs/{sync_id}/cancel",
            json={"confirm": sync_id, "reason": "live cancel"},
        )
        assert response.status_code == 200
        received = ws.receive_json()
        assert received["event"] == "sync.cancelled"
        assert received["sync_id"] == sync_id
        assert received["user_email"] == "ws@x"
        assert received["platform"] == "teams"
        assert "timestamp" in received


# silence unused-import warnings
_ = asyncio
