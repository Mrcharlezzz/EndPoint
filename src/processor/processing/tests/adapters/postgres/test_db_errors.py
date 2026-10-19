# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from django.db import DatabaseError, DataError, OperationalError

from processing.adapters.postgres.db_errors import (
    get_sqlstate,
    translate_persistence_error,
)
from processing.exceptions import RowScopedPersistenceError, TransientPersistenceError


class FakeDriverError(Exception):
    def __init__(self, pgcode: str | None = None):
        super().__init__("driver failure")
        self.pgcode = pgcode


def test_get_sqlstate_reads_driver_exception_chain():
    exc = DatabaseError("database failure")
    exc.__cause__ = FakeDriverError("40P01")

    assert get_sqlstate(exc) == "40P01"


def test_translate_persistence_error_maps_operational_error_to_transient():
    translated = translate_persistence_error(OperationalError("connection lost"))

    assert isinstance(translated, TransientPersistenceError)


def test_translate_persistence_error_maps_transient_sqlstate_to_transient():
    exc = DatabaseError("database failure")
    exc.__cause__ = FakeDriverError("40001")

    translated = translate_persistence_error(exc)

    assert isinstance(translated, TransientPersistenceError)


def test_translate_persistence_error_maps_data_error_to_row_scoped():
    translated = translate_persistence_error(DataError("value too long"))

    assert isinstance(translated, RowScopedPersistenceError)
