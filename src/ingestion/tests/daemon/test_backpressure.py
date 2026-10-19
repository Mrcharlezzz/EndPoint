"""Backpressure on the force-sync queue (slice 10).

Pins the new ``max_queue_depth`` knob in
:mod:`ingestion.daemon.services.sync_service`:

* ``force_sync_one`` raises :class:`BackpressureError` when the current
  queue depth is at or above the cap;
* ``force_sync_one`` succeeds (no-op for the check) when the depth is
  under the cap;
* ``force_sync_all_for_user`` enqueues partial rows then raises when
  the cap is hit mid-loop and writes an audit row that lists what
  landed before the rejection;
* a cap of ``0`` or ``None`` disables the check (operator opt-out);
* the :class:`BackpressureError` exception translates to a 503 +
  ``code: rate_limited`` envelope per the slice 4 wire shape;
* the carried ``queue_depth`` and ``max_depth`` ride through to the
  envelope's message.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.force_sync import enqueue_force_sync, force_sync_queue_depth
from ingestion.daemon.ports.credential_port import CredentialRow
from ingestion.daemon.services import sync_service
from ingestion.daemon.services.exceptions import BackpressureError
from ingestion.daemon.store.local import apply_migrations

from tests.daemon.services.test_credential_service import (
    CapturingAuditWriter,
    FakeCredentialPort,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


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


def _read_force_sync(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT user_email, platform FROM force_sync_requests ORDER BY user_email, platform"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# force_sync_queue_depth helper
# ---------------------------------------------------------------------------


async def test_force_sync_queue_depth_returns_zero_when_empty(db_path: Path) -> None:
    """A fresh queue reports depth 0."""
    assert await force_sync_queue_depth(db_path) == 0


async def test_force_sync_queue_depth_counts_rows(db_path: Path) -> None:
    """Each enqueue bumps the depth by one (until the same key conflicts
    via the upsert clause)."""
    await enqueue_force_sync(db_path, user_email="alice@x", platform="moodle")
    assert await force_sync_queue_depth(db_path) == 1
    await enqueue_force_sync(db_path, user_email="bob@x", platform="moodle")
    assert await force_sync_queue_depth(db_path) == 2


async def test_force_sync_queue_depth_dedupes_via_upsert(db_path: Path) -> None:
    """Same (user, platform) -> upsert, depth unchanged. Pin so the cap
    isn't bypassable by repeated submits for one credential."""
    await enqueue_force_sync(db_path, user_email="alice@x", platform="moodle")
    await enqueue_force_sync(db_path, user_email="alice@x", platform="moodle")
    assert await force_sync_queue_depth(db_path) == 1


# ---------------------------------------------------------------------------
# force_sync_one backpressure
# ---------------------------------------------------------------------------


async def test_force_sync_one_below_cap_succeeds(db_path: Path) -> None:
    """Depth 0, cap 5 -> insert lands, no rejection."""
    writer = CapturingAuditWriter()
    result = await sync_service.force_sync_one(
        db_path,
        email="alice@x",
        platform="moodle",
        audit_writer=writer,
        reason="ok",
        max_queue_depth=5,
    )
    assert result.ok is True
    assert _read_force_sync(db_path) == [("alice@x", "moodle")]


async def test_force_sync_one_at_cap_raises_backpressure(db_path: Path) -> None:
    """Depth 3, cap 3 -> next insert rejected with the cap-exceeded
    payload populated."""
    # Pre-fill the queue.
    await enqueue_force_sync(db_path, user_email="u1@x", platform="moodle")
    await enqueue_force_sync(db_path, user_email="u2@x", platform="moodle")
    await enqueue_force_sync(db_path, user_email="u3@x", platform="moodle")
    writer = CapturingAuditWriter()
    with pytest.raises(BackpressureError) as excinfo:
        await sync_service.force_sync_one(
            db_path,
            email="alice@x",
            platform="moodle",
            audit_writer=writer,
            reason="overload",
            max_queue_depth=3,
        )
    err = excinfo.value
    assert err.queue_depth == 3
    assert err.max_depth == 3
    # Nothing inserted; no audit row.
    assert ("alice@x", "moodle") not in _read_force_sync(db_path)
    assert writer.calls == []


async def test_force_sync_one_with_cap_zero_disables(db_path: Path) -> None:
    """Cap of 0 is the operator opt-out — rejections never fire."""
    writer = CapturingAuditWriter()
    # Pre-fill with many rows; cap=0 should not reject.
    for i in range(10):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")
    result = await sync_service.force_sync_one(
        db_path,
        email="alice@x",
        platform="moodle",
        audit_writer=writer,
        reason="bypass",
        max_queue_depth=0,
    )
    assert result.ok is True


async def test_force_sync_one_with_cap_none_disables(db_path: Path) -> None:
    """Cap of None disables (default) — rejections never fire."""
    writer = CapturingAuditWriter()
    for i in range(10):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")
    result = await sync_service.force_sync_one(
        db_path,
        email="alice@x",
        platform="moodle",
        audit_writer=writer,
        reason="bypass",
        max_queue_depth=None,
    )
    assert result.ok is True


# ---------------------------------------------------------------------------
# force_sync_all_for_user backpressure
# ---------------------------------------------------------------------------


async def test_force_sync_all_partial_enqueue_then_backpressure(db_path: Path) -> None:
    """5 platforms requested, queue cap reached after 3 inserts. The
    remainder rejects with BackpressureError; the audit row records
    the partial enqueue."""
    # Pre-fill 5 unrelated rows so the cap=8 will trip after 3 of
    # alice's platforms land (depth: 5 -> 6 -> 7 -> 8 ... reject at
    # the 4th alice attempt). We seed 5, cap=8: insert moodle (6),
    # teams (7), telegram (8), then the 4th rejects.
    for i in range(5):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle"),
            _row(user_email="alice@x", platform="teams"),
            _row(user_email="alice@x", platform="telegram"),
            _row(user_email="alice@x", platform="extra1"),
            _row(user_email="alice@x", platform="extra2"),
        ],
    )
    writer = CapturingAuditWriter()
    with pytest.raises(BackpressureError) as excinfo:
        await sync_service.force_sync_all_for_user(
            db_path,
            port=port,
            email="alice@x",
            audit_writer=writer,
            reason="rotation",
            max_queue_depth=8,
        )
    err = excinfo.value
    assert err.max_depth == 8

    # Audit row written before the raise — captures the partial
    # enqueue. enqueued has 3 alice platforms; skipped is empty; one
    # row in audit despite the partial.
    assert len(writer.calls) == 1
    audit = writer.calls[0]
    assert audit["action"] == "sync.force_requested_all"
    assert audit["target"] == "alice@x"
    enqueued = audit["after"]["enqueued"]
    assert len(enqueued) == 3
    # Only the first three made it.
    assert set(enqueued) == {"moodle", "teams", "telegram"}
    # The audit row carries the depth at rejection.
    assert audit["after"]["rejected_at"] == 8


async def test_force_sync_all_no_cap_enqueues_all_platforms(db_path: Path) -> None:
    """Without a cap, every credential lands and the audit row carries
    the full enqueued set."""
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle"),
            _row(user_email="alice@x", platform="teams"),
        ],
    )
    writer = CapturingAuditWriter()
    result = await sync_service.force_sync_all_for_user(
        db_path,
        port=port,
        email="alice@x",
        audit_writer=writer,
        reason=None,
        max_queue_depth=None,
    )
    assert sorted(result.enqueued) == ["moodle", "teams"]


async def test_force_sync_all_partial_then_backpressure_with_no_prior_enqueue(
    db_path: Path,
) -> None:
    """If the cap rejects on the FIRST platform (no enqueue lands first)
    no audit row is written — the audit row is only emitted when a
    partial enqueue exists."""
    # Pre-fill exactly cap rows so the very first alice insert rejects.
    for i in range(3):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle"),
            _row(user_email="alice@x", platform="teams"),
        ],
    )
    writer = CapturingAuditWriter()
    with pytest.raises(BackpressureError):
        await sync_service.force_sync_all_for_user(
            db_path,
            port=port,
            email="alice@x",
            audit_writer=writer,
            reason="rotation",
            max_queue_depth=3,
        )
    # No partial enqueue -> no audit row.
    assert writer.calls == []


# ---------------------------------------------------------------------------
# Translator: BackpressureError -> 503 + rate_limited
# ---------------------------------------------------------------------------


def test_backpressure_translates_to_503_with_rate_limited_code() -> None:
    """Slice 4 envelope shape: 503 + ``code: rate_limited``, message
    carries the depth + cap so the dashboard can render an
    informative banner."""
    err = BackpressureError(queue_depth=10, max_depth=10)
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 503
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "rate_limited"
    # Message carries the structured payload.
    assert "10" in exc.detail["message"]
    # Hint mentions the operator can retry once the queue drains.
    assert "retry" in (exc.detail.get("hint") or "").lower()


def test_backpressure_message_carries_depth_and_cap() -> None:
    """``str(err)`` carries both numbers so the audit log + envelope
    surface the cap context without parsing."""
    err = BackpressureError(queue_depth=42, max_depth=10)
    assert "42" in str(err)
    assert "10" in str(err)


def test_backpressure_constructor_is_kw_only() -> None:
    """``BackpressureError`` constructor enforces keyword-only args so
    callers can't accidentally swap the queue_depth and max_depth
    positions."""
    with pytest.raises(TypeError):
        BackpressureError(10, 5)  # type: ignore[misc]
