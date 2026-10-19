"""100% coverage of ``service_error_to_http``.

Exhaustive map: every typed ``ServiceError`` subclass routes to a
specific (status, ErrorCode, message). The translator's catchall path
must NEVER echo the raw exception message — internal errors can carry
sensitive context (paths, table names) and the operator pivots via the
correlated WARN log instead.

Future ServiceError subclasses caught by ``__subclasses__()`` walk
guarantee the translator stays exhaustive: a new subclass added to
``services/exceptions.py`` without a matching translator branch falls
into the catchall and emits the unmapped WARN — the test asserts that
catchall behaviour stays well-defined (status 500, generic body,
log emitted).
"""

from __future__ import annotations

from typing import NoReturn, get_type_hints

import pytest
from fastapi import HTTPException
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    AuthenticationFailedError,
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

# ---------------------------------------------------------------------------
# 404 — NotFound family
# ---------------------------------------------------------------------------


def test_credential_not_found_maps_to_404() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(CredentialNotFoundError("alice"))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "credential not found"


def test_user_not_found_maps_to_404() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(UserNotFoundError("alice"))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "user not found"


def test_sync_not_found_maps_to_404() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(SyncNotFoundError("123"))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "sync not found"


def test_item_not_found_maps_to_404() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(ItemNotFoundError("xid"))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "item not found"


def test_alert_rule_not_found_maps_to_404() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(AlertRuleNotFoundError(1))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "rule not found"


def test_unknown_not_found_subclass_falls_through_to_404_branch() -> None:
    """Future ``NotFoundError`` subclasses (or a bare ``NotFoundError``
    raised directly) land on the generic 404 path with the exception's
    own message rather than the internal-error catchall."""

    exc_obj = NotFoundError("custom thing")
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(exc_obj)
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "not_found"
    assert exc.detail["message"] == "custom thing"


def test_unknown_not_found_subclass_with_empty_message_uses_default() -> None:
    """A bare ``NotFoundError("")`` falls back to the default 'not
    found' string rather than emitting an empty message."""
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(NotFoundError(""))
    exc = excinfo.value
    assert exc.status_code == 404
    assert isinstance(exc.detail, dict)
    assert exc.detail["message"] == "not found"


# ---------------------------------------------------------------------------
# 409 — StateConflict
# ---------------------------------------------------------------------------


def test_state_conflict_maps_to_409() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(StateConflictError("running"))
    exc = excinfo.value
    assert exc.status_code == 409
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "state_conflict"
    assert exc.detail["message"] == "running"


def test_state_conflict_empty_message_uses_default() -> None:
    """Empty string falls back to ``"state conflict"`` so callers always
    see a non-empty body."""
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(StateConflictError(""))
    exc = excinfo.value
    assert exc.status_code == 409
    assert isinstance(exc.detail, dict)
    assert exc.detail["message"] == "state conflict"


# ---------------------------------------------------------------------------
# 401 — AuthenticationFailed (slice 8)
# ---------------------------------------------------------------------------


def test_authentication_failed_maps_to_401_unauthorized() -> None:
    """Slice 8: pipeline-detected 401/403-mid-sync routes through this
    typed exception and surfaces as 401 ``unauthorized``. The body
    carries the typed message (platform + email + http_status + cause)
    rather than the catchall's generic "internal error" string —
    AuthenticationFailedError's own repr is operator-safe (no secrets,
    just identity + status + cause text)."""
    err = AuthenticationFailedError(
        platform="teams",
        user_email="alice@x",
        http_status=401,
        cause="token expired",
    )
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 401
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "unauthorized"
    # Diagnostic fields ride through the message — no leak risk because
    # the constructor formatter is the only message source.
    assert "alice@x" in exc.detail["message"]
    assert "teams" in exc.detail["message"]
    assert "401" in exc.detail["message"]


def test_authentication_failed_preserves_403_status_in_message() -> None:
    """When the underlying HTTP cause was a 403 the wire body still
    surfaces 401 (the translator is type-driven, not field-driven) but
    the message keeps the original ``http_status=403`` so an operator
    can drill back to the underlying scope problem."""
    err = AuthenticationFailedError(
        platform="moodle",
        user_email="bob@x",
        http_status=403,
        cause="scope missing",
    )
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    # Wire status: 401 (translator decision); message carries the 403
    # so the dashboard log-search can pivot on the cause.
    assert exc.status_code == 401
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "unauthorized"
    assert "403" in exc.detail["message"]
    assert "scope missing" in exc.detail["message"]


# ---------------------------------------------------------------------------
# 503 — FeatureDisabled
# ---------------------------------------------------------------------------


def test_feature_disabled_maps_to_503() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(FeatureDisabledError("teams_refresh"))
    exc = excinfo.value
    assert exc.status_code == 503
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "feature_disabled"
    assert exc.detail["message"] == "teams_refresh"


def test_feature_disabled_empty_message_uses_default() -> None:
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(FeatureDisabledError(""))
    exc = excinfo.value
    assert exc.status_code == 503
    assert isinstance(exc.detail, dict)
    assert exc.detail["message"] == "feature disabled"


# ---------------------------------------------------------------------------
# 502 — TransientFailure
# ---------------------------------------------------------------------------


def test_transient_failure_maps_to_502_with_str_detail() -> None:
    """``TransientFailureError.detail`` is rendered with ``str()`` so the
    body stays human-readable; structured payloads ride along in the
    audit log."""
    err = TransientFailureError("graph 502")
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 502
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "transient_failure"
    assert exc.detail["message"] == "graph 502"


def test_transient_failure_with_dict_detail() -> None:
    """``detail`` can be a dict (refresh outcome payload). The
    translator stringifies it for the wire body."""
    err = TransientFailureError({"outcome": "error", "error": "boom"})
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 502
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "transient_failure"
    # Pydantic-ish: the message contains the dict's str() output. We
    # don't pin the dict-formatting bytewise — that's a Python-version
    # detail — but the keys must appear in the message.
    assert "outcome" in exc.detail["message"]
    assert "error" in exc.detail["message"]


def test_transient_failure_with_none_detail_uses_default() -> None:
    """When ``.detail`` is None the translator falls through to
    ``"transient failure"`` so the body still carries a useful string."""
    err = TransientFailureError("anything")
    err.detail = None  # simulate post-construction reset
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(err)
    exc = excinfo.value
    assert exc.status_code == 502
    assert isinstance(exc.detail, dict)
    assert exc.detail["message"] == "transient failure"


# ---------------------------------------------------------------------------
# Catchall — unmapped ServiceError subclass
# ---------------------------------------------------------------------------


class _UnmappedServiceError(ServiceError):
    """Stand-in for a future ``ServiceError`` subclass the translator
    forgot to add a branch for."""


def test_catchall_maps_to_500_with_generic_body() -> None:
    """Catchall body MUST be the generic ``"internal error"`` string —
    NEVER the exception's own message — to avoid leaking sensitive
    context (paths, table names, partial credentials)."""
    sentinel = "very-private-table-name-do-not-leak"
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(_UnmappedServiceError(sentinel))
    exc = excinfo.value
    assert exc.status_code == 500
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "internal_error"
    assert exc.detail["message"] == "internal error"
    assert sentinel not in exc.detail["message"]


def test_catchall_emits_warn_log_with_exc_type_and_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operator-facing trail: the unmapped branch logs at WARN with
    ``exc_type`` + ``exc_message`` so it can pivot back to the original
    error class without grepping the response body.

    The daemon's structlog config writes to stdout (see
    ``ingestion.daemon.logging.configure_logging``); ``caplog`` only
    sees logs that pass through the stdlib root logger, not the
    structlog ConsoleRenderer/JSONRenderer output. Pin via stdout so
    the assertion matches what an operator actually pivots from.
    """
    sentinel = "private-table-x"
    with pytest.raises(HTTPException):
        service_error_to_http(_UnmappedServiceError(sentinel))
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "api.service_error_unmapped" in text
    assert "_UnmappedServiceError" in text
    # The exc_message field carries the sentinel — important for ops.
    assert sentinel in text


def test_catchall_handles_bare_serviceerror() -> None:
    """A bare ``ServiceError("...")`` — no subclass — still funnels to
    the catchall and emits the canonical 500 body."""
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(ServiceError("uncategorised"))
    exc = excinfo.value
    assert exc.status_code == 500
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == "internal_error"
    assert exc.detail["message"] == "internal error"


# ---------------------------------------------------------------------------
# Type signature
# ---------------------------------------------------------------------------


def test_translator_is_no_return_typed() -> None:
    """``service_error_to_http`` is annotated ``NoReturn`` — pyright
    treats code after a call to it as unreachable."""
    hints = get_type_hints(service_error_to_http)
    assert hints.get("return") is NoReturn


# ---------------------------------------------------------------------------
# Exhaustiveness — every ServiceError subclass routes somewhere
# ---------------------------------------------------------------------------


def _all_service_error_subclasses() -> list[type[ServiceError]]:
    """Walk the class tree so future subclasses are picked up
    automatically. Excludes ``ServiceError`` itself (the test of the
    bare-base-class route lives above)."""
    seen: set[type[ServiceError]] = set()
    stack: list[type[ServiceError]] = list(ServiceError.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return sorted(seen, key=lambda c: c.__name__)


def test_every_service_error_subclass_routes_somewhere() -> None:
    """Exhaustiveness regression guard: a new ``ServiceError`` subclass
    added to ``services/exceptions.py`` either gets a translator branch
    (and surfaces with the right status/code) OR lands on the catchall.
    Either way the translator must not raise the input exception itself
    or a non-HTTPException — every subclass MUST translate cleanly.
    """
    subclasses = _all_service_error_subclasses()
    assert subclasses, "no ServiceError subclasses found — import failed?"

    for cls in subclasses:
        # ``TransientFailureError`` requires a single positional arg via
        # its constructor; ``AuthenticationFailedError`` requires four
        # keyword-only args (slice 8); ``BackpressureError`` requires
        # ``queue_depth`` + ``max_depth`` kw-only (slice 10); most
        # others accept an arbitrary message arg.
        try:
            instance = cls("test-message")
        except TypeError:
            try:
                instance = cls()
            except TypeError:
                if cls.__name__ == "BackpressureError":
                    # Slice 10 kw-only constructor.
                    instance = cls(queue_depth=10, max_depth=5)
                else:
                    # Kw-only constructors (e.g. AuthenticationFailedError):
                    # supply a generic fixture payload so the translator
                    # exhaustiveness check still runs.
                    instance = cls(
                        platform="test",
                        user_email="user@example.com",
                        http_status=401,
                        cause="test-message",
                    )
        with pytest.raises(HTTPException) as excinfo:
            service_error_to_http(instance)
        exc = excinfo.value
        assert exc.status_code in (401, 404, 409, 500, 502, 503), (
            f"{cls.__name__} routed to unexpected status {exc.status_code}"
        )
        assert isinstance(exc.detail, dict), (
            f"{cls.__name__} produced non-dict detail {exc.detail!r}"
        )
        assert exc.detail.get("code") in {
            "not_found",
            "state_conflict",
            "unauthorized",
            "feature_disabled",
            "transient_failure",
            "rate_limited",
            "internal_error",
        }, f"{cls.__name__} produced unexpected code {exc.detail.get('code')!r}"


def test_every_known_subclass_in_translator_table() -> None:
    """Frozen list of known ServiceError subclasses present today.
    Future additions are FINE — they'll fall through the catchall — but
    a rename or removal of a known subclass breaks this test as a
    deliberate signal that the translator branch needs revisiting."""
    known = {cls.__name__ for cls in _all_service_error_subclasses()}
    expected = {
        "NotFoundError",
        "CredentialNotFoundError",
        "UserNotFoundError",
        "ItemNotFoundError",
        "SyncNotFoundError",
        "AlertRuleNotFoundError",
        "StateConflictError",
        "FeatureDisabledError",
        "TransientFailureError",
        "AuthenticationFailedError",
        "BackpressureError",
    }
    # Don't assert equality (future subclasses might be added without a
    # translator branch); assert the known set is a subset of what we
    # observe — a removal will fail the test, an addition won't.
    missing = expected - known
    assert missing == set(), f"known ServiceError subclasses missing: {missing}"
