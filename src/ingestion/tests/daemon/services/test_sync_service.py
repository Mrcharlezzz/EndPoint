"""Coverage for :mod:`ingestion.daemon.services.sync_service`.

* force_sync_one — enqueues one row, emits one audit
* force_sync_all_for_user — enqueues per non-expired credential, emits
  one bulk audit row, raises ``UserNotFoundError`` when the user has zero
  credentials
* dry_run_sync — runs the pipeline in-memory, doesn't touch the queue,
  raises ``CredentialNotFoundError`` when the token store returns None
* cancel_sync — terminal state, ``StateConflictError`` on non-running,
  ``SyncNotFoundError`` on missing
* set_drain_mode + emergency_stop — overlay flips, audit-first ordering
* bulk_refresh_teams — per-Teams-credential enqueue, one audit row
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.api.cancel_registry import CancelRegistry
from ingestion.daemon.api.pubsub import EventBus
from ingestion.daemon.ports.credential_port import CredentialRow
from ingestion.daemon.services import sync_service
from ingestion.daemon.services.exceptions import (
    CredentialNotFoundError,
    StateConflictError,
    SyncNotFoundError,
    UserNotFoundError,
)
from ingestion.daemon.store.local import apply_migrations, open_store

from tests.daemon.services.test_credential_service import (
    CapturingAuditWriter,
    FakeCredentialPort,
)


def _row(
    *,
    user_email: str = "alice@x",
    platform: str = "moodle",
    is_expired: bool = False,
) -> CredentialRow:
    return CredentialRow(
        user_email=user_email,
        platform=platform,
        is_paused=False,
        is_expired=is_expired,
        expired_at=None,
        last_refreshed_at=None,
        encrypted_payload=b"\x00",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _read_force_sync(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT user_email, platform FROM force_sync_requests").fetchall()
    finally:
        conn.close()


def _insert_sync_row(
    db_path: Path,
    *,
    sync_id: str,
    state: str = "running",
    user_email: str = "alice@x",
    platform: str = "teams",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (sync_id, user_email, platform, "2026-04-26T00:00:00+00:00", state),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# force_sync_one
# ---------------------------------------------------------------------------


async def test_force_sync_one_enqueues_row_and_emits_audit(db_path: Path) -> None:
    writer = CapturingAuditWriter()
    result = await sync_service.force_sync_one(
        db_path, email="alice@x", platform="moodle", audit_writer=writer, reason="manual"
    )
    assert result.ok is True
    assert result.user_email == "alice@x"
    assert result.platform == "moodle"
    assert _read_force_sync(db_path) == [("alice@x", "moodle")]
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "sync.force_requested"
    assert call["target"] == "alice@x/moodle"
    assert call["after"] == {"queued": True}
    assert call["reason"] == "manual"


# ---------------------------------------------------------------------------
# force_sync_all_for_user
# ---------------------------------------------------------------------------


async def test_force_sync_all_skips_expired_credentials(db_path: Path) -> None:
    """Expired creds are skipped by the operator-side service so the
    operator doesn't queue no-ops. Pin the split: enqueued vs skipped."""
    port = FakeCredentialPort(
        [
            _row(platform="moodle"),
            _row(platform="teams", is_expired=True),
            _row(platform="telegram"),
        ]
    )
    writer = CapturingAuditWriter()
    result = await sync_service.force_sync_all_for_user(
        db_path, port=port, email="alice@x", audit_writer=writer, reason="rotation"
    )
    assert sorted(result.enqueued) == ["moodle", "telegram"]
    assert result.skipped == ["teams"]
    enqueued = sorted(_read_force_sync(db_path))
    assert enqueued == [("alice@x", "moodle"), ("alice@x", "telegram")]


async def test_force_sync_all_emits_one_audit_row(db_path: Path) -> None:
    port = FakeCredentialPort([_row(platform="moodle"), _row(platform="teams")])
    writer = CapturingAuditWriter()
    await sync_service.force_sync_all_for_user(
        db_path, port=port, email="alice@x", audit_writer=writer, reason=None
    )
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "sync.force_requested_all"
    assert call["target"] == "alice@x"
    assert sorted(call["after"]["enqueued"]) == ["moodle", "teams"]
    assert call["after"]["skipped"] == []


async def test_force_sync_all_raises_when_user_unknown(db_path: Path) -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(UserNotFoundError):
        await sync_service.force_sync_all_for_user(
            db_path, port=port, email="ghost@x", audit_writer=writer, reason=None
        )
    # Nothing enqueued and no audit row.
    assert _read_force_sync(db_path) == []
    assert writer.calls == []


async def test_force_sync_all_with_only_expired_emits_audit(db_path: Path) -> None:
    """[TEST-COVERAGE] User has credentials but ALL are expired -> the
    user is known so we still emit (enqueued=[], skipped=[platform]).
    Pins that this is NOT routed to UserNotFoundError."""
    port = FakeCredentialPort([_row(platform="moodle", is_expired=True)])
    writer = CapturingAuditWriter()
    result = await sync_service.force_sync_all_for_user(
        db_path, port=port, email="alice@x", audit_writer=writer, reason=None
    )
    assert result.enqueued == []
    assert result.skipped == ["moodle"]
    assert len(writer.calls) == 1


# ---------------------------------------------------------------------------
# dry_run_sync
# ---------------------------------------------------------------------------


class _StubTokenStore:
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


async def test_dry_run_sync_raises_when_no_credentials(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """No credentials -> ``CredentialNotFoundError``. Pin the typed
    raise so the router translation contract holds."""
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    monkeypatch.setattr(
        token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _StubTokenStore(None)
    )
    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    with pytest.raises(CredentialNotFoundError):
        await sync_service.dry_run_sync(
            db_path,
            cipher=cipher,
            email="ghost@x",
            platform="moodle",
            audit_writer=writer,
        )
    # No audit row when the precondition failed.
    assert writer.calls == []


# ---------------------------------------------------------------------------
# Internal helpers — _build_adapter, _ReadThroughState, _InMemoryDryRunLogger
# ---------------------------------------------------------------------------


def test_build_adapter_raises_for_unknown_platform() -> None:
    """[TEST-COVERAGE] The adapter factory rejects unknown platforms
    with a clear ``ValueError``. Pin the message shape so the router /
    log line stays grep-able."""
    with pytest.raises(ValueError, match="reddit"):
        sync_service._build_adapter("reddit")


@pytest.mark.parametrize("platform", ["moodle", "teams", "telegram"])
def test_build_adapter_returns_an_adapter_for_known_platform(platform: str) -> None:
    """[TEST-COVERAGE] Each known platform yields an adapter instance.
    Constructing each one is enough; we don't exercise their methods.
    Pin the lazy-import path so a future move of an adapter module
    surfaces here, not at first force-sync."""
    adapter = sync_service._build_adapter(platform)
    assert adapter is not None
    # Adapters expose a ``.platform`` attribute by convention; pin it.
    assert getattr(adapter, "platform", None) is not None


async def test_read_through_state_returns_real_cursor_and_drops_writes() -> None:
    """[TEST-COVERAGE] The dry-run wrapper around SyncStateStore must
    forward reads but swallow writes — pin both halves so a future
    refactor can't accidentally start advancing cursors during a dry
    run."""
    real_calls: list[tuple[str, ...]] = []

    class _Real:
        async def get_cursor(
            self, user_id: str, platform: str, scope: str
        ) -> dict[str, Any] | None:
            real_calls.append(("get", user_id, platform, scope))
            return {"token": "abc"}

        async def set_cursor(
            self, user_id: str, platform: str, scope: str, cursor: dict[str, Any]
        ) -> None:
            real_calls.append(("set", user_id, platform, scope))

    state = sync_service._ReadThroughState(_Real())
    cursor = await state.get_cursor("alice@x", "moodle", "default")
    assert cursor == {"token": "abc"}
    # Writes must NOT reach the underlying real_state.
    await state.set_cursor("alice@x", "moodle", "default", {"new": "v"})
    assert ("set", "alice@x", "moodle", "default") not in real_calls


async def test_in_memory_dry_run_logger_captures_lifecycle() -> None:
    """[TEST-COVERAGE] The fake sync-logger must answer the four
    methods the pipeline calls (log_start / log_complete / log_failure)
    without writing to sync_history. Pin the sentinel id ``"dry-run"``
    so failure logs are grep-able."""
    logger = sync_service._InMemoryDryRunLogger()
    sid = await logger.log_start("alice@x", "moodle")
    assert sid == "dry-run"
    await logger.log_complete(sid, {"items_fetched": 5})
    assert logger.stats == {"items_fetched": 5}
    await logger.log_failure(sid, "boom")
    assert logger.failure == "boom"


async def test_dry_run_sync_does_not_enqueue_force_sync(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
) -> None:
    """[TEST-COVERAGE] dry-run is a READ-ONLY probe — it must not write
    a row to ``force_sync_requests``. Caller relies on this to A/B a
    config change without scheduling a real sync."""
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports import token_store as token_store_mod

    monkeypatch.setattr(
        token_store_mod, "PostgresTokenStore", lambda *_a, **_kw: _StubTokenStore(None)
    )
    cipher = CredentialCipher([fernet_key])
    writer = CapturingAuditWriter()
    with pytest.raises(CredentialNotFoundError):
        await sync_service.dry_run_sync(
            db_path,
            cipher=cipher,
            email="ghost@x",
            platform="moodle",
            audit_writer=writer,
        )
    assert _read_force_sync(db_path) == []


# ---------------------------------------------------------------------------
# cancel_sync
# ---------------------------------------------------------------------------


async def test_cancel_sync_happy_path_writes_terminal_row(db_path: Path) -> None:
    sync_id = "11111111-2222-3333-4444-555555555555"
    _insert_sync_row(db_path, sync_id=sync_id, state="running")
    cancels = CancelRegistry()
    bus = EventBus()
    writer = CapturingAuditWriter()

    result = await sync_service.cancel_sync(
        db_path,
        sync_id=sync_id,
        cancels=cancels,
        events=bus,
        audit_writer=writer,
        reason="stuck",
    )
    assert result.previous_state == "running"
    assert result.new_state == "cancelled"

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT state, error, finished_at, stats_json FROM sync_history WHERE id = ?",
            (sync_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    state, error, finished_at, stats_json = row
    assert state == "cancelled"
    assert error == "cancelled by operator"
    assert finished_at is not None
    assert stats_json is None
    # One audit row.
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "sync.cancel"
    assert writer.calls[0]["target"] == sync_id


async def test_cancel_sync_raises_when_id_unknown(db_path: Path) -> None:
    cancels = CancelRegistry()
    bus = EventBus()
    writer = CapturingAuditWriter()
    with pytest.raises(SyncNotFoundError):
        await sync_service.cancel_sync(
            db_path,
            sync_id="missing",
            cancels=cancels,
            events=bus,
            audit_writer=writer,
            reason="x",
        )
    assert writer.calls == []


async def test_cancel_sync_raises_state_conflict_when_terminal(db_path: Path) -> None:
    sync_id = "deadbeef-0000-0000-0000-000000000000"
    _insert_sync_row(db_path, sync_id=sync_id, state="complete")
    cancels = CancelRegistry()
    bus = EventBus()
    writer = CapturingAuditWriter()
    with pytest.raises(StateConflictError) as excinfo:
        await sync_service.cancel_sync(
            db_path,
            sync_id=sync_id,
            cancels=cancels,
            events=bus,
            audit_writer=writer,
            reason="late",
        )
    # The error string must include the offending state so the router
    # can pass it back as a 409 detail without re-querying.
    assert "complete" in str(excinfo.value)
    assert writer.calls == []


async def test_cancel_sync_publishes_ws_event(db_path: Path) -> None:
    """[TEST-COVERAGE] Pin the WS event semantics: ``syncs`` channel,
    event=``sync.cancelled``, sync_id present. Subscribe BEFORE the
    cancel so the in-memory bus has somewhere to deliver."""
    sync_id = "aaaa-bbbb-cccc-dddd"
    _insert_sync_row(db_path, sync_id=sync_id, state="running")
    cancels = CancelRegistry()
    bus = EventBus()
    writer = CapturingAuditWriter()

    received: list[dict[str, Any]] = []

    async def _consumer() -> None:
        async with bus.subscribe("syncs") as queue:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            received.append(event)

    consumer = asyncio.create_task(_consumer())
    # Yield once so the subscriber has registered before we publish.
    await asyncio.sleep(0)
    await sync_service.cancel_sync(
        db_path,
        sync_id=sync_id,
        cancels=cancels,
        events=bus,
        audit_writer=writer,
        reason="x",
    )
    await consumer

    assert len(received) == 1
    event = received[0]
    assert event["event"] == "sync.cancelled"
    assert event["sync_id"] == sync_id


# ---------------------------------------------------------------------------
# set_drain_mode + emergency_stop
# ---------------------------------------------------------------------------


async def test_set_drain_mode_flips_overlay_and_audits_first(db_path: Path) -> None:
    store = open_store(db_path)
    writer = CapturingAuditWriter()
    result = await sync_service.set_drain_mode(
        store=store, enabled=True, audit_writer=writer, reason="prep"
    )
    assert result.drain_mode is True
    assert result.previous is False
    assert store.get_overlay("scheduler.drain_mode") is True
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "scheduler.drain_set"


async def test_set_drain_mode_audit_fires_before_overlay_flip(db_path: Path) -> None:
    """[TEST-COVERAGE] Audit-first ordering — pin it. If the writer
    raises (it can't here, but it could in production), the overlay
    must NOT flip."""

    class _BoomWriter:
        async def write(self, **_kwargs: Any) -> int:
            raise RuntimeError("boom")

    store = open_store(db_path)
    with pytest.raises(RuntimeError):
        await sync_service.set_drain_mode(
            store=store, enabled=True, audit_writer=_BoomWriter(), reason=None
        )
    # Overlay must still hold the previous value (None / False).
    assert store.get_overlay("scheduler.drain_mode") in (None, False)


async def test_emergency_stop_sets_overlay(db_path: Path) -> None:
    store = open_store(db_path)
    writer = CapturingAuditWriter()
    await sync_service.emergency_stop(store=store, audit_writer=writer, reason="panic")
    assert store.get_overlay("scheduler.emergency_stop_requested") is True
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "scheduler.emergency_stop"


# ---------------------------------------------------------------------------
# bulk_refresh_teams
# ---------------------------------------------------------------------------


async def test_bulk_refresh_teams_enqueues_per_user(db_path: Path) -> None:
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="teams"),
            _row(user_email="bob@x", platform="teams"),
            # A non-teams row should not be touched.
            _row(user_email="alice@x", platform="moodle"),
        ]
    )
    writer = CapturingAuditWriter()
    count = await sync_service.bulk_refresh_teams(
        db_path, port=port, audit_writer=writer, reason="rotation"
    )
    assert count == 2
    enqueued = sorted(_read_force_sync(db_path))
    assert enqueued == [("alice@x", "teams"), ("bob@x", "teams")]
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "teams.bulk_refresh"
    assert writer.calls[0]["after"] == {"enqueued": 2}


async def test_bulk_refresh_teams_with_zero_users_emits_audit(db_path: Path) -> None:
    """[TEST-COVERAGE] No teams users -> count=0; audit row still lands
    so the operator's intent is preserved."""
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    count = await sync_service.bulk_refresh_teams(
        db_path, port=port, audit_writer=writer, reason=None
    )
    assert count == 0
    assert len(writer.calls) == 1
    assert writer.calls[0]["after"] == {"enqueued": 0}
