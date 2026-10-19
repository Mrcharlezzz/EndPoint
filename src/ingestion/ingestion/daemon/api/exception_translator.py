"""Single-source-of-truth translator: ServiceError -> HTTP envelope.

Every router that catches a typed service-layer exception delegates the
translation to :func:`service_error_to_http`. This eliminates the
copy-paste of ``if/elif`` mappings that used to live in each router and
guarantees one canonical (status, code, message) per exception subclass.

The translator is exhaustive over the typed-exception hierarchy in
:mod:`ingestion.daemon.services.exceptions`. Adding a new ``ServiceError``
subclass means a one-liner edit here; otherwise the catchall maps it to
500 + ``internal_error`` and logs the full type for the audit trail.

The catchall deliberately does NOT echo the raw exception message in the
response — internal errors can carry sensitive context (paths, table
names, partial credentials in the worst case). Operators read the
correlated structlog event for the underlying detail; clients see a
generic "internal error" string.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import status

from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.middleware.request_id import request_id_var
from ingestion.daemon.logging import get_logger
from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    AuthenticationFailedError,
    BackpressureError,
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

_log = get_logger(__name__)


def service_error_to_http(exc: ServiceError) -> NoReturn:
    """Translate a typed service exception to an :class:`HTTPException`.

    Status / code mapping (locked by tests in the slice 4 tester pass):

    * :class:`CredentialNotFoundError` -> 404 ``not_found``
    * :class:`UserNotFoundError`       -> 404 ``not_found``
    * :class:`SyncNotFoundError`       -> 404 ``not_found``
    * :class:`ItemNotFoundError`       -> 404 ``not_found``
    * :class:`AlertRuleNotFoundError`  -> 404 ``not_found``
    * (any other :class:`NotFoundError`) -> 404 ``not_found``
    * :class:`StateConflictError`      -> 409 ``state_conflict``
    * :class:`AuthenticationFailedError` -> 401 ``unauthorized``
    * :class:`BackpressureError`       -> 503 ``rate_limited``
    * :class:`FeatureDisabledError`    -> 503 ``feature_disabled``
    * :class:`TransientFailureError`   -> 502 ``transient_failure``
    * (any other :class:`ServiceError`) -> 500 ``internal_error``
    """
    # Specific NotFound subclasses share the 404 path. Per-subclass
    # messages let the dashboard surface "user not found" vs "credential
    # not found" without inspecting the type. Subclass tests precede the
    # base ``NotFoundError`` so the more specific message wins.
    if isinstance(exc, CredentialNotFoundError):
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "credential not found",
        )
    if isinstance(exc, UserNotFoundError):
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "user not found",
        )
    if isinstance(exc, SyncNotFoundError):
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "sync not found",
        )
    if isinstance(exc, ItemNotFoundError):
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "item not found",
        )
    if isinstance(exc, AlertRuleNotFoundError):
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "rule not found",
        )
    if isinstance(exc, NotFoundError):
        # Future NotFoundError subclasses we haven't enumerated yet still
        # land on 404 with the exception's own message — preferable to
        # falling through to the catchall 500.
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            str(exc) or "not found",
        )
    if isinstance(exc, StateConflictError):
        raise_http_exception(
            status.HTTP_409_CONFLICT,
            ErrorCode.STATE_CONFLICT,
            str(exc) or "state conflict",
        )
    if isinstance(exc, AuthenticationFailedError):
        # Slice 8: pipeline 401/403-mid-sync detection raises this typed
        # exception after marking the credential expired. Surface as 401
        # unauthorized so the dashboard's auth-aware fetch wrapper can
        # branch on the status without parsing the message text. The
        # rich diagnostic fields (platform, user_email, http_status,
        # cause) are carried in the ``str(exc)`` repr — sufficient for
        # operator drill-downs without leaking secrets.
        raise_http_exception(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.UNAUTHORIZED,
            str(exc) or "authentication failed",
        )
    if isinstance(exc, BackpressureError):
        # Slice 10: force-sync queue capped at ``max_force_sync_queue_depth``
        # to prevent operator scripts from flooding SQLite. Surface as
        # 503 + ``rate_limited`` so clients with a backoff policy retry
        # the request later instead of treating it as a hard failure.
        # The exception's payload (queue_depth + max_depth) appears in
        # the message so the dashboard can render an informative
        # "queue full" banner without parsing internal fields.
        raise_http_exception(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorCode.RATE_LIMITED,
            str(exc) or "force-sync queue at capacity",
            hint="retry after the queue drains",
        )
    if isinstance(exc, FeatureDisabledError):
        raise_http_exception(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorCode.FEATURE_DISABLED,
            str(exc) or "feature disabled",
        )
    if isinstance(exc, TransientFailureError):
        # ``TransientFailureError.detail`` is a structured payload (often
        # a dict with refresh-outcome context). Render its ``str()`` so
        # the message stays human-readable; the dashboard can read the
        # structured payload from the audit log if it needs more detail.
        raise_http_exception(
            status.HTTP_502_BAD_GATEWAY,
            ErrorCode.TRANSIENT_FAILURE,
            str(exc.detail) if exc.detail is not None else "transient failure",
        )
    # Catchall: log the full type + repr at WARN so an operator can
    # identify the unmapped class, but DO NOT echo the message back to
    # the client. Internal errors can carry sensitive context (paths,
    # table names, partial credentials); a generic body is safer.
    #
    # ``request_id`` is read explicitly from
    # :data:`request_id_var` so the WARN carries the correlation field
    # even when a caller invoked the translator from a code path that
    # set the contextvar without also calling
    # ``structlog.contextvars.bind_contextvars`` (which is what
    # ``merge_contextvars`` reads from). Only attach the kwarg when the
    # contextvar has a non-empty value so we don't override the
    # structlog-bound copy with an empty fallback when the prod
    # middleware path bound the value via structlog only.
    extra_fields: dict[str, str] = {}
    bound_rid = request_id_var.get()
    if bound_rid:
        extra_fields["request_id"] = bound_rid
    _log.warning(
        "api.service_error_unmapped",
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        **extra_fields,
    )
    raise_http_exception(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        "internal error",
    )
