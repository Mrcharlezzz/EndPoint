from __future__ import annotations

from django.db import DataError, DatabaseError, IntegrityError, InterfaceError
from django.db import OperationalError, ProgrammingError

from processing.exceptions import (
    PersistenceError,
    RowScopedPersistenceError,
    TransientPersistenceError,
)

TRANSIENT_SQLSTATES = {
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "55P03",  # lock_not_available
}

TRANSIENT_SQLSTATE_PREFIXES = ("08",)


def get_sqlstate(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))

        pgcode = getattr(current, "pgcode", None)
        if isinstance(pgcode, str) and pgcode:
            return pgcode

        diag = getattr(current, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
        if isinstance(sqlstate, str) and sqlstate:
            return sqlstate

        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return None


def translate_persistence_error(exc: Exception) -> PersistenceError:
    if isinstance(exc, PersistenceError):
        return exc
    if isinstance(exc, (OperationalError, InterfaceError)):
        return TransientPersistenceError(str(exc))
    if isinstance(exc, (DataError, IntegrityError, ProgrammingError)):
        return RowScopedPersistenceError(str(exc))
    if isinstance(exc, DatabaseError):
        sqlstate = get_sqlstate(exc)
        if sqlstate in TRANSIENT_SQLSTATES or (
            sqlstate is not None
            and any(
                sqlstate.startswith(prefix) for prefix in TRANSIENT_SQLSTATE_PREFIXES
            )
        ):
            return TransientPersistenceError(str(exc))
        return RowScopedPersistenceError(str(exc))
    return RowScopedPersistenceError(str(exc))
