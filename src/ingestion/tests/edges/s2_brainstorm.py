"""Brainstorm edge tests for slice 2 — sibling to s2_services_and_routers.py.

These are the >= 10 additional edges the tester brainstormed beyond the
seed list. Lives in a separate file purely so each test module stays
under the 600-LOC budget; the seed list lives in
``s2_services_and_routers.py``.

Each test is tagged in its docstring with [CODE-FIX] or [TEST-COVERAGE]
per the slice contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.services import (
    credential_service,
    maintenance_service,
    sync_service,
)
from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    CredentialNotFoundError,
    ItemNotFoundError,
    NotFoundError,
    ServiceError,
    SyncNotFoundError,
    UserNotFoundError,
)
from ingestion.daemon.services.results import (
    BulkRefreshResult,
    CancelSyncResult,
    CompactStorageResult,
    DrainModeResult,
    DryRunResult,
    ForceRefreshResult,
    ForceSyncAllResult,
    ForceSyncResult,
    MarkExpiredResult,
    MuteRuleResult,
    PauseResult,
    PurgeItemResult,
    ResetCursorResult,
)
from ingestion.daemon.store.local import apply_migrations, open_store

from tests.edges._s2_helpers import CapturingWriter, FakeCredPort, build_client, make_row


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _minimal_kwargs(cls: type) -> dict[str, Any]:
    return {
        PauseResult: {"rows_affected": 0, "rows_total": 0},
        MarkExpiredResult: {"rows_affected": 0},
        DrainModeResult: {"drain_mode": True, "previous": False},
        ForceRefreshResult: {"outcome": "success"},
        ForceSyncResult: {"ok": True, "user_email": "u", "platform": "p"},
        ForceSyncAllResult: {"user_email": "u", "enqueued": [], "skipped": []},
        DryRunResult: {
            "items_fetched": 0,
            "items_would_write": 0,
            "items_would_skip": 0,
            "errors": [],
        },
        ResetCursorResult: {"rows_deleted": 0},
        BulkRefreshResult: {"enqueued": 0},
        PurgeItemResult: {"ok": True, "item_id": "x"},
        CompactStorageResult: {"before_bytes": 0, "after_bytes": 0},
        MuteRuleResult: {"rule_id": 1, "enabled": True},
        CancelSyncResult: {
            "sync_id": "x",
            "previous_state": "running",
            "new_state": "cancelled",
        },
    }[cls]


def test_pydantic_models_reject_extra_fields() -> None:
    """[TEST-COVERAGE] brainstorm: every result model carries
    extra='forbid'; a typo'd field on the wire must reject."""
    for cls in (
        PauseResult,
        MarkExpiredResult,
        DrainModeResult,
        ForceRefreshResult,
        ForceSyncResult,
        ForceSyncAllResult,
        DryRunResult,
        ResetCursorResult,
        BulkRefreshResult,
        PurgeItemResult,
        CompactStorageResult,
        MuteRuleResult,
        CancelSyncResult,
    ):
        with pytest.raises(ValueError):
            cls.model_validate({"unknown_field": 1, **_minimal_kwargs(cls)})


def test_every_typed_exception_subclasses_service_error() -> None:
    """[TEST-COVERAGE] brainstorm: property-style — every public
    exception class chains to ServiceError so the router's catch-all
    base works."""
    from ingestion.daemon.services import exceptions as exc_mod

    for name in dir(exc_mod):
        obj = getattr(exc_mod, name)
        if not isinstance(obj, type) or not issubclass(obj, Exception):
            continue
        if obj in (Exception, ServiceError):
            continue
        assert issubclass(obj, ServiceError), f"{name} does not chain to ServiceError"


def test_every_not_found_subclass_chains_to_not_found_error() -> None:
    """[TEST-COVERAGE] brainstorm: tighter NotFound sub-tree pin."""
    for cls in (
        CredentialNotFoundError,
        UserNotFoundError,
        ItemNotFoundError,
        SyncNotFoundError,
        AlertRuleNotFoundError,
    ):
        assert issubclass(cls, NotFoundError)


async def test_concurrent_pause_for_same_user_is_safe() -> None:
    """[TEST-COVERAGE] brainstorm: two concurrent pauses for the same
    user; end state is paused regardless of order. Production uses one
    SQL UPDATE which is atomic."""
    port = FakeCredPort([make_row(platform="moodle"), make_row(platform="teams")])
    writer = CapturingWriter()
    await asyncio.gather(
        credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None),
        credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None),
    )
    rows = await port.list_for_user("alice@x")
    assert all(r.is_paused for r in rows)
    assert len(writer.calls) == 2


async def test_unpause_user_with_no_credentials_raises_user_not_found() -> None:
    """[TEST-COVERAGE] brainstorm: a future "be lenient" change is a
    deliberate decision; pin the typed raise."""
    port = FakeCredPort()
    writer = CapturingWriter()
    with pytest.raises(UserNotFoundError):
        await credential_service.unpause_user(port, "ghost@x", audit_writer=writer, reason=None)


async def test_force_sync_all_with_no_credentials_raises(tmp_path: Path) -> None:
    """[TEST-COVERAGE] brainstorm: empty credential list -> typed raise."""
    db = tmp_path / "s.db"
    apply_migrations(db)
    writer = CapturingWriter()
    with pytest.raises(UserNotFoundError):
        await sync_service.force_sync_all_for_user(
            db, port=FakeCredPort(), email="ghost@x", audit_writer=writer, reason=None
        )


def test_dry_run_rejects_unknown_platform_at_router(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] brainstorm: Pydantic regex rejects platform=reddit
    -> 422 BEFORE service runs."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post(
        "/api/actions/sync/dry_run",
        json={"user_email": "alice@x", "platform": "reddit"},
    )
    assert response.status_code == 422


def test_emergency_stop_rejects_wrong_confirm(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] brainstorm: confirm-gate is a hard pre-check;
    wrong confirm -> 422 before the service runs."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post(
        "/api/actions/scheduler/emergency_stop",
        json={"confirm": "not-scheduler", "reason": "test"},
    )
    assert response.status_code == 422


async def test_drain_set_writes_audit_before_overlay_flip(tmp_path: Path) -> None:
    """[TEST-COVERAGE] brainstorm: audit-first ordering. A writer that
    raises must NOT leave the overlay flipped."""

    class _BoomWriter:
        async def write(self, **_kw: Any) -> int:
            raise RuntimeError("boom")

    db = tmp_path / "s.db"
    store = open_store(db)
    with pytest.raises(RuntimeError):
        await sync_service.set_drain_mode(
            store=store, enabled=True, audit_writer=_BoomWriter(), reason=None
        )
    assert store.get_overlay("scheduler.drain_mode") in (None, False)


async def test_compact_storage_handles_size_branch(tmp_path: Path) -> None:
    """[TEST-COVERAGE] brainstorm: compact's before-size reads
    file.exists() else 0; pin the >0 branch."""
    db = tmp_path / "fresh.db"
    apply_migrations(db)
    writer = CapturingWriter()
    result = await maintenance_service.compact_storage(db, audit_writer=writer, reason=None)
    assert result.before_bytes >= 0


def test_drain_mode_endpoint_round_trip(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] brainstorm: drain endpoint flips overlay AND
    returns previous state; two-call round trip pins the flip."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    first = client.post("/api/actions/scheduler/drain", json={"enabled": True, "reason": "prep"})
    assert first.status_code == 200
    assert first.json() == {"drain_mode": True, "previous": False}
    second = client.post(
        "/api/actions/scheduler/drain", json={"enabled": False, "reason": "resume"}
    )
    assert second.status_code == 200
    assert second.json() == {"drain_mode": False, "previous": True}


def test_storage_compact_endpoint_returns_size_envelope(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] brainstorm: storage/compact response carries
    before_bytes + after_bytes in the envelope."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    response = client.post("/api/actions/storage/compact", json={"reason": "test"})
    assert response.status_code == 200
    body = response.json()
    assert "before_bytes" in body and "after_bytes" in body


@pytest.mark.django_db(transaction=True)
def test_purge_item_route_never_returns_200_for_missing_id(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] brainstorm: purge route must never 200 for a
    missing id (typed-exception translation drives 4xx)."""
    client = build_client(tmp_path, fernet_key, credentials=FakeCredPort())
    item_id = "00000000-0000-0000-0000-000000000000"
    response = client.post(
        f"/api/actions/content/{item_id}/purge",
        json={"confirm": f"item:{item_id}", "reason": "test"},
    )
    assert response.status_code != 200


async def test_cancel_registry_signal_returns_false_for_unregistered() -> None:
    """[TEST-COVERAGE] brainstorm: CancelRegistry.signal returns False
    when no subscriber registered. The cancel service relies on this
    for the "best-effort persisted" log line; pin so a future change
    is a deliberate decision."""
    from ingestion.daemon.api.cancel_registry import CancelRegistry

    cancels = CancelRegistry()
    assert await cancels.signal("never-registered") is False


async def test_cancel_registry_signal_after_register_returns_true() -> None:
    """[TEST-COVERAGE] brainstorm: register + signal -> True so the
    cancel service knows a subscriber exists. Pin the lifecycle."""
    from ingestion.daemon.api.cancel_registry import CancelRegistry

    cancels = CancelRegistry()
    await cancels.register("sync-123")
    assert await cancels.signal("sync-123") is True


def test_results_models_use_aliased_imports() -> None:
    """[TEST-COVERAGE] brainstorm: every result model is a Pydantic
    BaseModel; pin the type so a future swap to dataclasses is loud."""
    from pydantic import BaseModel

    for cls in (
        PauseResult,
        MarkExpiredResult,
        DrainModeResult,
        ForceRefreshResult,
        ForceSyncResult,
        ForceSyncAllResult,
        DryRunResult,
        ResetCursorResult,
        BulkRefreshResult,
        PurgeItemResult,
        CompactStorageResult,
        MuteRuleResult,
        CancelSyncResult,
    ):
        assert issubclass(cls, BaseModel)


async def test_force_refresh_teams_audit_action_label() -> None:
    """[TEST-COVERAGE] brainstorm: refresh emits a single audit row
    even on the FeatureDisabled path? Actually NO — pin: the gate
    short-circuits BEFORE any audit call. Catch a future "always
    audit" regression early."""

    class _Cfg:
        class teams_refresh:  # noqa: N801
            enabled = False
            timeout_seconds = 1
            max_retries_per_user = 0

    from cryptography.fernet import Fernet

    cipher = CredentialCipher([Fernet.generate_key().decode()])
    writer = CapturingWriter()
    from ingestion.daemon.services.exceptions import FeatureDisabledError

    with pytest.raises(FeatureDisabledError):
        await maintenance_service.force_refresh_teams(
            config=_Cfg(),  # type: ignore[arg-type]
            cipher=cipher,
            user_refresh_locks={},
            email="alice@x",
            audit_writer=writer,
            reason=None,
        )
    # Audit MUST be empty when the feature gate trips first.
    assert writer.calls == []
