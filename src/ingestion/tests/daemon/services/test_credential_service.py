"""Coverage for :mod:`ingestion.daemon.services.credential_service`.

Every function (pause/unpause user, pause/unpause credential, mark
expired) is exercised:

* happy path returns the right ``PauseResult`` / ``MarkExpiredResult``
* port returning ``None`` -> typed ``NotFoundError`` raised
* exactly one audit row per destructive call (the count is the
  contract — bulk pause emits ONE row, not N)
* the audit row carries the documented ``before`` / ``after`` shape
  (``platforms`` list for bulk; per-row dict for credential-level)
"""

from __future__ import annotations

from typing import Any

import pytest
from ingestion.daemon.ports.credential_port import CredentialRow
from ingestion.daemon.services import credential_service
from ingestion.daemon.services.exceptions import (
    CredentialNotFoundError,
    UserNotFoundError,
)

# ---------------------------------------------------------------------------
# Fakes — frozen-dataclass-backed in-memory port + capturing audit writer
# ---------------------------------------------------------------------------


def _row(
    *,
    user_email: str = "alice@x",
    platform: str = "moodle",
    is_paused: bool = False,
    is_expired: bool = False,
) -> CredentialRow:
    return CredentialRow(
        user_email=user_email,
        platform=platform,
        is_paused=is_paused,
        is_expired=is_expired,
        expired_at=None,
        last_refreshed_at=None,
        encrypted_payload=b"\x00",
    )


class FakeCredentialPort:
    """In-memory implementation of :class:`CredentialPort`.

    Keeps rows in a dict keyed by ``(email, platform)``. Mutations
    return rows-affected just like the production store. Re-used by
    every service test in this directory.
    """

    def __init__(self, seed: list[CredentialRow] | None = None) -> None:
        self._rows: dict[tuple[str, str], CredentialRow] = {}
        for row in seed or ():
            self._rows[(row.user_email, row.platform)] = row

    async def get(self, email: str, platform: str) -> CredentialRow | None:
        return self._rows.get((email, platform))

    async def list_for_user(self, email: str) -> list[CredentialRow]:
        return [r for (e, _), r in self._rows.items() if e == email]

    async def list_for_platform(self, platform: str) -> list[CredentialRow]:
        return [r for (_, p), r in self._rows.items() if p == platform]

    async def set_paused(self, email: str, platform: str, paused: bool) -> int:
        row = self._rows.get((email, platform))
        if row is None or row.is_paused == paused:
            return 0
        self._rows[(email, platform)] = _replace(row, is_paused=paused)
        return 1

    async def set_paused_bulk_for_user(self, email: str, paused: bool) -> int:
        affected = 0
        for key, row in list(self._rows.items()):
            if key[0] != email or row.is_paused == paused:
                continue
            self._rows[key] = _replace(row, is_paused=paused)
            affected += 1
        return affected

    async def set_expired(self, email: str, platform: str, expired: bool) -> int:
        row = self._rows.get((email, platform))
        if row is None or row.is_expired == expired:
            return 0
        self._rows[(email, platform)] = _replace(row, is_expired=expired)
        return 1


def _replace(row: CredentialRow, **changes: Any) -> CredentialRow:
    """Functional update — frozen dataclass requires this. Used by the
    fake port to simulate ``aupdate``."""
    base: dict[str, Any] = {
        "user_email": row.user_email,
        "platform": row.platform,
        "is_paused": row.is_paused,
        "is_expired": row.is_expired,
        "expired_at": row.expired_at,
        "last_refreshed_at": row.last_refreshed_at,
        "encrypted_payload": row.encrypted_payload,
    }
    base.update(changes)
    return CredentialRow(**base)


class CapturingAuditWriter:
    """In-memory :class:`AuditWriter`. Captures every write so tests can
    assert the call shape and count."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def write(
        self,
        *,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
    ) -> int:
        self.calls.append(
            {
                "action": action,
                "target": target,
                "before": before,
                "after": after,
                "reason": reason,
            }
        )
        return len(self.calls)


# ---------------------------------------------------------------------------
# pause_user
# ---------------------------------------------------------------------------


async def test_pause_user_flips_every_credential() -> None:
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle"),
            _row(user_email="alice@x", platform="teams"),
        ]
    )
    writer = CapturingAuditWriter()
    result = await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason="x")
    assert result.rows_affected == 2
    assert result.rows_total == 2
    rows = await port.list_for_user("alice@x")
    assert all(r.is_paused for r in rows)


async def test_pause_user_emits_one_audit_row_not_n() -> None:
    """The CONTRACT (per slice spec): one bulk audit row per call,
    regardless of how many credentials were updated. Pin it."""
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle"),
            _row(user_email="alice@x", platform="teams"),
            _row(user_email="alice@x", platform="telegram"),
        ]
    )
    writer = CapturingAuditWriter()
    await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason="triage")
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "user.pause"
    assert call["target"] == "alice@x"
    # before snapshot carries one entry per credential (matches the
    # legacy shape so dashboard diff views keep rendering).
    assert call["before"] == {
        "platforms": [
            {"platform": "moodle", "is_paused": False},
            {"platform": "teams", "is_paused": False},
            {"platform": "telegram", "is_paused": False},
        ]
    }
    # after carries the post-mutation state plus rows_updated.
    after = call["after"]
    assert after["rows_updated"] == 3
    assert all(p["is_paused"] for p in after["platforms"])


async def test_pause_user_raises_user_not_found_when_no_credentials() -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(UserNotFoundError):
        await credential_service.pause_user(port, "ghost@x", audit_writer=writer, reason="x")
    # No audit row when the operation didn't run.
    assert writer.calls == []


async def test_pause_user_idempotent_returns_zero_rows_but_writes_audit() -> None:
    """All credentials already paused -> rows_affected=0, but the audit
    row still lands so operator intent is recorded."""
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle", is_paused=True)])
    writer = CapturingAuditWriter()
    result = await credential_service.pause_user(port, "alice@x", audit_writer=writer, reason=None)
    assert result.rows_affected == 0
    assert result.rows_total == 1
    assert len(writer.calls) == 1


# ---------------------------------------------------------------------------
# unpause_user
# ---------------------------------------------------------------------------


async def test_unpause_user_inverse_of_pause_user() -> None:
    port = FakeCredentialPort(
        [
            _row(user_email="alice@x", platform="moodle", is_paused=True),
            _row(user_email="alice@x", platform="teams", is_paused=True),
        ]
    )
    writer = CapturingAuditWriter()
    result = await credential_service.unpause_user(
        port, "alice@x", audit_writer=writer, reason=None
    )
    assert result.rows_affected == 2
    rows = await port.list_for_user("alice@x")
    assert not any(r.is_paused for r in rows)


async def test_unpause_user_emits_one_audit_row() -> None:
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle", is_paused=True)])
    writer = CapturingAuditWriter()
    await credential_service.unpause_user(port, "alice@x", audit_writer=writer, reason="resume")
    assert len(writer.calls) == 1
    assert writer.calls[0]["action"] == "user.unpause"


async def test_unpause_user_raises_user_not_found_when_no_credentials() -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(UserNotFoundError):
        await credential_service.unpause_user(port, "ghost@x", audit_writer=writer, reason=None)
    assert writer.calls == []


# ---------------------------------------------------------------------------
# pause_credential / unpause_credential
# ---------------------------------------------------------------------------


async def test_pause_credential_happy_path() -> None:
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle")])
    writer = CapturingAuditWriter()
    result = await credential_service.pause_credential(
        port, "alice@x", "moodle", audit_writer=writer, reason="triage"
    )
    assert result.rows_affected == 1
    assert result.rows_total == 1
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "credential.pause"
    assert call["target"] == "alice@x/moodle"
    assert call["before"] == {"is_paused": False}
    assert call["after"] == {"is_paused": True}


async def test_pause_credential_idempotent_writes_audit() -> None:
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle", is_paused=True)])
    writer = CapturingAuditWriter()
    result = await credential_service.pause_credential(
        port, "alice@x", "moodle", audit_writer=writer, reason=None
    )
    assert result.rows_affected == 0
    assert len(writer.calls) == 1


async def test_pause_credential_raises_when_missing() -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(CredentialNotFoundError):
        await credential_service.pause_credential(
            port, "ghost@x", "moodle", audit_writer=writer, reason=None
        )
    assert writer.calls == []


async def test_unpause_credential_happy_path() -> None:
    port = FakeCredentialPort([_row(user_email="alice@x", platform="teams", is_paused=True)])
    writer = CapturingAuditWriter()
    result = await credential_service.unpause_credential(
        port, "alice@x", "teams", audit_writer=writer, reason="ok"
    )
    assert result.rows_affected == 1
    assert writer.calls[0]["action"] == "credential.unpause"


async def test_unpause_credential_raises_when_missing() -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(CredentialNotFoundError):
        await credential_service.unpause_credential(
            port, "ghost@x", "teams", audit_writer=writer, reason=None
        )
    assert writer.calls == []


# ---------------------------------------------------------------------------
# mark_credential_expired
# ---------------------------------------------------------------------------


async def test_mark_credential_expired_flips_flag_and_writes_audit() -> None:
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle")])
    writer = CapturingAuditWriter()
    result = await credential_service.mark_credential_expired(
        port, "alice@x", "moodle", audit_writer=writer, reason="revoked"
    )
    assert result.rows_affected == 1
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["action"] == "credential.mark_expired"
    assert call["target"] == "alice@x/moodle"
    assert call["before"] == {"is_expired": False}
    assert call["after"] == {"is_expired": True, "rows_updated": 1}
    # And the actual store flipped.
    row = await port.get("alice@x", "moodle")
    assert row is not None
    assert row.is_expired is True


async def test_mark_credential_expired_raises_when_missing() -> None:
    port = FakeCredentialPort()
    writer = CapturingAuditWriter()
    with pytest.raises(CredentialNotFoundError):
        await credential_service.mark_credential_expired(
            port, "ghost@x", "moodle", audit_writer=writer, reason=None
        )
    assert writer.calls == []


async def test_mark_credential_expired_idempotent_writes_audit() -> None:
    """[TEST-COVERAGE] Re-call on already-expired row -> rows_affected=0
    but audit row still lands. Mirror of pause_credential's idempotence
    contract."""
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle", is_expired=True)])
    writer = CapturingAuditWriter()
    result = await credential_service.mark_credential_expired(
        port, "alice@x", "moodle", audit_writer=writer, reason=None
    )
    assert result.rows_affected == 0
    assert len(writer.calls) == 1


# ---------------------------------------------------------------------------
# Cross-cutting: reason propagation
# ---------------------------------------------------------------------------


async def test_reason_propagates_to_every_action() -> None:
    """The reason kwarg is what an operator typed in the dashboard;
    every audit row must carry it verbatim. Regression-prone: a
    refactor that drops it from the kwargs would silently strip
    operator context."""
    port = FakeCredentialPort([_row(user_email="alice@x", platform="moodle")])
    writer = CapturingAuditWriter()
    await credential_service.pause_credential(
        port, "alice@x", "moodle", audit_writer=writer, reason="why-this-1"
    )
    await credential_service.unpause_credential(
        port, "alice@x", "moodle", audit_writer=writer, reason="why-this-2"
    )
    await credential_service.mark_credential_expired(
        port, "alice@x", "moodle", audit_writer=writer, reason="why-this-3"
    )
    assert [c["reason"] for c in writer.calls] == [
        "why-this-1",
        "why-this-2",
        "why-this-3",
    ]
