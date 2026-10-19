"""Typed credential-CRUD port.

Routers and services depend on the :class:`CredentialPort` Protocol — never
on the Django ORM directly. The single concrete implementation lives in
:mod:`ingestion.daemon.ports.credential_store`. Centralising the ORM access
keeps Django coupling on one well-known seam: every other module in the
daemon talks shape-stable, side-effect-free dataclasses.

``CredentialRow`` is intentionally a small read-only mirror of
``PlatformCredential`` — only the fields the daemon actually consults
(pause flag, expiry flag, expiry timestamp, the encrypted payload byte
string). Tests can hand-roll instances; nothing on the row depends on
Django being initialised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CredentialRow:
    """Snapshot of a single ``PlatformCredential`` row.

    Frozen + slotted so tests can compare instances by value and
    accidental mutation raises ``FrozenInstanceError``. Mirrors the
    columns daemon code actually reads — display_name and refresh_url
    are intentionally omitted because no service needs them yet.
    """

    user_email: str
    platform: str
    is_paused: bool
    is_expired: bool
    expired_at: str | None
    last_refreshed_at: str | None
    encrypted_payload: bytes


class CredentialPort(Protocol):
    """Port for reading and mutating per-platform credential rows.

    The implementation owns transactional semantics. Callers pass
    primitives (email, platform, booleans) and read snapshots back as
    :class:`CredentialRow`. ``set_*`` methods return rows-affected so a
    caller can detect a no-op (already in the requested state).
    """

    async def get(self, email: str, platform: str) -> CredentialRow | None:
        """Return one credential row, or ``None`` when no row exists."""
        ...

    async def list_for_user(self, email: str) -> list[CredentialRow]:
        """Return every credential row for ``email``. Empty list ≠ error."""
        ...

    async def list_for_platform(self, platform: str) -> list[CredentialRow]:
        """Return every credential row for ``platform`` across all users."""
        ...

    async def set_paused(self, email: str, platform: str, paused: bool) -> int:
        """Set ``is_paused`` for one row. Returns rows-actually-updated.

        Filters on the inverse of ``paused`` so an idempotent re-call
        returns 0. The caller should branch on the return value when it
        needs to know whether a state transition occurred.
        """
        ...

    async def set_paused_bulk_for_user(self, email: str, paused: bool) -> int:
        """Set ``is_paused`` across every credential for ``email``.

        One UPDATE statement; atomic at the database level. Returns the
        number of rows whose state actually flipped.
        """
        ...

    async def set_expired(self, email: str, platform: str, expired: bool) -> int:
        """Set ``is_expired`` for one row. Returns rows-actually-updated.

        Mirrors :meth:`set_paused` semantics: idempotent, returns 0 when
        the row was already in the requested state.
        """
        ...
