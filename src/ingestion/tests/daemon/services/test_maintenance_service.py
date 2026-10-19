"""Coverage for :mod:`ingestion.daemon.services.maintenance_service`.

* reset_cursor — deletes per-(email, platform[, scope]) cursor rows;
  audit row carries the deleted scopes
* purge_item — typed raise on missing id (covered structurally;
  Django-touching variant is in the live-DB pass)
* compact_storage — VACUUM round-trip; works on an empty DB too
* clear_dead_letter — idempotent; audit always lands
* mute_rule — flips ``alert_rules.enabled``; raises typed exception
  on missing id
* force_refresh_teams — typed exceptions for disabled feature and
  transient failure; audit row carries outcome
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.services import maintenance_service
from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    FeatureDisabledError,
    TransientFailureError,
)
from ingestion.daemon.store.local import apply_migrations

from tests.daemon.services.test_credential_service import CapturingAuditWriter


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _seed_cursor(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    scope: str = "default",
    cursor: dict[str, Any] | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_email,
                platform,
                scope,
                json.dumps(cursor or {"token": "abc"}),
                "2026-04-26T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# reset_cursor
# ---------------------------------------------------------------------------


async def test_reset_cursor_deletes_all_scopes_when_unset(db_path: Path) -> None:
    _seed_cursor(db_path, user_email="alice@x", platform="teams", scope="messages")
    _seed_cursor(db_path, user_email="alice@x", platform="teams", scope="assignments")
    _seed_cursor(db_path, user_email="alice@x", platform="moodle")  # untouched

    writer = CapturingAuditWriter()
    result = await maintenance_service.reset_cursor(
        db_path,
        email="alice@x",
        platform="teams",
        scope=None,
        audit_writer=writer,
        reason="stuck",
    )
    assert result.rows_deleted == 2
    # The moodle cursor must remain.
    conn = sqlite3.connect(str(db_path))
    try:
        remaining = conn.execute(
            "SELECT platform, scope FROM sync_cursors ORDER BY platform, scope"
        ).fetchall()
    finally:
        conn.close()
    assert remaining == [("moodle", "default")]
    # Audit row carries the deleted scopes (snapshot).
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "sync.reset_cursor"
    assert call["target"] == "alice@x/teams"
    assert call["after"] == {"rows_deleted": 2, "scope_filter": None}


async def test_reset_cursor_deletes_only_named_scope(db_path: Path) -> None:
    _seed_cursor(db_path, user_email="alice@x", platform="teams", scope="messages")
    _seed_cursor(db_path, user_email="alice@x", platform="teams", scope="assignments")

    writer = CapturingAuditWriter()
    result = await maintenance_service.reset_cursor(
        db_path,
        email="alice@x",
        platform="teams",
        scope="messages",
        audit_writer=writer,
        reason=None,
    )
    assert result.rows_deleted == 1
    conn = sqlite3.connect(str(db_path))
    try:
        remaining = conn.execute("SELECT scope FROM sync_cursors").fetchall()
    finally:
        conn.close()
    assert remaining == [("assignments",)]
    assert writer.calls[0]["after"]["scope_filter"] == "messages"


async def test_reset_cursor_with_no_rows_returns_zero(db_path: Path) -> None:
    """[TEST-COVERAGE] Nothing to delete -> 0; audit row still lands so
    operator intent is recorded even on a no-op."""
    writer = CapturingAuditWriter()
    result = await maintenance_service.reset_cursor(
        db_path,
        email="ghost@x",
        platform="moodle",
        scope=None,
        audit_writer=writer,
        reason=None,
    )
    assert result.rows_deleted == 0
    assert len(writer.calls) == 1
    assert writer.calls[0]["before"] == {"scopes": []}


# ---------------------------------------------------------------------------
# compact_storage
# ---------------------------------------------------------------------------


async def test_compact_storage_returns_before_and_after_size(db_path: Path) -> None:
    """VACUUM round-trip on a freshly-migrated DB. ``after`` may equal
    ``before`` (nothing to reclaim); the contract is just that both
    fields are present and non-negative."""
    writer = CapturingAuditWriter()
    result = await maintenance_service.compact_storage(
        db_path, audit_writer=writer, reason="housekeeping"
    )
    assert result.before_bytes >= 0
    assert result.after_bytes >= 0
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "storage.compact"


async def test_compact_storage_works_on_minimal_database(tmp_path: Path) -> None:
    """[TEST-COVERAGE] A freshly-applied schema (no user data) must
    VACUUM cleanly. Pins that compact_storage doesn't crash on a 'cold
    start' database."""
    path = tmp_path / "fresh.db"
    apply_migrations(path)
    writer = CapturingAuditWriter()
    result = await maintenance_service.compact_storage(path, audit_writer=writer, reason=None)
    # before == after on a no-op vacuum is valid.
    assert result.after_bytes > 0


# ---------------------------------------------------------------------------
# clear_dead_letter
# ---------------------------------------------------------------------------


async def test_clear_dead_letter_emits_audit_when_no_state(db_path: Path) -> None:
    """No prior dead-letter state -> the call is a no-op on the table
    but still emits one audit row carrying ``before=None``."""
    writer = CapturingAuditWriter()
    await maintenance_service.clear_dead_letter(
        db_path,
        email="alice@x",
        platform="moodle",
        threshold=3,
        cooldown_seconds=60,
        audit_writer=writer,
        reason=None,
    )
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "dead_letter.clear"
    assert call["target"] == "alice@x/moodle"
    assert call["before"] is None
    assert call["after"] == {
        "consecutive_failures": 0,
        "last_failure_at": None,
        "suppressed_until": None,
    }


async def test_clear_dead_letter_idempotent_each_call_emits_one_row(db_path: Path) -> None:
    """[TEST-COVERAGE] Two consecutive clears each emit ONE audit row
    (one per call). Pin that we don't accidentally double-write."""
    writer = CapturingAuditWriter()
    for _ in range(2):
        await maintenance_service.clear_dead_letter(
            db_path,
            email="alice@x",
            platform="moodle",
            threshold=3,
            cooldown_seconds=60,
            audit_writer=writer,
            reason=None,
        )
    assert len(writer.calls) == 2


async def test_clear_dead_letter_snapshots_existing_state(db_path: Path) -> None:
    """When dead-letter state exists, the audit ``before`` carries the
    suppressed-until + counter values."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dead_letter_state (user_email, platform, consecutive_failures, "
            "last_failure_at, suppressed_until) VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "moodle", 5, "2026-04-25T00:00:00+00:00", "2026-04-26T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    writer = CapturingAuditWriter()
    await maintenance_service.clear_dead_letter(
        db_path,
        email="alice@x",
        platform="moodle",
        threshold=3,
        cooldown_seconds=60,
        audit_writer=writer,
        reason=None,
    )
    assert len(writer.calls) == 1
    before = writer.calls[0]["before"]
    assert before is not None
    assert before["consecutive_failures"] == 5


# ---------------------------------------------------------------------------
# mute_rule
# ---------------------------------------------------------------------------


async def test_mute_rule_raises_when_id_unknown(db_path: Path) -> None:
    writer = CapturingAuditWriter()
    with pytest.raises(AlertRuleNotFoundError):
        await maintenance_service.mute_rule(
            db_path, rule_id=9999, enabled=False, audit_writer=writer, reason=None
        )
    assert writer.calls == []


async def test_mute_rule_flips_enabled_and_emits_audit(db_path: Path) -> None:
    from ingestion.daemon.alerts import rules as rule_store

    rule_id = await rule_store.create_rule(
        db_path,
        name="noisy",
        metric="failure_count",
        comparator=rule_store.Comparator.GT,
        threshold=1,
        cooldown_seconds=60,
    )
    writer = CapturingAuditWriter()
    result = await maintenance_service.mute_rule(
        db_path, rule_id=rule_id, enabled=False, audit_writer=writer, reason="noisy"
    )
    assert result.enabled is False
    assert result.rule_id == rule_id
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "alerts.rule_muted"
    assert call["target"] == "noisy"
    # Re-enable hits the unmuted action.
    await maintenance_service.mute_rule(
        db_path, rule_id=rule_id, enabled=True, audit_writer=writer, reason=None
    )
    assert writer.calls[1]["action"] == "alerts.rule_unmuted"


# ---------------------------------------------------------------------------
# force_refresh_teams
# ---------------------------------------------------------------------------


class _StubConfig:
    """Minimal config carrying just teams_refresh; the service only
    reads ``config.teams_refresh.{enabled,timeout_seconds,max_retries_per_user}``."""

    def __init__(self, *, enabled: bool, timeout_seconds: int = 1, max_retries: int = 0) -> None:
        self.teams_refresh = _StubRefreshConfig(
            enabled=enabled, timeout_seconds=timeout_seconds, max_retries_per_user=max_retries
        )


class _StubRefreshConfig:
    def __init__(self, *, enabled: bool, timeout_seconds: int, max_retries_per_user: int) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_retries_per_user = max_retries_per_user


async def test_force_refresh_teams_raises_feature_disabled(db_path: Path, fernet_key: str) -> None:
    from ingestion.daemon.crypto import CredentialCipher

    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    locks: dict[str, asyncio.Lock] = {}
    with pytest.raises(FeatureDisabledError):
        await maintenance_service.force_refresh_teams(
            config=_StubConfig(enabled=False),  # type: ignore[arg-type]
            cipher=cipher,
            user_refresh_locks=locks,
            email="alice@x",
            audit_writer=writer,
            reason=None,
        )
    # No audit when the feature gate trips before any work.
    assert writer.calls == []


async def test_force_refresh_teams_translates_error_outcome_to_transient(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """[TEST-COVERAGE] When the underlying refresh returns an ERROR
    outcome (not SUCCESS, not EXPIRED), the service raises
    ``TransientFailureError`` carrying the structured detail. Pin that
    the audit row STILL lands (so failures are visible to the operator)
    BEFORE the raise."""
    from ingestion.daemon.auth import refresh as refresh_mod
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    # Stub out PostgresTokenStore — never actually called because we
    # also stub refresh_one — but the service constructs it.
    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())

    async def _fake_refresh_one(**_kwargs: Any) -> Any:
        return refresh_mod.RefreshResult(
            user_email="alice@x", outcome=refresh_mod.RefreshOutcome.ERROR, error="boom"
        )

    # The service does ``from ingestion.daemon.auth.refresh import refresh_one``
    # inside the function body, so patch the source module.
    monkeypatch.setattr(refresh_mod, "refresh_one", _fake_refresh_one)

    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    locks: dict[str, asyncio.Lock] = {}
    with pytest.raises(TransientFailureError) as excinfo:
        await maintenance_service.force_refresh_teams(
            config=_StubConfig(enabled=True),  # type: ignore[arg-type]
            cipher=cipher,
            user_refresh_locks=locks,
            email="alice@x",
            audit_writer=writer,
            reason=None,
        )
    assert excinfo.value.detail == {"outcome": "error", "error": "boom"}
    # Audit row landed BEFORE the raise (operator can see the attempt).
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "teams.force_refresh"


async def test_force_refresh_teams_returns_outcome_on_success(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """Happy path: outcome=SUCCESS -> typed result, audit landed."""
    from ingestion.daemon.auth import refresh as refresh_mod
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())

    async def _fake_refresh_one(**_kwargs: Any) -> Any:
        return refresh_mod.RefreshResult(
            user_email="alice@x", outcome=refresh_mod.RefreshOutcome.SUCCESS
        )

    monkeypatch.setattr(refresh_mod, "refresh_one", _fake_refresh_one)

    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    locks: dict[str, asyncio.Lock] = {}
    result = await maintenance_service.force_refresh_teams(
        config=_StubConfig(enabled=True),  # type: ignore[arg-type]
        cipher=cipher,
        user_refresh_locks=locks,
        email="alice@x",
        audit_writer=writer,
        reason=None,
    )
    assert result.outcome == "success"
    assert len(writer.calls) == 1


async def test_force_refresh_teams_returns_outcome_on_expired(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """[TEST-COVERAGE] Expired -> typed result (NOT TransientFailure).
    Pins the branch in the service body."""
    from ingestion.daemon.auth import refresh as refresh_mod
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())

    async def _fake_refresh_one(**_kwargs: Any) -> Any:
        return refresh_mod.RefreshResult(
            user_email="alice@x", outcome=refresh_mod.RefreshOutcome.EXPIRED
        )

    monkeypatch.setattr(refresh_mod, "refresh_one", _fake_refresh_one)

    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    locks: dict[str, asyncio.Lock] = {}
    result = await maintenance_service.force_refresh_teams(
        config=_StubConfig(enabled=True),  # type: ignore[arg-type]
        cipher=cipher,
        user_refresh_locks=locks,
        email="alice@x",
        audit_writer=writer,
        reason=None,
    )
    assert result.outcome == "expired"


# ---------------------------------------------------------------------------
# purge_item — Django-touching; auto-skipped without Postgres
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_purge_item_raises_when_id_unknown() -> None:
    """``ItemNotFoundError`` is the typed exception the router maps to
    404. Pin the raise; the audit writer is never reached."""
    from ingestion.daemon.services.exceptions import ItemNotFoundError

    writer = CapturingAuditWriter()
    with pytest.raises(ItemNotFoundError):
        await maintenance_service.purge_item(
            item_id="00000000-0000-0000-0000-000000000000",
            audit_writer=writer,
            reason=None,
        )
    assert writer.calls == []


@pytest.mark.django_db(transaction=True)
async def test_purge_item_happy_path_writes_corrected_snapshot() -> None:
    """[FIX-VERIFY] Happy path after the slice-2 attribute-name fix.
    The audit ``before`` now references the real model columns
    (``fetched_by_user_email`` not ``user_email``) and pulls
    ``content_type`` out of the JSONB ``payload`` instead of expecting
    a top-level field. The row is deleted only after the audit lands.
    """
    from core.models import IngestedItem

    item = await IngestedItem.objects.acreate(
        external_id="ext-purge-1",
        platform="moodle",
        course_external_id="C-101",
        source_url="https://example/source",
        fetched_by_user_email="alice@example.com",
        payload={"content_type": "assignment", "title": "Homework 1"},
        raw_payload={},
    )

    writer = CapturingAuditWriter()
    result = await maintenance_service.purge_item(
        item_id=str(item.id),
        audit_writer=writer,
        reason="cleanup",
    )

    assert result.ok is True
    assert result.item_id == str(item.id)
    # Row must be gone after the call.
    assert not await IngestedItem.objects.filter(id=item.id).aexists()
    # Audit row carries the corrected fields.
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "content.purge"
    assert call["target"] == f"item:{item.id}"
    before = call["before"]
    assert before["id"] == str(item.id)
    assert before["external_id"] == "ext-purge-1"
    # The KEY fix: audit references the real attribution column.
    assert before["user_email"] == "alice@example.com"
    assert before["platform"] == "moodle"
    assert before["course_external_id"] == "C-101"
    assert before["source_url"] == "https://example/source"
    # Content type pulled from the JSONB payload, not a top-level field.
    assert before["content_type"] == "assignment"
    assert before["ingested_at"] is not None
    assert call["after"] == {"deleted": True}
    assert call["reason"] == "cleanup"


@pytest.mark.django_db(transaction=True)
async def test_purge_item_snapshot_handles_missing_payload_fields() -> None:
    """[FIX-VERIFY] Defensive: legacy rows with non-dict or
    content-type-free ``payload`` blobs must not crash the snapshot;
    ``content_type`` falls through to ``None``."""
    from core.models import IngestedItem

    item = await IngestedItem.objects.acreate(
        external_id="ext-purge-legacy",
        platform="telegram",
        course_external_id=None,
        source_url=None,
        fetched_by_user_email=None,  # telegram bot has no per-user attribution
        payload={},  # no content_type key
        raw_payload={},
    )

    writer = CapturingAuditWriter()
    await maintenance_service.purge_item(
        item_id=str(item.id),
        audit_writer=writer,
        reason=None,
    )

    before = writer.calls[0]["before"]
    assert before["content_type"] is None
    assert before["user_email"] is None
    assert before["course_external_id"] is None
    assert before["source_url"] is None


# ---------------------------------------------------------------------------
# Concurrency / lock reuse
# ---------------------------------------------------------------------------


async def test_force_refresh_teams_reuses_per_user_lock(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """[TEST-COVERAGE] The user_refresh_locks dict must hold the lock
    after the call so a concurrent bulk-cycle reuses the same lock for
    the same email. Pin the lazy-create-and-stash."""
    from ingestion.daemon.auth import refresh as refresh_mod
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    class _TS:
        async def get_credentials(self, *_a: Any, **_kw: Any) -> None:
            return None

    monkeypatch.setattr(token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _TS())

    async def _fake_refresh_one(**_kwargs: Any) -> Any:
        return refresh_mod.RefreshResult(
            user_email="alice@x", outcome=refresh_mod.RefreshOutcome.SUCCESS
        )

    monkeypatch.setattr(refresh_mod, "refresh_one", _fake_refresh_one)

    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    locks: dict[str, asyncio.Lock] = {}
    await maintenance_service.force_refresh_teams(
        config=_StubConfig(enabled=True),  # type: ignore[arg-type]
        cipher=cipher,
        user_refresh_locks=locks,
        email="alice@x",
        audit_writer=writer,
        reason=None,
    )
    assert "alice@x" in locks
    # Re-call: the same lock must be reused (same id).
    initial_lock = locks["alice@x"]
    await maintenance_service.force_refresh_teams(
        config=_StubConfig(enabled=True),  # type: ignore[arg-type]
        cipher=cipher,
        user_refresh_locks=locks,
        email="alice@x",
        audit_writer=writer,
        reason=None,
    )
    assert locks["alice@x"] is initial_lock
