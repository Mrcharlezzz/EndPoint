"""POST /api/actions/sync/dry_run: one-shot sync with writes suppressed.

Exercises the endpoint's three invariants:

    1. The response shape matches the contract (items_fetched,
       items_would_write, items_would_skip, errors).
    2. No ``sync_history``, ``sync_cursors``, or ``ingested_items`` rows
       are written during the invocation — the dry-run leaves the real
       state machine untouched.
    3. The live ``ops.shadow_mode`` overlay is not mutated — the forced
       shadow inside the dry-run is request-scoped.
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
from ingestion.daemon.services import sync_service as sync_service_mod
from ingestion.daemon.store.local import open_store
from ingestion.schemas import FetchResult, NormalizedItem, SyncStats


def _test_with_xrw(client: TestClient) -> TestClient:
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


class _StubTokenStore:
    """In-memory stand-in for PostgresTokenStore. The dry-run asks for
    credentials; we return a fixed dict so the pipeline can run without
    Postgres. ``mark_expired`` is never called because token_store=None
    inside the handler — but the Protocol requires the method."""

    def __init__(self, creds: dict[str, object] | None) -> None:
        self._creds = creds

    async def get_credentials(self, _user_id: str, _platform: str) -> dict[str, object] | None:
        return self._creds

    async def update_credentials(
        self, _user_id: str, _platform: str, _credentials: dict[str, object]
    ) -> None:
        return

    async def mark_expired(self, _user_id: str, _platform: str) -> None:
        return


class _StubAdapter:
    """Return a fixed FetchResult so the pipeline's counts are predictable."""

    def __init__(self, platform: str, items: list[NormalizedItem]) -> None:
        self.platform = platform
        self._items = items

    async def fetch(
        self,
        _credentials: dict[str, object],
        _cursor: dict[str, object] | None = None,
        _scope: object = None,
    ) -> FetchResult:
        return FetchResult(items=list(self._items), cursor={"ts": "v1"}, stats=SyncStats())

    async def test_connection(self, _credentials: dict[str, object]) -> bool:
        return True


@pytest.fixture
def test_client(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)

    # Patch the handler's PostgresTokenStore and _build_adapter so the test
    # never touches Postgres. The handler imports them lazily via
    # ``from ingestion.daemon.ports.token_store import PostgresTokenStore``;
    # monkeypatching the module attribute covers that import path.
    from ingestion.daemon.ports import content_sink as content_sink_mod
    from ingestion.daemon.ports import token_store as token_store_mod

    monkeypatch.setattr(
        token_store_mod,
        "PostgresTokenStore",
        lambda *_a, **_kw: _StubTokenStore({"token": "stub"}),
    )

    items = [
        NormalizedItem(  # type: ignore[arg-type]
            external_id=f"moodle_forum_{i}",
            platform="moodle",
            content_type="announcement",
            title=f"item {i}",
            body="body",
        )
        for i in range(3)
    ]
    monkeypatch.setattr(
        sync_service_mod, "_build_adapter", lambda _p: _StubAdapter("moodle", items)
    )

    # Mirror PostgresContentSink's shadow-mode behaviour without touching
    # Postgres. Under shadow the sink skips writes and returns sentinels;
    # ``exists`` never hits the ORM in this stub so the real run_sync
    # pipeline has something safe to await. The test uses this via a
    # monkeypatch of PostgresContentSink because importing Django-backed
    # ORM calls for a unit test is both slow and requires a live DB.
    class _StubSink:
        def __init__(self, *, is_shadow_mode: Any = None) -> None:
            self._is_shadow_mode = is_shadow_mode or (lambda: False)

        async def exists(self, _platform: str, _external_id: str) -> bool:
            return False

        async def exists_batch(self, _platform: str, _external_ids: list[str]) -> set[str]:
            # Dry-run has no pre-existing rows; the stub reports nothing
            # exists so every fetched item counts as new in the report.
            return set()

        async def upsert(self, item: NormalizedItem) -> str:
            if self._is_shadow_mode():
                return f"shadow:{item.external_id}"
            raise AssertionError("dry-run must force shadow mode on")

        async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
            if self._is_shadow_mode():
                return [f"shadow:{it.external_id}" for it in items]
            raise AssertionError("dry-run must force shadow mode on")

    def _fake_sink(*_a: object, is_shadow_mode: Any = None, **_kw: object) -> _StubSink:
        return _StubSink(is_shadow_mode=is_shadow_mode)

    monkeypatch.setattr(content_sink_mod, "PostgresContentSink", _fake_sink)

    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = _test_with_xrw(TestClient(app))
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def test_dry_run_returns_counts_report(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # 3 items fetched, all read as new (exists returns False), so
    # items_would_write == 3.
    assert body["items_fetched"] == 3
    assert body["items_would_write"] == 3
    assert body["items_would_skip"] == 0
    assert body["errors"] == []


def test_dry_run_does_not_persist_sync_history(test_client: TestClient) -> None:
    """The in-memory sync-logger fake must not leak a sync_history row."""
    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]

    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200

    conn = sqlite3.connect(str(db_path))
    try:
        # sync_history should only carry the audit row-related rows from
        # prior test setup — the dry-run itself must not add to it.
        count = conn.execute(
            "SELECT COUNT(*) FROM sync_history WHERE user_email = ? AND platform = ?",
            ("alice@x", "moodle"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_dry_run_does_not_advance_cursor(test_client: TestClient) -> None:
    """``sync_cursors`` stays empty because the in-memory read-through
    state swallows writes — a subsequent real sync would still start from
    the pre-dry-run cursor."""
    db_path = test_client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]

    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM sync_cursors WHERE user_email = ? AND platform = ?",
            ("alice@x", "moodle"),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_dry_run_does_not_mutate_shadow_overlay(test_client: TestClient) -> None:
    """The overlay's ``ops.shadow_mode`` must stay at whatever the operator
    left it — the forced shadow inside the dry-run is invocation-scoped."""
    overlay_before = test_client.get("/api/config/overlay").json()
    assert "ops.shadow_mode" not in overlay_before

    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200

    overlay_after = test_client.get("/api/config/overlay").json()
    # The key must still not be present — dry-run did not set it.
    assert "ops.shadow_mode" not in overlay_after


def test_dry_run_preserves_existing_shadow_overlay(test_client: TestClient) -> None:
    """If the overlay already holds ``ops.shadow_mode``, dry-run leaves
    the value untouched."""
    test_client.put(
        "/api/config/overlay/ops.shadow_mode",
        json={"value": True},
    )
    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200
    overlay = test_client.get("/api/config/overlay").json()
    assert overlay["ops.shadow_mode"] is True


def test_dry_run_writes_audit_row(test_client: TestClient) -> None:
    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "moodle"},
    )
    assert response.status_code == 200
    audit = test_client.get("/api/audit", params={"action": "sync.dry_run"}).json()
    assert len(audit) >= 1
    entry = audit[0]
    assert entry["target"] == "alice@x/moodle"


def test_dry_run_404_when_no_credentials(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credentials => 404, no side-effects."""
    from ingestion.daemon.ports import token_store as token_store_mod

    monkeypatch.setattr(
        token_store_mod,
        "PostgresTokenStore",
        lambda *_a, **_kw: _StubTokenStore(None),
    )
    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "nobody@x", "platform": "moodle"},
    )
    assert response.status_code == 404


def test_dry_run_rejects_unknown_platform(test_client: TestClient) -> None:
    """Pydantic pattern validation rejects anything outside the three
    known platforms with a 422 so the handler never touches the adapter
    factory with a typo."""
    response = test_client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "reddit"},
    )
    assert response.status_code == 422
