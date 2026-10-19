"""Affected-components coverage for ``GET /api/actions/audit/export``.

Targets the ``export_audit`` route refactored in commit 8f76319 plus the
new ``_AUDIT_EXPORT_FILTERS`` allowlist constant. Each branch of the
WHERE-builder is exercised, the streaming envelope is asserted, and the
emitted NDJSON is decoded line-by-line so future regressions in the
serialised shape are caught here.

Tests are pure-SQLite — no Django bootstrap needed — so they stay fast.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.routers.actions_audit import _AUDIT_EXPORT_FILTERS
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store

_EXPECTED_KEYS = {
    "id",
    "timestamp",
    "actor",
    "action",
    "target",
    "before",
    "after",
    "reason",
}


# ---------------------------------------------------------------------------
# fixtures (local — kept self-contained per task constraints)
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    """Local Fernet key fixture so the test file is self-contained.

    The shared ``daemon/conftest.py`` exposes the same name; pytest's
    closest-match resolution picks this one when running this file in
    isolation.
    """
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture
def test_client(tmp_path: Path, fernet_key: str) -> TestClient:
    """Wire up an in-process daemon API with a fresh SQLite db.

    Mirrors the fixture in ``test_api_actions.py`` but kept local so this
    file does not depend on the sibling test module's fixture surface.
    """
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
    )
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined,no-any-return]


def _wipe_audit(client: TestClient) -> None:
    """Delete every audit_log row so we can assert exact counts.

    The ``test_client`` fixture's login writes one ``auth.login`` row;
    most filter-exact tests need a known-empty starting state.
    """
    conn = sqlite3.connect(str(_db_path(client)))
    try:
        conn.execute("DELETE FROM audit_log")
        conn.commit()
    finally:
        conn.close()


def _insert_row(
    client: TestClient,
    *,
    timestamp: str = "2026-04-26T00:00:00+00:00",
    actor: str | None = "admin",
    action: str = "user.pause",
    target: str | None = "alice@x",
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> int:
    conn = sqlite3.connect(str(_db_path(client)))
    try:
        cur = conn.execute(
            """
            INSERT INTO audit_log
                (timestamp, actor, action, target, before_json, after_json, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                actor,
                action,
                target,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
                reason,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def _decode_lines(body: str) -> list[dict[str, Any]]:
    """Split the NDJSON body into parsed dicts. Drops only the trailing
    empty string from the final newline, never from the middle."""
    raw_lines = body.split("\n")
    # The streaming response always emits ``\n`` after every row, so the
    # final element in ``raw_lines`` is "" when there is at least one row,
    # and the body itself is "" when there are zero rows. Both cases are
    # handled by filtering empties — never drop legitimate content.
    return [json.loads(line) for line in raw_lines if line]


# ---------------------------------------------------------------------------
# allowlist constant — pin the public contract
# ---------------------------------------------------------------------------


def test_audit_export_filters_constant_shape() -> None:
    """The allowlist owns exactly the three documented filters and binds
    each to a positional placeholder. A future change that adds a key
    without updating this test forces an explicit decision."""
    assert _AUDIT_EXPORT_FILTERS == {
        "action": "action = ?",
        "target": "target = ?",
        "actor": "actor = ?",
    }


def test_audit_export_filters_use_only_placeholders() -> None:
    """Every fragment binds via ``?`` — never an f-string interpolation
    or a literal value."""
    for fragment in _AUDIT_EXPORT_FILTERS.values():
        assert fragment.endswith(" = ?")
        assert "%s" not in fragment


# ---------------------------------------------------------------------------
# happy path: no filters
# ---------------------------------------------------------------------------


def test_export_no_filters_returns_all_rows(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, timestamp="2026-04-26T00:00:01+00:00", action="a")
    _insert_row(test_client, timestamp="2026-04-26T00:00:02+00:00", action="b")
    _insert_row(test_client, timestamp="2026-04-26T00:00:03+00:00", action="c")

    response = test_client.get("/api/actions/audit/export")
    assert response.status_code == 200
    rows = _decode_lines(response.text)
    assert len(rows) == 3


def test_export_orders_by_timestamp_desc_then_id_desc(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, timestamp="2026-04-26T00:00:01+00:00", action="early")
    _insert_row(test_client, timestamp="2026-04-26T00:00:03+00:00", action="late")
    _insert_row(test_client, timestamp="2026-04-26T00:00:02+00:00", action="middle")

    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert [r["action"] for r in rows] == ["late", "middle", "early"]


# ---------------------------------------------------------------------------
# response envelope
# ---------------------------------------------------------------------------


def test_export_content_disposition_header(test_client: TestClient) -> None:
    response = test_client.get("/api/actions/audit/export")
    assert response.headers["content-disposition"] == 'attachment; filename="audit.ndjson"'


def test_export_media_type(test_client: TestClient) -> None:
    response = test_client.get("/api/actions/audit/export")
    assert response.headers["content-type"].startswith("application/x-ndjson")


# ---------------------------------------------------------------------------
# emitted record shape
# ---------------------------------------------------------------------------


def test_export_each_line_has_eight_keys(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(
        test_client,
        action="row",
        before={"x": 1},
        after={"x": 2},
        reason="why",
    )
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert len(rows) == 1
    assert set(rows[0].keys()) == _EXPECTED_KEYS


def test_export_decodes_before_and_after_when_present(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(
        test_client,
        action="diff",
        before={"is_paused": False},
        after={"is_paused": True, "rows_updated": 3},
    )
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["before"] == {"is_paused": False}
    assert rows[0]["after"] == {"is_paused": True, "rows_updated": 3}


def test_export_before_and_after_null_when_json_column_null(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="bare")  # before/after default to None
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["before"] is None
    assert rows[0]["after"] is None


# ---------------------------------------------------------------------------
# single-filter branches
# ---------------------------------------------------------------------------


def test_export_filter_by_action(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause", target="alice@x")
    _insert_row(test_client, action="user.unpause", target="alice@x")

    response = test_client.get("/api/actions/audit/export", params={"action": "user.pause"})
    rows = _decode_lines(response.text)
    assert [r["action"] for r in rows] == ["user.pause"]


def test_export_filter_by_target(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause", target="alice@x")
    _insert_row(test_client, action="user.pause", target="bob@x")

    response = test_client.get("/api/actions/audit/export", params={"target": "bob@x"})
    rows = _decode_lines(response.text)
    assert [r["target"] for r in rows] == ["bob@x"]


def test_export_filter_by_actor(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, actor="alice@x", action="x")
    _insert_row(test_client, actor="bob@x", action="y")

    response = test_client.get("/api/actions/audit/export", params={"actor": "alice@x"})
    rows = _decode_lines(response.text)
    assert [r["actor"] for r in rows] == ["alice@x"]


# ---------------------------------------------------------------------------
# combined-filter branches
# ---------------------------------------------------------------------------


def test_export_filter_action_and_target(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause", target="alice@x")
    _insert_row(test_client, action="user.pause", target="bob@x")
    _insert_row(test_client, action="user.unpause", target="alice@x")

    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "user.pause", "target": "alice@x"},
    )
    rows = _decode_lines(response.text)
    assert len(rows) == 1
    assert rows[0]["action"] == "user.pause"
    assert rows[0]["target"] == "alice@x"


def test_export_filter_action_and_actor(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause", actor="op1")
    _insert_row(test_client, action="user.unpause", actor="op1")
    _insert_row(test_client, action="user.pause", actor="op2")

    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "user.pause", "actor": "op1"},
    )
    rows = _decode_lines(response.text)
    assert len(rows) == 1
    assert rows[0]["actor"] == "op1"


def test_export_filter_target_and_actor(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, target="t1", actor="op1")
    _insert_row(test_client, target="t2", actor="op1")
    _insert_row(test_client, target="t1", actor="op2")

    response = test_client.get(
        "/api/actions/audit/export",
        params={"target": "t1", "actor": "op1"},
    )
    rows = _decode_lines(response.text)
    assert len(rows) == 1


def test_export_filter_all_three_combined(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="x", target="t1", actor="op1")
    _insert_row(test_client, action="x", target="t1", actor="op2")
    _insert_row(test_client, action="x", target="t2", actor="op1")
    _insert_row(test_client, action="y", target="t1", actor="op1")

    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "x", "target": "t1", "actor": "op1"},
    )
    rows = _decode_lines(response.text)
    assert len(rows) == 1
    assert rows[0]["action"] == "x"
    assert rows[0]["target"] == "t1"
    assert rows[0]["actor"] == "op1"


# ---------------------------------------------------------------------------
# empty-state behaviour
# ---------------------------------------------------------------------------


def test_export_empty_table_returns_empty_body(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    response = test_client.get("/api/actions/audit/export")
    assert response.status_code == 200
    assert response.text == ""


def test_export_filter_with_no_match_returns_empty_body(test_client: TestClient) -> None:
    _wipe_audit(test_client)
    _insert_row(test_client, action="real.action")
    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "no.such.action"},
    )
    assert response.status_code == 200
    assert response.text == ""


# ---------------------------------------------------------------------------
# auth gate (the route inherits ``require_admin`` from the app factory)
# ---------------------------------------------------------------------------


def test_export_requires_session_cookie(tmp_path: Path, fernet_key: str) -> None:
    """An unauthenticated TestClient — no /api/auth/login round-trip — gets
    401 from the export endpoint, proving the route inherits the admin
    dependency added in ``create_app``."""
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
    )
    anon_client = TestClient(app)
    anon_client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = anon_client.get("/api/actions/audit/export")
    assert response.status_code == 401
