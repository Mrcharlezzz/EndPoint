"""Coverage for the typed service-layer exception hierarchy.

These exceptions are the contract between services (which raise) and
routers (which translate to HTTP). Pin:

* every subclass instantiates with sensible args
* the isinstance chain is intact so router ``except ServiceError`` catches
* ``TransientFailureError.detail`` survives the str(super) call so router
  HTTP body building reads the structured payload back
"""

from __future__ import annotations

import pytest
from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    CredentialNotFoundError,
    FeatureDisabledError,
    ItemNotFoundError,
    NotFoundError,
    ServiceError,
    StateConflictError,
    SyncNotFoundError,
    TransientFailureError,
    UserNotFoundError,
)

# Every subclass we expose. Used by the parametrized chain test below.
_NOT_FOUND_SUBCLASSES = (
    CredentialNotFoundError,
    UserNotFoundError,
    ItemNotFoundError,
    SyncNotFoundError,
    AlertRuleNotFoundError,
)


def test_service_error_is_an_exception() -> None:
    """Sanity: ``ServiceError`` extends Exception so ``raise`` works."""
    assert issubclass(ServiceError, Exception)


@pytest.mark.parametrize("cls", _NOT_FOUND_SUBCLASSES)
def test_each_not_found_subclass_inherits_from_not_found(cls: type[Exception]) -> None:
    """Every NotFound subclass is-a NotFoundError. Routers translate by
    ``except NotFoundError`` only when the impl chooses to; some
    routers branch on the leaf class. Both must work."""
    err = cls("anything")
    assert isinstance(err, NotFoundError)
    assert isinstance(err, ServiceError)
    assert isinstance(err, Exception)


def test_state_conflict_chains_to_service_error() -> None:
    err = StateConflictError("bad state")
    assert isinstance(err, ServiceError)
    assert not isinstance(err, NotFoundError)


def test_feature_disabled_chains_to_service_error() -> None:
    err = FeatureDisabledError("disabled")
    assert isinstance(err, ServiceError)


def test_transient_failure_chains_to_service_error() -> None:
    err = TransientFailureError({"k": "v"})
    assert isinstance(err, ServiceError)


def test_transient_failure_carries_detail_unchanged() -> None:
    """``detail`` is the structured payload routers serialise into the
    502 body; pin that it survives ``__init__``."""
    detail = {"outcome": "error", "error": "boom"}
    err = TransientFailureError(detail)
    assert err.detail is detail


def test_transient_failure_str_uses_detail() -> None:
    """Fallback for log lines: ``str(err)`` is ``str(detail)``."""
    err = TransientFailureError("boom")
    assert str(err) == "boom"


def test_transient_failure_with_complex_detail_stringifies_for_logs() -> None:
    """Edge: a non-string detail (dict) -> str(detail) for the
    Exception's args. The router still reads ``.detail`` for the
    structured body."""
    err = TransientFailureError({"x": 1})
    assert err.detail == {"x": 1}
    # The base exception still carries a string repr so logs aren't
    # opaque.
    assert "x" in str(err)


def test_each_subclass_can_be_raised_and_caught_as_base() -> None:
    """End-to-end: every leaf can be raised and caught via the base.
    Pins the full chain in one test instead of N tiny ones."""
    for cls in (
        *_NOT_FOUND_SUBCLASSES,
        StateConflictError,
        FeatureDisabledError,
    ):
        with pytest.raises(ServiceError):
            raise cls("x")
    with pytest.raises(ServiceError):
        raise TransientFailureError({"x": 1})


def test_not_found_subclasses_can_be_raised_and_caught_via_not_found() -> None:
    """Routers may branch on NotFoundError itself; pin every leaf is
    catchable that way."""
    for cls in _NOT_FOUND_SUBCLASSES:
        with pytest.raises(NotFoundError):
            raise cls("x")


# ---------------------------------------------------------------------------
# Property-style sanity: every public attribute on the module is either
# an exception class or a private name. Catches accidental "I added a
# helper here" bloat.
# ---------------------------------------------------------------------------


def test_module_exposes_only_exception_classes() -> None:
    """[TEST-COVERAGE] The exceptions module is a contract surface; it
    must not start growing helpers without an explicit decision. Pin
    that every public name is an Exception subclass."""
    from ingestion.daemon.services import exceptions

    public = [name for name in dir(exceptions) if not name.startswith("_")]
    for name in public:
        obj = getattr(exceptions, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            continue
        # Allow ``annotations`` from __future__ import.
        if name == "annotations":
            continue
        msg = f"unexpected public attribute on exceptions module: {name!r}"
        raise AssertionError(msg)
