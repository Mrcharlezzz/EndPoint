"""Thin DI wrapper around :mod:`ingestion.daemon.store.audit`.

Services need to emit audit rows but should not import the storage
module directly: that would couple business logic to the SQLite path
and the persistence call style. The router builds an :class:`AuditWriter`
once per request (binding the database path and the resolved actor),
then hands the writer to the service it dispatches to.

Tests can substitute :class:`InMemoryAuditWriter` for assertions
without monkey-patching :mod:`audit_store`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ingestion.daemon.store import audit as audit_store

if TYPE_CHECKING:
    from pathlib import Path


class AuditWriter(Protocol):
    """Single-method writer for audit rows.

    The signature mirrors :func:`audit_store.write_entry` minus the
    ``db_path`` and ``actor`` kwargs (those are bound at construction
    so services don't need to know either).
    """

    async def write(
        self,
        *,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
    ) -> int:
        """Append one audit row. Returns the inserted row id."""
        ...


class SqliteAuditWriter:
    """Production :class:`AuditWriter` against the daemon's SQLite file."""

    def __init__(self, db_path: Path, actor: str) -> None:
        self._db_path = db_path
        self._actor = actor

    async def write(
        self,
        *,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
    ) -> int:
        return await audit_store.write_entry(
            self._db_path,
            actor=self._actor,
            action=action,
            target=target,
            before=before,
            after=after,
            reason=reason,
        )
