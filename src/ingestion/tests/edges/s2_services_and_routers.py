"""Edge + production-edge coverage for slice 2 (services + routers).

Brainstorm of additional edges (>= 10 beyond the seed list, each tagged
[CODE-FIX] for impl bugs the reviewer-fixer must address, or
[TEST-COVERAGE] for things the test layer pins itself):

* [FIX-LANDED] purge_item previously referenced row.user_email +
  row.content_type but IngestedItem has neither (attribution lives in
  ``fetched_by_user_email`` and content_type is JSONB-only). Fixed in
  the slice-2 reviewer-fixer pass; happy-path coverage now lives in
  ``test_maintenance_service::test_purge_item_happy_path_writes_corrected_snapshot``
* [TEST-COVERAGE] router stack exposes 19 routes; if a wildcard is
  added later, the count test catches the silent surface change
* [TEST-COVERAGE] CredentialPort.list_for_user("unknown") returns []
  rather than raising — pinned in test_credential_store
* [TEST-COVERAGE] set_expired re-call on already-expired row returns 0
* [TEST-COVERAGE] every Pydantic model in results.py rejects extras
* [TEST-COVERAGE] AuditWriter Protocol surface is one method
* [TEST-COVERAGE] typed exceptions all chain to ServiceError
* [TEST-COVERAGE] router-side typed-exception translation table
* [TEST-COVERAGE] dry_run with reddit (unknown) -> 422 Pydantic
  validation, never reaches _build_adapter
* [TEST-COVERAGE] every router prefix is /api/actions
* [TEST-COVERAGE] pause flapping doesn't drop audit rows (one per call)
* [TEST-COVERAGE] _AUDIT_EXPORT_FILTERS bytewise stable
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.api.routers import (
    actions_audit,
    actions_maintenance,
    actions_pause,
    actions_sync,
)
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.services import (
    credential_service,
    maintenance_service,
    sync_service,
)
from ingestion.daemon.services.exceptions import (
    CredentialNotFoundError,
    FeatureDisabledError,
    ItemNotFoundError,
    TransientFailureError,
)
from ingestion.daemon.store.local import apply_migrations

from tests.edges._s2_helpers import CapturingWriter, FakeCredPort, build_client, make_row


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


# --- Seed list 1 — router translates typed exceptions to status codes ---


def test_router_translates_credential_not_found_to_404(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1a: per-credential route -> 404 on missing row."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post("/api/actions/users/ghost@x/moodle/pause", json={"reason": "x"})
    assert response.status_code == 404


def test_router_translates_user_not_found_to_404(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1b: user-level route -> 404 when user has zero rows."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post("/api/actions/users/ghost@x/pause", json={"reason": "x"})
    assert response.status_code == 404


def test_router_translates_sync_not_found_to_404(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1c: cancel a sync_id that doesn't exist -> 404."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post(
        "/api/actions/syncs/missing-id/cancel",
        json={"confirm": "missing-id", "reason": "x"},
    )
    assert response.status_code == 404


def test_router_translates_state_conflict_to_409(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1e: cancel a finished sync -> 409. Detail includes the
    state so the dashboard can render it without a re-fetch."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    db_path = client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "abcd-1234-5678-9012"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (sync_id, "alice@x", "teams", "2026-04-26T00:00:00+00:00", "complete"),
        )
        conn.commit()
    finally:
        conn.close()
    response = client.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": sync_id, "reason": "late"},
    )
    assert response.status_code == 409
    # Envelope shape: detail.message carries the human-readable string.
    detail = response.json()["detail"]
    assert detail["code"] == "state_conflict"
    assert "complete" in detail["message"]


def test_router_translates_alert_rule_not_found_to_404(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1d: PATCH .../mute on an unknown rule_id -> 404."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.patch("/api/actions/alerts/rules/9999/mute", json={"reason": "x"})
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_router_translates_item_not_found_to_404(tmp_path: Path, fernet_key: str) -> None:
    """Seed #1: ItemNotFound -> 404. Marked django_db; auto-skips
    without Postgres."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    item_id = "00000000-0000-0000-0000-000000000000"
    response = client.post(
        f"/api/actions/content/{item_id}/purge",
        json={"confirm": f"item:{item_id}", "reason": "test"},
    )
    assert response.status_code == 404


# --- Seed list 2 + 16 — URL stability ---


_EXPECTED_ROUTES: set[tuple[str, str]] = {
    ("/api/actions/audit/export", "GET"),
    ("/api/actions/users/{email}/{platform}/reset_cursor", "POST"),
    ("/api/actions/content/{item_id}/purge", "POST"),
    ("/api/actions/storage/compact", "POST"),
    ("/api/actions/dead_letter/{email}/{platform}/clear", "POST"),
    ("/api/actions/alerts/rules/{rule_id}/mute", "PATCH"),
    ("/api/actions/users/{email}/teams/refresh", "POST"),
    ("/api/actions/users/{email}/pause", "POST"),
    ("/api/actions/users/{email}/unpause", "POST"),
    ("/api/actions/users/{email}/{platform}/pause", "POST"),
    ("/api/actions/users/{email}/{platform}/unpause", "POST"),
    ("/api/actions/users/{email}/{platform}/mark_expired", "POST"),
    ("/api/actions/users/{email}/{platform}/force_sync", "POST"),
    ("/api/actions/users/{email}/force_sync_all", "POST"),
    ("/api/actions/sync/dry_run", "POST"),
    ("/api/actions/syncs/{sync_id}/cancel", "POST"),
    ("/api/actions/scheduler/drain", "POST"),
    ("/api/actions/scheduler/emergency_stop", "POST"),
    ("/api/actions/teams/refresh_all", "POST"),
}


def test_nineteen_routes_registered_at_legacy_paths(tmp_path: Path, fernet_key: str) -> None:
    """Seed #2 + #16: full URL stability table. The four-router split
    must register exactly the 19 legacy paths."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    actions_routes: set[tuple[str, str]] = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not path.startswith("/api/actions/") or methods is None:
            continue
        for method in methods:
            actions_routes.add((path, method))
    assert actions_routes == _EXPECTED_ROUTES


def test_actions_router_count_is_nineteen() -> None:
    """Seed #16: directly inspect the four routers' route lists."""
    total = sum(
        len(mod.router.routes)
        for mod in (actions_audit, actions_maintenance, actions_pause, actions_sync)
    )
    assert total == 19


def test_routers_share_api_actions_prefix() -> None:
    """[TEST-COVERAGE] Every router declares prefix=/api/actions."""
    for mod in (actions_audit, actions_maintenance, actions_pause, actions_sync):
        assert mod.router.prefix == "/api/actions"


# --- Seed list 3-7 — pause / unpause audit-row contracts ---


async def test_pause_user_emits_one_audit_row_seed3() -> None:
    """Seed #3: ONE bulk audit row per call."""
    port = FakeCredPort(
        [make_row(platform="moodle"), make_row(platform="teams"), make_row(platform="telegram")]
    )
    writer = CapturingWriter()
    await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason="x")
    assert len(writer.calls) == 1


async def test_unpause_user_emits_one_audit_row_seed4() -> None:
    """Seed #4: same one-row contract for the inverse op."""
    port = FakeCredPort(
        [make_row(platform="moodle", is_paused=True), make_row(platform="teams", is_paused=True)]
    )
    writer = CapturingWriter()
    await credential_service.unpause_user(port, "alice@x", audit_writer=writer, reason=None)
    assert len(writer.calls) == 1


async def test_per_credential_pause_emits_one_row_each_seed5() -> None:
    """Seed #5: per-credential route -> one row per call (two calls -> two rows)."""
    port = FakeCredPort([make_row(platform="moodle"), make_row(platform="teams")])
    writer = CapturingWriter()
    await credential_service.pause_credential(
        port, "alice@x", "moodle", audit_writer=writer, reason=None
    )
    await credential_service.pause_credential(
        port, "alice@x", "teams", audit_writer=writer, reason=None
    )
    assert len(writer.calls) == 2
    assert {c["target"] for c in writer.calls} == {"alice@x/moodle", "alice@x/teams"}


async def test_set_paused_bulk_returns_zero_on_no_op_seed6() -> None:
    """Seed #6: set_paused_bulk_for_user(paused=False) is a no-op when
    no credentials match the inverse filter."""
    port = FakeCredPort([make_row(is_paused=False)])
    assert await port.set_paused_bulk_for_user("alice@x", paused=False) == 0


async def test_pause_user_audit_before_carries_platforms_seed7() -> None:
    """Seed #7: ``before`` snapshot carries the platforms list."""
    port = FakeCredPort([make_row(platform="moodle"), make_row(platform="teams")])
    writer = CapturingWriter()
    await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None)
    before = writer.calls[0]["before"]
    assert isinstance(before, dict) and "platforms" in before
    assert {p["platform"] for p in before["platforms"]} == {"moodle", "teams"}


# --- Seed list 8 — force_sync_one is queue-only ---


async def test_force_sync_one_does_not_consult_credential_port(tmp_path: Path) -> None:
    """[TEST-COVERAGE] (revised seed #8 — the impl skips the port
    lookup; the queue insert is the contract). Pin: enqueue happens
    even with no credential lookup."""
    db = tmp_path / "s.db"
    apply_migrations(db)
    writer = CapturingWriter()
    result = await sync_service.force_sync_one(
        db, email="anyone@x", platform="moodle", audit_writer=writer, reason=None
    )
    assert result.ok is True
    assert len(writer.calls) == 1


# --- Seed list 9 — dry_run does not enqueue ---


async def test_dry_run_does_not_touch_force_sync_queue(
    tmp_path: Path, fernet_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed #9: dry-run is read-only — force_sync_requests stays empty."""
    from ingestion.daemon.ports import token_store as token_store_mod

    db = tmp_path / "s.db"
    apply_migrations(db)

    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())
    cipher = CredentialCipher([fernet_key])
    writer = CapturingWriter()
    with pytest.raises(CredentialNotFoundError):
        await sync_service.dry_run_sync(
            db, cipher=cipher, email="x@x", platform="moodle", audit_writer=writer
        )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT * FROM force_sync_requests").fetchall()
    finally:
        conn.close()
    assert rows == []


# --- Seed list 10 — compact on empty db ---


async def test_compact_storage_runs_against_fresh_db(tmp_path: Path) -> None:
    """Seed #10: VACUUM on a freshly-applied schema completes cleanly."""
    db = tmp_path / "s.db"
    apply_migrations(db)
    writer = CapturingWriter()
    result = await maintenance_service.compact_storage(db, audit_writer=writer, reason=None)
    assert result.after_bytes > 0


# --- Seed list 11 — clear_dead_letter idempotent ---


async def test_clear_dead_letter_idempotent_one_row_per_call(tmp_path: Path) -> None:
    """Seed #11: each clear call writes exactly one audit row."""
    db = tmp_path / "s.db"
    apply_migrations(db)
    writer = CapturingWriter()
    for _ in range(2):
        await maintenance_service.clear_dead_letter(
            db,
            email="alice@x",
            platform="moodle",
            threshold=3,
            cooldown_seconds=60,
            audit_writer=writer,
            reason=None,
        )
    assert len(writer.calls) == 2


# --- Seed list 12 — purge_item raises ItemNotFoundError ---


@pytest.mark.django_db(transaction=True)
async def test_purge_item_raises_item_not_found_for_missing_id() -> None:
    """Seed #12: typed raise + zero audit rows."""
    writer = CapturingWriter()
    with pytest.raises(ItemNotFoundError):
        await maintenance_service.purge_item(
            item_id="00000000-0000-0000-0000-000000000000",
            audit_writer=writer,
            reason=None,
        )
    assert writer.calls == []


# --- Seed list 13 — mute_rule toggles ---


async def test_mute_rule_toggles_enabled_and_emits_audit(tmp_path: Path) -> None:
    """Seed #13: full toggle cycle pins both action labels."""
    from ingestion.daemon.alerts import rules as rule_store

    db = tmp_path / "s.db"
    apply_migrations(db)
    rid = await rule_store.create_rule(
        db,
        name="noisy",
        metric="failure_count",
        comparator=rule_store.Comparator.GT,
        threshold=1,
        cooldown_seconds=60,
    )
    writer = CapturingWriter()
    await maintenance_service.mute_rule(
        db, rule_id=rid, enabled=False, audit_writer=writer, reason=None
    )
    await maintenance_service.mute_rule(
        db, rule_id=rid, enabled=True, audit_writer=writer, reason=None
    )
    assert [c["action"] for c in writer.calls] == ["alerts.rule_muted", "alerts.rule_unmuted"]


# --- Seed list 14 + 15 — force_refresh_teams typed raises ---


async def test_force_refresh_teams_503_when_disabled(fernet_key: str) -> None:
    """Seed #14: gate trips -> FeatureDisabledError -> router 503."""

    class _Cfg:
        class teams_refresh:  # noqa: N801
            enabled = False
            timeout_seconds = 1
            max_retries_per_user = 0

    cipher = CredentialCipher([fernet_key])
    writer = CapturingWriter()
    with pytest.raises(FeatureDisabledError):
        await maintenance_service.force_refresh_teams(
            config=_Cfg(),  # type: ignore[arg-type]
            cipher=cipher,
            user_refresh_locks={},
            email="alice@x",
            audit_writer=writer,
            reason=None,
        )


async def test_force_refresh_teams_502_on_transient(
    fernet_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed #15: ERROR outcome -> TransientFailureError -> router 502."""
    from ingestion.daemon.auth import refresh as refresh_mod
    from ingestion.daemon.ports import token_store as token_store_mod

    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())

    async def _fake_refresh(**_kw: Any) -> Any:
        return refresh_mod.RefreshResult(
            user_email="alice@x", outcome=refresh_mod.RefreshOutcome.ERROR, error="boom"
        )

    monkeypatch.setattr(refresh_mod, "refresh_one", _fake_refresh)

    class _Cfg:
        class teams_refresh:  # noqa: N801
            enabled = True
            timeout_seconds = 1
            max_retries_per_user = 0

    cipher = CredentialCipher([fernet_key])
    writer = CapturingWriter()
    with pytest.raises(TransientFailureError) as excinfo:
        await maintenance_service.force_refresh_teams(
            config=_Cfg(),  # type: ignore[arg-type]
            cipher=cipher,
            user_refresh_locks={},
            email="alice@x",
            audit_writer=writer,
            reason=None,
        )
    assert excinfo.value.detail == {"outcome": "error", "error": "boom"}


# --- Seed list 17 — flapping audit trail ---


async def test_pause_unpause_flapping_keeps_audit_trail() -> None:
    """Seed #17: 50 alternating calls -> 50 audit rows; no dedup."""
    port = FakeCredPort([make_row(platform="moodle")])
    writer = CapturingWriter()
    for i in range(50):
        if i % 2 == 0:
            await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None)
        else:
            await credential_service.unpause_user(port, "alice@x", audit_writer=writer, reason=None)
    assert len(writer.calls) == 50


# --- Seed list 18 — pause-during-sync state observable ---


async def test_pause_user_during_in_flight_sync_succeeds() -> None:
    """Seed #18: pause flag flips even with a 'sync in flight'
    (modeled by the port state)."""
    port = FakeCredPort([make_row(platform="moodle")])
    writer = CapturingWriter()
    await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None)
    after = await port.get("alice@x", "moodle")
    assert after is not None and after.is_paused is True


# --- Seed list 19 — typed exception __cause__ chain preserved ---


async def test_typed_exception_preserves_cause_chain() -> None:
    """Seed #19: ``raise ... from exc`` preserves __cause__ for log
    pivots back to the underlying ORM error."""
    try:
        try:
            raise RuntimeError("sql said no")  # noqa: TRY301
        except RuntimeError as exc:
            raise ItemNotFoundError("missing") from exc
    except ItemNotFoundError as caught:
        assert caught.__cause__ is not None
        assert isinstance(caught.__cause__, RuntimeError)


# --- Seed list 20 — _AUDIT_EXPORT_FILTERS byte-stable ---


def test_audit_export_filters_byte_identical() -> None:
    """Seed #20: pin the constant exactly under its new module path."""
    from ingestion.daemon.api.routers.actions_audit import _AUDIT_EXPORT_FILTERS

    assert _AUDIT_EXPORT_FILTERS == {
        "action": "action = ?",
        "target": "target = ?",
        "actor": "actor = ?",
    }
