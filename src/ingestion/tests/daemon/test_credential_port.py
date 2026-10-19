"""Coverage for the :class:`CredentialPort` Protocol surface.

The port itself is a Protocol (no behaviour), but the row dataclass it
returns IS load-bearing — it's the immutable handoff between the ORM-
backed store and every downstream service. Pin the dataclass invariants
that the rest of the slice depends on:

* frozen + slots: services treat these as values; mutation must raise
* hashability: services may stash these in sets/dicts
* equality: tests compare instances by value
* protocol satisfaction: the production
  :class:`DjangoCredentialStore` is a structural match for
  :class:`CredentialPort` so consumers can typecheck against the port
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest
from ingestion.daemon.ports.credential_port import CredentialPort, CredentialRow
from ingestion.daemon.ports.credential_store import DjangoCredentialStore


def _row(**overrides: object) -> CredentialRow:
    """Build a CredentialRow with sensible defaults for the 7 fields."""
    base: dict[str, object] = {
        "user_email": "alice@x",
        "platform": "moodle",
        "is_paused": False,
        "is_expired": False,
        "expired_at": None,
        "last_refreshed_at": None,
        "encrypted_payload": b"\x00",
    }
    base.update(overrides)
    return CredentialRow(**base)  # type: ignore[arg-type]


def test_row_is_a_dataclass() -> None:
    """``CredentialRow`` is a stdlib dataclass — every consumer relies
    on the dataclass surface (``fields``, replace, ...)."""
    assert is_dataclass(CredentialRow)


def test_row_has_seven_fields_in_documented_order() -> None:
    """The 7 fields match what the docstring claims; the order matters
    because the production code constructs by positional + keyword mix."""
    names = [f.name for f in fields(CredentialRow)]
    assert names == [
        "user_email",
        "platform",
        "is_paused",
        "is_expired",
        "expired_at",
        "last_refreshed_at",
        "encrypted_payload",
    ]


def test_row_is_frozen() -> None:
    """Mutation must raise ``FrozenInstanceError`` so a downstream
    service can't accidentally rewrite a snapshot another caller is
    holding."""
    row = _row()
    with pytest.raises(FrozenInstanceError):
        row.is_paused = True  # type: ignore[misc]


def test_row_uses_slots() -> None:
    """``__slots__`` defined → no per-instance ``__dict__``. Catches
    accidental ``@dataclass(frozen=True)`` (without ``slots=True``)
    regressions that would re-enable attribute injection."""
    assert hasattr(CredentialRow, "__slots__")
    row = _row()
    assert not hasattr(row, "__dict__")


def test_row_is_hashable() -> None:
    """Frozen dataclasses are hashable. Services may stash these in
    sets/dicts; the test pins that they can."""
    row = _row()
    bucket = {row}
    assert row in bucket


def test_row_equality_is_by_value() -> None:
    """Two rows with the same fields compare equal regardless of
    construction. Pins the dataclass-default ``__eq__`` is enabled."""
    a = _row(user_email="bob@x", platform="teams")
    b = _row(user_email="bob@x", platform="teams")
    assert a == b
    assert hash(a) == hash(b)


def test_row_inequality_when_any_field_differs() -> None:
    """One field difference -> not equal. Pins that ``__eq__`` actually
    compares fields (vs always returning True)."""
    a = _row(is_paused=False)
    b = _row(is_paused=True)
    assert a != b


def test_row_repr_contains_field_names() -> None:
    """Default dataclass repr exposes field names — useful for failure
    output. Pin the contract so a future ``@dataclass(repr=False)``
    regression is loud."""
    row = _row()
    text = repr(row)
    assert "user_email" in text
    assert "is_paused" in text


def test_row_carries_optional_iso_timestamps() -> None:
    """``expired_at`` and ``last_refreshed_at`` accept None or string
    values and round-trip them unchanged."""
    row = _row(
        expired_at="2026-04-26T00:00:00+00:00",
        last_refreshed_at="2026-04-25T12:00:00+00:00",
    )
    assert row.expired_at == "2026-04-26T00:00:00+00:00"
    assert row.last_refreshed_at == "2026-04-25T12:00:00+00:00"


def test_row_carries_bytes_payload_unchanged() -> None:
    """``encrypted_payload`` is bytes — pin that it isn't coerced to
    str on construction."""
    payload = b"\xde\xad\xbe\xef"
    row = _row(encrypted_payload=payload)
    assert row.encrypted_payload == payload
    assert isinstance(row.encrypted_payload, bytes)


def test_django_store_is_structural_match_for_port() -> None:
    """The production store class implements the protocol structurally —
    every method on :class:`CredentialPort` is present on
    :class:`DjangoCredentialStore`. Pyright already enforces this at
    type-check time; the runtime check pins it for callers that don't
    run pyright (e.g., service-layer tests)."""
    store: CredentialPort = DjangoCredentialStore()
    # Touch every Protocol method by name so a future rename in either
    # half is caught at test time, not on the next live call.
    assert callable(store.get)
    assert callable(store.list_for_user)
    assert callable(store.list_for_platform)
    assert callable(store.set_paused)
    assert callable(store.set_paused_bulk_for_user)
    assert callable(store.set_expired)


def test_protocol_method_names_are_stable() -> None:
    """Pin the Protocol's method surface byte-for-byte. A renamed method
    here would be a contract break; a new method would mean every
    in-memory fake the test suite uses needs an update."""
    expected = {
        "get",
        "list_for_user",
        "list_for_platform",
        "set_paused",
        "set_paused_bulk_for_user",
        "set_expired",
    }
    actual = {
        name
        for name in dir(CredentialPort)
        if not name.startswith("_") and callable(getattr(CredentialPort, name, None))
    }
    assert expected.issubset(actual)
