"""Concrete :class:`CredentialPort` backed by the Django ORM.

This module is the *only* place in the daemon allowed to import
``core.models.PlatformCredential`` for credential CRUD. Every other
service / router goes through the :class:`CredentialPort` protocol so the
ORM dependency is contained on a single seam.

Imports of ``core.models`` are lazy: each method imports inside its body
so the module can be loaded before ``core.bootstrap.init()`` ran. This
matches the convention used by the existing Postgres-backed ports
(:mod:`ingestion.daemon.ports.token_store`, :mod:`.candidate_source`).
"""

from __future__ import annotations

from typing import Any

from ingestion.daemon.ports.credential_port import CredentialRow


def _to_iso(value: Any) -> str | None:
    """Datetime -> ISO-8601 string, or ``None`` if the column was NULL.

    Typed as ``Any`` because callers hand it Django-stub field types that
    pyright can't see through (the runtime value is always
    ``datetime | None`` and ``isoformat`` is the only attribute we touch).
    """
    if value is None:
        return None
    return value.isoformat()


class DjangoCredentialStore:
    """Concrete :class:`CredentialPort` against ``PlatformCredential``.

    The class deliberately exposes only the surface declared on
    :class:`CredentialPort`. ``set_*`` calls filter on the *inverse* of
    the desired state so an idempotent re-call updates zero rows.
    """

    async def get(self, email: str, platform: str) -> CredentialRow | None:
        from core.models import PlatformCredential

        try:
            row = await PlatformCredential.objects.aget(
                user_email=email,
                platform=platform,
            )
        except PlatformCredential.DoesNotExist:
            return None
        return self._row_to_dataclass(row)

    async def list_for_user(self, email: str) -> list[CredentialRow]:
        from core.models import PlatformCredential

        rows: list[CredentialRow] = []
        async for row in PlatformCredential.objects.filter(user_email=email).aiterator():
            rows.append(self._row_to_dataclass(row))
        return rows

    async def list_for_platform(self, platform: str) -> list[CredentialRow]:
        from core.models import PlatformCredential

        rows: list[CredentialRow] = []
        async for row in PlatformCredential.objects.filter(platform=platform).aiterator():
            rows.append(self._row_to_dataclass(row))
        return rows

    async def set_paused(self, email: str, platform: str, paused: bool) -> int:
        from core.models import PlatformCredential

        # Filter on the inverse so an already-in-state row updates 0 — the
        # caller can branch on the rowcount to detect a real transition.
        return await PlatformCredential.objects.filter(
            user_email=email,
            platform=platform,
            is_paused=not paused,
        ).aupdate(is_paused=paused)

    async def set_paused_bulk_for_user(self, email: str, paused: bool) -> int:
        from core.models import PlatformCredential

        # One SQL UPDATE — atomic at the DB level. Replaces the previous
        # per-credential aiterator + per-row aupdate fan-out.
        return await PlatformCredential.objects.filter(
            user_email=email,
            is_paused=not paused,
        ).aupdate(is_paused=paused)

    async def set_expired(self, email: str, platform: str, expired: bool) -> int:
        from core.models import PlatformCredential

        return await PlatformCredential.objects.filter(
            user_email=email,
            platform=platform,
            is_expired=not expired,
        ).aupdate(is_expired=expired)

    @staticmethod
    def _row_to_dataclass(row: Any) -> CredentialRow:
        # Each ORM row is mapped to the slim, frozen mirror that callers
        # downstream consume. The ORM instance itself never leaks past
        # this method. Typed ``Any`` because the Django stubs surface
        # field types (BinaryField, DateTimeField) instead of runtime
        # values when pyright sees an explicit annotation.
        return CredentialRow(
            user_email=str(row.user_email),
            platform=str(row.platform),
            is_paused=bool(row.is_paused),
            is_expired=bool(row.is_expired),
            expired_at=_to_iso(row.expired_at),
            last_refreshed_at=_to_iso(row.last_refreshed_at),
            encrypted_payload=bytes(row.encrypted_payload),
        )
