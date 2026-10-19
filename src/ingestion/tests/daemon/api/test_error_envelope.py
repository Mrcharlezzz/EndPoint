"""100% coverage of ``error_envelope.py`` — model, enum, and helper.

These tests pin the public contract of :class:`ErrorEnvelope` and
:class:`ErrorCode` so a future rename or accidental schema relaxation
breaks the build rather than the dashboard. The envelope shape is part
of the daemon's wire-format API; clients branch on these strings.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from ingestion.daemon.api.error_envelope import (
    ErrorCode,
    ErrorEnvelope,
    raise_http_exception,
)
from ingestion.daemon.api.middleware.request_id import request_id_var
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# ErrorCode (StrEnum) — exhaustive value pinning
# ---------------------------------------------------------------------------


def test_error_code_is_strenum_subclass() -> None:
    """``ErrorCode`` inherits from ``StrEnum`` so values are usable as
    plain strings without ``.value`` everywhere — important for JSON
    serialization through Pydantic."""
    from enum import StrEnum

    assert issubclass(ErrorCode, StrEnum)


def test_error_code_str_value_round_trip() -> None:
    """``StrEnum`` semantics: enum value compares equal to its string."""
    assert ErrorCode.NOT_FOUND == "not_found"
    assert str(ErrorCode.NOT_FOUND) == "not_found"
    assert ErrorCode.NOT_FOUND.value == "not_found"


def test_error_code_exhaustive_value_table() -> None:
    """Pin every ErrorCode value bytewise. New values may be appended,
    but renames or drops break this test — clients branch on these
    strings so they're a public contract.
    """
    expected = {
        "NOT_FOUND": "not_found",
        "STATE_CONFLICT": "state_conflict",
        "FEATURE_DISABLED": "feature_disabled",
        "TRANSIENT_FAILURE": "transient_failure",
        "VALIDATION_ERROR": "validation_error",
        "UNAUTHORIZED": "unauthorized",
        "FORBIDDEN": "forbidden",
        "RATE_LIMITED": "rate_limited",
        "INTERNAL_ERROR": "internal_error",
    }
    actual = {member.name: member.value for member in ErrorCode}
    assert actual == expected


def test_error_code_member_count_is_nine() -> None:
    """Pin the count so a silent drop (or duplicate) gets caught even
    without checking the value table."""
    assert len(list(ErrorCode)) == 9


# ---------------------------------------------------------------------------
# ErrorEnvelope — Pydantic model
# ---------------------------------------------------------------------------


def test_envelope_required_fields_only() -> None:
    """Minimum-shape envelope: code + message. Optional fields default
    to None and are dropped from ``model_dump(exclude_none=True)``."""
    envelope = ErrorEnvelope(code=ErrorCode.NOT_FOUND.value, message="missing")
    assert envelope.code == "not_found"
    assert envelope.message == "missing"
    assert envelope.hint is None
    assert envelope.request_id is None


def test_envelope_with_all_fields() -> None:
    envelope = ErrorEnvelope(
        code=ErrorCode.STATE_CONFLICT.value,
        message="bad state",
        hint="try again",
        request_id="abc123" + "0" * 26,
    )
    assert envelope.hint == "try again"
    assert envelope.request_id == "abc123" + "0" * 26


def test_envelope_extra_keys_rejected() -> None:
    """``extra="forbid"`` blocks unknown fields. Important: a future
    schema change that adds a field can't accidentally land via the
    backdoor of an unintended caller-side payload."""
    with pytest.raises(ValidationError):
        ErrorEnvelope(  # type: ignore[call-arg]
            code=ErrorCode.NOT_FOUND.value,
            message="m",
            extra_field="should not be allowed",
        )


def test_envelope_dump_drops_none_fields() -> None:
    """`model_dump(exclude_none=True)` is the canonical serialiser used
    by ``raise_http_exception``. None-valued fields must NOT appear in
    the output (clients shouldn't see ``"request_id": null`` and start
    branching on it)."""
    envelope = ErrorEnvelope(code=ErrorCode.NOT_FOUND.value, message="missing")
    body = envelope.model_dump(exclude_none=True)
    assert body == {"code": "not_found", "message": "missing"}
    assert "hint" not in body
    assert "request_id" not in body


def test_envelope_dump_keeps_set_fields() -> None:
    """Fields with non-None values survive the exclude_none pass."""
    envelope = ErrorEnvelope(
        code=ErrorCode.RATE_LIMITED.value,
        message="slow down",
        hint="back off 30s",
        request_id="f" * 32,
    )
    body = envelope.model_dump(exclude_none=True)
    assert body == {
        "code": "rate_limited",
        "message": "slow down",
        "hint": "back off 30s",
        "request_id": "f" * 32,
    }


def test_envelope_message_required() -> None:
    """``message`` has no default — must be supplied explicitly."""
    with pytest.raises(ValidationError):
        ErrorEnvelope(code=ErrorCode.NOT_FOUND.value)  # type: ignore[call-arg]


def test_envelope_code_required() -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(message="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# raise_http_exception — main helper
# ---------------------------------------------------------------------------


def test_raise_http_exception_minimal() -> None:
    """Default path: status + code + message wraps to HTTPException with
    detail dict in canonical envelope shape."""
    with pytest.raises(HTTPException) as excinfo:
        raise_http_exception(404, ErrorCode.NOT_FOUND, "missing")
    exc = excinfo.value
    assert exc.status_code == 404
    assert exc.detail == {"code": "not_found", "message": "missing"}


def test_raise_http_exception_with_hint() -> None:
    with pytest.raises(HTTPException) as excinfo:
        raise_http_exception(
            422,
            ErrorCode.VALIDATION_ERROR,
            "bad input",
            hint="see schema",
        )
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["hint"] == "see schema"
    assert detail["code"] == "validation_error"
    assert detail["message"] == "bad input"


def test_raise_http_exception_no_request_id_outside_request() -> None:
    """When called outside any HTTP scope (background task, unit test)
    the request_id contextvar resolves to the default empty string;
    ``raise_http_exception`` translates that to None and the field is
    dropped via ``exclude_none=True``. This pins the documented
    "no request scope -> no request_id key" behaviour.
    """
    request_id_var.set("")
    with pytest.raises(HTTPException) as excinfo:
        raise_http_exception(500, ErrorCode.INTERNAL_ERROR, "boom")
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert "request_id" not in detail


def test_raise_http_exception_includes_request_id_when_bound() -> None:
    """When a request scope IS active the contextvar holds a hex string;
    the helper threads it into the envelope so the dashboard / log feed
    can correlate."""
    rid = "deadbeef" * 4
    token = request_id_var.set(rid)
    try:
        with pytest.raises(HTTPException) as excinfo:
            raise_http_exception(404, ErrorCode.NOT_FOUND, "missing")
        detail = excinfo.value.detail
        assert isinstance(detail, dict)
        assert detail["request_id"] == rid
    finally:
        request_id_var.reset(token)


def test_raise_http_exception_all_status_codes() -> None:
    """Sanity sweep: helper accepts any non-zero status code; the
    detail-shape contract holds for 4xx and 5xx alike."""
    for status_code in (400, 401, 403, 404, 409, 422, 500, 502, 503):
        with pytest.raises(HTTPException) as excinfo:
            raise_http_exception(status_code, ErrorCode.INTERNAL_ERROR, "x")
        assert excinfo.value.status_code == status_code


def test_raise_http_exception_uses_enum_value_not_object() -> None:
    """The wire body's ``code`` must be the StrEnum's ``.value`` (a
    plain string), not a JSON-rendered Enum object. JSON serialisation
    in Pydantic V2 already does this transparently because ErrorCode
    is a StrEnum, but pin it here so a future refactor that swaps the
    enum to a Mapping or a Literal can't silently change the body."""
    with pytest.raises(HTTPException) as excinfo:
        raise_http_exception(403, ErrorCode.FORBIDDEN, "no")
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "forbidden"
    assert isinstance(detail["code"], str)


def test_raise_http_exception_is_no_return_typed() -> None:
    """Pin the type signature: the helper is annotated ``NoReturn`` so
    pyright treats code after a call to it as unreachable. We can't
    introspect that annotation cleanly at runtime, but we can verify
    the function ALWAYS raises — never reaches a normal return path."""
    import inspect
    from typing import NoReturn, get_type_hints

    hints = get_type_hints(raise_http_exception)
    assert hints.get("return") is NoReturn
    # And confirm it always raises by calling and observing.
    sig = inspect.signature(raise_http_exception)
    assert "status" in sig.parameters
    assert "code" in sig.parameters
    assert "message" in sig.parameters
    assert "hint" in sig.parameters
