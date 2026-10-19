"""Uniform error envelope for 4xx / 5xx responses.

Every error response from the daemon's API surfaces the same shape:

    {
        "code": "<machine-readable enum>",
        "message": "<human-readable summary>",
        "hint": "<optional remediation>",
        "request_id": "<correlation hex>"
    }

Wrapping the envelope in ``HTTPException(detail=...)`` keeps Starlette's
default error renderer (``JSONResponse({"detail": ...})``) — clients see
the envelope under the ``detail`` key, which is the FastAPI-canonical
location and avoids breaking the dashboard's existing fetch wrappers.

The :class:`ErrorCode` enum is part of the public API contract: dashboard
code branches on these values. New codes can be appended; existing codes
must not be renamed without a migration plan.

``request_id`` is pulled from the request-scoped contextvar so background
tasks (no request scope) emit ``"request_id": ""`` rather than crashing.
``model_dump(exclude_none=True)`` drops the field entirely when the value
is None — clients that read the envelope shouldn't see ``"request_id":
null`` and start branching on it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NoReturn

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.middleware.request_id import request_id_var


class ErrorCode(StrEnum):
    """Machine-readable error codes. Public contract — clients branch on
    these strings, so renaming any value is a breaking change. New codes
    may be appended without ceremony.
    """

    NOT_FOUND = "not_found"
    STATE_CONFLICT = "state_conflict"
    FEATURE_DISABLED = "feature_disabled"
    TRANSIENT_FAILURE = "transient_failure"
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


class ErrorEnvelope(BaseModel):
    """Wire-format payload returned under HTTPException(detail=...).

    ``code`` is the enum value (already a string at the wire layer thanks
    to :class:`StrEnum`). ``hint`` is operator-facing remediation copy;
    omit when the message is sufficient. ``request_id`` correlates the
    error with a structlog event in the daemon's log feed.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    hint: str | None = None
    request_id: str | None = None


def raise_http_exception(
    status: int,
    code: ErrorCode,
    message: str,
    *,
    hint: str | None = None,
) -> NoReturn:
    """Raise an :class:`HTTPException` whose ``detail`` is an envelope.

    ``request_id`` is populated automatically from the request-scoped
    contextvar; when raised outside an HTTP request (background task,
    bare unit test) the field falls through as None and is excluded from
    the serialised body via ``exclude_none=True``.
    """
    bound = request_id_var.get()
    envelope = ErrorEnvelope(
        code=code.value,
        message=message,
        hint=hint,
        # An empty string from the contextvar default means "no request
        # scope was active"; treat it the same as None so the field is
        # dropped on serialise.
        request_id=bound or None,
    )
    raise HTTPException(
        status_code=status,
        detail=envelope.model_dump(exclude_none=True),
    )
