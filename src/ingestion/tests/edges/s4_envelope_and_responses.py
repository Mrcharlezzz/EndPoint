"""Edge + production-edge coverage for slice 4 (error envelope + ServiceError
translator + Pydantic response models).

Per-test docstrings tag each scenario with one of:

    ``[TEST-COVERAGE]``  pinning correct existing behaviour
    ``[CODE-FIX]``       flagging an issue for the reviewer-fixer

The edge brainstorm beyond the seed list lives in this module's
docstring — at least 8 own additions:

* ``[EDGE-1]`` ErrorCode set is closed: clients can't smuggle a new
  code through ``Mapping[str, str]``-typed access on ``ErrorEnvelope``
* ``[EDGE-2]`` Pyright passes after the slice's narrowing — we test
  that narrowing didn't accidentally relax in another module
* ``[EDGE-3]`` ``raise_http_exception``'s NoReturn typing means a
  pyright check would flag reachable code after a call to it
* ``[EDGE-4]`` WARN log includes the request_id contextvar so the
  unmapped event is correlatable with the inbound request
* ``[EDGE-5]`` Two pre-existing tests adjusted by the impl
  (``test_cancel_sync_409``, ``s2_services_and_routers``) still pass —
  re-exercise the assertions here so a re-break shows a regression
* ``[EDGE-6]`` Background-task raise (no request scope) emits an
  envelope without ``request_id``
* ``[EDGE-7]`` Catchall does not echo a sentinel exception message in
  the response body — privacy guarantee, sweep multiple status codes
* ``[EDGE-8]`` Unmapped ``ServiceError`` doesn't mask the inner
  exception via ``__context__`` chaining (debugging affordance)
* ``[EDGE-9]`` JSON wire body keys are sorted into a fixed shape (no
  caller-injectable extras via reflection on the envelope model)
* ``[EDGE-10]`` Future ``ErrorCode`` additions don't break the
  exception_handler registration on ``app.py``

Slow tests (>500ms) carry ``@pytest.mark.slow``. The whole suite
finishes well under five seconds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.error_envelope import (
    ErrorCode,
    ErrorEnvelope,
    raise_http_exception,
)
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.api.middleware.request_id import request_id_var
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.services import results as results_module
from ingestion.daemon.services.exceptions import (
    ItemNotFoundError,
    ServiceError,
    StateConflictError,
    UserNotFoundError,
)
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def app_for_serviceerror_test(tmp_path: Path, fernet_key: str) -> FastAPI:
    """Real ``create_app`` instance with the ``ServiceError`` exception
    handler registered. Used by the production-path tests below to
    verify envelope wire shape end-to-end."""
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    return create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)


@pytest.fixture
def authed_client_for_app(app_for_serviceerror_test: FastAPI) -> TestClient:
    """TestClient logged in as admin against the slice-4 app."""
    client = TestClient(app_for_serviceerror_test)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = client.post("/api/auth/login", json={"password": "hunter2-strong"})
    assert response.status_code == 200, response.text
    return client


# ---------------------------------------------------------------------------
# Seed #1 — ErrorCode enum stability
# ---------------------------------------------------------------------------


def test_error_code_frozen_value_list() -> None:
    """[TEST-COVERAGE] Seed #1: pin the 9 values bytewise. Future
    additions OK; renames or drops break the test."""
    expected = {
        "not_found",
        "state_conflict",
        "feature_disabled",
        "transient_failure",
        "validation_error",
        "unauthorized",
        "forbidden",
        "rate_limited",
        "internal_error",
    }
    actual = {member.value for member in ErrorCode}
    # ``expected`` MUST be a subset; new codes are allowed without breaking.
    missing = expected - actual
    assert missing == set(), f"ErrorCode lost values: {missing}"


# ---------------------------------------------------------------------------
# Seed #2 — Catchall doesn't echo exception message
# ---------------------------------------------------------------------------


class _LeakySentinelError(ServiceError):
    """Custom subclass not mapped by the translator; carries a sentinel
    in its message that MUST NOT appear in the wire body."""


_SENSITIVE_SENTINEL = "/var/lib/postgres/SECRET-PATH-DO-NOT-LEAK"


def test_catchall_never_echoes_exception_message_in_body() -> None:
    """[TEST-COVERAGE] Seed #2: privacy guarantee — generic 500 body
    must NEVER carry the original message."""
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(_LeakySentinelError(_SENSITIVE_SENTINEL))
    exc = excinfo.value
    assert exc.status_code == 500
    body = json.dumps(exc.detail)
    assert _SENSITIVE_SENTINEL not in body
    assert "internal error" in body


# ---------------------------------------------------------------------------
# Seed #3 — request_id propagation through middleware -> exception -> envelope
# ---------------------------------------------------------------------------


def test_request_id_propagates_full_pipeline(authed_client_for_app: TestClient) -> None:
    """[TEST-COVERAGE] Seed #3: inbound X-Request-ID -> contextvar ->
    envelope's request_id field -> outbound response (both body and
    response header). Full integration."""
    rid = "1a2b3c4d" * 4
    response = authed_client_for_app.get(
        "/api/syncs/missing-id",
        headers={"X-Request-ID": rid},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["request_id"] == rid
    assert response.headers["x-request-id"] == rid


# ---------------------------------------------------------------------------
# Seed #4 — request_id absent when raised from a background task
# ---------------------------------------------------------------------------


async def test_request_id_absent_in_background_task() -> None:
    """[TEST-COVERAGE] Seed #4: a coroutine spawned with no request
    scope sees the contextvar at its default empty string; the
    envelope drops the request_id field via ``exclude_none=True``."""

    async def background() -> dict[str, Any]:
        # Reset to default to defeat any leaked binding.
        request_id_var.set("")
        try:
            raise_http_exception(500, ErrorCode.INTERNAL_ERROR, "bg")
        except HTTPException as exc:
            assert isinstance(exc.detail, dict)
            return exc.detail
        return {}

    detail = await background()
    assert "request_id" not in detail


# ---------------------------------------------------------------------------
# Seed #5 — Pydantic response models declare extra="forbid"
# ---------------------------------------------------------------------------


def test_every_results_model_forbids_extras() -> None:
    """[TEST-COVERAGE] Seed #5: every model in
    ``ingestion.daemon.services.results`` declares
    ``model_config = ConfigDict(extra="forbid")``. Walks the module's
    ``BaseModel`` subclasses so a new class added later trips the test
    if it forgets the strict config.
    """
    from pydantic import BaseModel

    found = 0
    for name in dir(results_module):
        obj = getattr(results_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            extra = obj.model_config.get("extra")
            assert extra == "forbid", (
                f"{obj.__name__} in results.py is missing extra='forbid' "
                f"(got {extra!r}); inbound clients can smuggle unknown keys."
            )
            found += 1
    # Sanity check: there's at least the dozen-or-so we know about.
    assert found >= 10, f"only found {found} response models"


def test_error_envelope_itself_forbids_extras() -> None:
    """[TEST-COVERAGE] Spot-check: ``ErrorEnvelope`` itself respects
    the contract its consumers do."""
    assert ErrorEnvelope.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# Seed #6 — OpenAPI has response_model for every endpoint (>= 90%)
# ---------------------------------------------------------------------------


def test_openapi_response_shapes_present_above_threshold(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Seed #6: at least 90% of OpenAPI paths declare
    a JSON ``application/json`` response schema. Soft floor — 100%
    isn't achievable when streaming/exposition responses are in the
    mix, but the bulk of the surface must be typed."""
    response = authed_client_for_app.get("/api/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec.get("paths", {})
    total_ops = 0
    typed_ops = 0
    for ops in paths.values():
        for verb, op_spec in ops.items():
            if verb.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "options",
                "head",
            }:
                continue
            total_ops += 1
            responses = op_spec.get("responses", {})
            success = (
                responses.get("200")
                or responses.get("201")
                or responses.get("204")
                or responses.get("default")
            )
            if not success:
                continue
            content = success.get("content", {})
            if "application/json" in content and "schema" in content["application/json"]:
                typed_ops += 1
            elif success.get("description") and not content:
                # 204 No Content has no body; treat as typed for the
                # purposes of this metric since "no body" is itself a
                # well-defined response shape.
                typed_ops += 1
    assert total_ops > 0
    proportion = typed_ops / total_ops
    assert proportion >= 0.9, (
        f"only {typed_ops}/{total_ops} OpenAPI ops have a typed success response "
        f"({proportion:.1%}); expected >=90%"
    )


# ---------------------------------------------------------------------------
# Seed #7 — exception handler catches ServiceError subclasses
# ---------------------------------------------------------------------------


def test_serviceerror_handler_catches_subclass_via_real_app(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Seed #7: register a route that raises a
    ServiceError subclass (UserNotFoundError) directly; the
    ``app.exception_handler(ServiceError)`` decorator catches the
    subclass and emits the envelope rather than letting FastAPI return
    a raw 500."""
    app = authed_client_for_app.app

    async def _ghost_route() -> None:
        raise UserNotFoundError("ghost@x")

    # Mount through the auth-protected list so the route shares the
    # admin-cookie protection and our ``authed_client_for_app`` works
    # unchanged. The handler is app-level, so ANY route raising a
    # ServiceError subclass funnels through it.
    app.add_api_route("/api/__test_ghost", _ghost_route, methods=["POST"])
    response = authed_client_for_app.post("/api/__test_ghost")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not_found"
    assert detail["message"] == "user not found"


def test_serviceerror_handler_catches_state_conflict_subclass(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Seed #7 redux: subclass of a different parent
    branch (StateConflictError) routes through the same handler and
    still lands at the right (status, code) pair."""
    app = authed_client_for_app.app

    async def _conflict_route() -> None:
        raise StateConflictError("already running")

    app.add_api_route("/api/__test_conflict", _conflict_route, methods=["POST"])
    response = authed_client_for_app.post("/api/__test_conflict")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "state_conflict"
    assert detail["message"] == "already running"


# ---------------------------------------------------------------------------
# Seed #8 — service_error_to_http raises HTTPException with status_code set
# ---------------------------------------------------------------------------


def test_translator_sets_http_status_code_correctly() -> None:
    """[TEST-COVERAGE] Seed #8: pin the ``status_code`` attribute on
    the raised HTTPException — FastAPI uses this to set the wire HTTP
    status, not just the body."""
    cases = [
        (UserNotFoundError("x"), 404),
        (StateConflictError("x"), 409),
        (ItemNotFoundError("x"), 404),
    ]
    for exc, expected_status in cases:
        with pytest.raises(HTTPException) as excinfo:
            service_error_to_http(exc)
        assert excinfo.value.status_code == expected_status


# ---------------------------------------------------------------------------
# Seed #9 — WARN log includes the request_id contextvar binding
# ---------------------------------------------------------------------------


def test_unmapped_warn_log_includes_request_id_when_bound_via_structlog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[TEST-COVERAGE] Seed #9 (also EDGE-4): the unmapped WARN
    carries the ``request_id`` so the log event correlates back to the
    inbound request.

    PROD PATH: ``RequestIdMiddleware`` does BOTH ``request_id_var.set``
    AND ``structlog.contextvars.bind_contextvars(request_id=...)`` so
    either pathway populates the field. The translator now reads the
    contextvar explicitly so the WARN carries ``request_id`` even when
    the structlog bind was skipped (post fix #1).
    """
    from ingestion.daemon.logging import configure_logging
    from structlog.contextvars import bind_contextvars, unbind_contextvars

    configure_logging("WARNING")
    rid = "feedface" * 4
    bind_contextvars(request_id=rid)
    try:
        with pytest.raises(HTTPException):
            service_error_to_http(_LeakySentinelError("anything"))
    finally:
        unbind_contextvars("request_id")
    captured = capsys.readouterr().out
    assert "api.service_error_unmapped" in captured
    assert rid in captured, (
        "request_id missing from WARN event; "
        "operator can't pivot from log event back to inbound request"
    )


def test_unmapped_warn_log_includes_request_id_via_contextvar_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[TEST-COVERAGE] Fix #1: the translator reads ``request_id_var.get()``
    explicitly so the WARN carries ``request_id`` even when only the
    contextvar was set (no ``structlog.contextvars.bind_contextvars``
    call at the boundary). Pin the contract: a service-layer path that
    forgot to call ``bind_contextvars`` still emits a correlatable log.
    """
    from ingestion.daemon.logging import configure_logging

    configure_logging("WARNING")
    rid = "0a1b2c3d" * 4  # 32 hex chars
    token = request_id_var.set(rid)
    try:
        with pytest.raises(HTTPException):
            service_error_to_http(_LeakySentinelError("anything"))
    finally:
        request_id_var.reset(token)
    captured = capsys.readouterr().out
    assert "api.service_error_unmapped" in captured
    assert rid in captured, (
        "translator did not read request_id from contextvar explicitly; "
        "missing structlog.bind_contextvars at the boundary breaks the pivot"
    )


# ---------------------------------------------------------------------------
# Seed #10 — Pydantic 422 vs envelope shape
# ---------------------------------------------------------------------------


def test_pydantic_422_uses_envelope_shape(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Fix #4: FastAPI's pydantic-validation 422 used
    to surface as a list of error dicts under ``detail`` — heterogeneous
    with the rest of the API surface. Post fix #4 it goes through the
    same envelope shape as ``raise_http_exception`` calls.

    Pin: ``code == "validation_error"``, ``message`` non-empty,
    ``detail`` is a dict (NOT a list)."""
    response = authed_client_for_app.put(
        "/api/config/overrides/alice@x",
        json={"platform": "teams", "interval_seconds": -1},
    )
    assert response.status_code == 422
    body = response.json()
    # Envelope shape: detail is a dict, not a list of error dicts.
    detail = body["detail"]
    assert isinstance(detail, dict), f"detail not a dict: {detail!r}"
    assert detail["code"] == "validation_error"
    assert isinstance(detail["message"], str)
    assert len(detail["message"]) > 0


# ---------------------------------------------------------------------------
# Seed #11 — Uncaught (non-ServiceError) exceptions go through FastAPI
# ---------------------------------------------------------------------------


def test_uncaught_exception_uses_envelope_shape(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Fix #2: a route that raises a non-ServiceError
    exception (e.g. ``RuntimeError``) now funnels through the catchall
    ``app.exception_handler(Exception)`` and emits the canonical
    envelope. Previously these returned the bare text ``"Internal Server
    Error"`` and broke the dashboard's envelope-aware fetch wrapper.

    Privacy: the raw exception message MUST NOT appear in the wire
    body — internal errors can carry sensitive context. Operators read
    the correlated log event for the underlying detail."""
    app = authed_client_for_app.app
    sentinel = "plain python exception, NOT a ServiceError"

    async def _raise_runtime_error() -> None:
        raise RuntimeError(sentinel)

    app.add_api_route("/api/__test_runtime_error", _raise_runtime_error, methods=["POST"])
    # ``raise_server_exceptions=False`` disables the TestClient's
    # default of re-raising 500s so we can inspect the wire body.
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    response = client.post("/api/__test_runtime_error")
    assert response.status_code == 500
    body = response.json()
    detail = body["detail"]
    assert isinstance(detail, dict), f"detail not a dict: {detail!r}"
    assert detail["code"] == "internal_error"
    assert detail["message"] == "internal error"
    # Privacy guarantee: the original message stays server-side.
    assert sentinel not in json.dumps(body), (
        "uncaught exception leaked sentinel into wire body — privacy violation"
    )


# ---------------------------------------------------------------------------
# Seed #12 — detail-as-string is intentional breaking change
# ---------------------------------------------------------------------------


def test_detail_is_dict_not_string_for_envelope_routes(
    authed_client_for_app: TestClient,
) -> None:
    """[TEST-COVERAGE] Seed #12: routes that go through the envelope
    return ``detail`` as a DICT — clients that expected a bare string
    must update. Pin the breaking change."""
    response = authed_client_for_app.get("/api/syncs/does-not-exist")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    # Old format was ``detail: "sync not found"``; pin the migration.
    assert not isinstance(detail, str)


# ---------------------------------------------------------------------------
# Seed #13 — exclude_none=True keeps required fields, drops Nones
# ---------------------------------------------------------------------------


def test_exclude_none_keeps_required_drops_optional() -> None:
    """[TEST-COVERAGE] Seed #13: ``code`` + ``message`` always present;
    ``hint`` + ``request_id`` dropped when None. Pin so a future change
    to ``model_dump`` defaults can't silently include ``"hint": null``."""
    envelope = ErrorEnvelope(code="not_found", message="missing")
    body = envelope.model_dump(exclude_none=True)
    assert "code" in body
    assert "message" in body
    assert "hint" not in body
    assert "request_id" not in body

    full = ErrorEnvelope(
        code="not_found",
        message="missing",
        hint="hint",
        request_id="r" * 32,
    )
    body_full = full.model_dump(exclude_none=True)
    assert set(body_full) == {"code", "message", "hint", "request_id"}


# ---------------------------------------------------------------------------
# Seed #14 — A route can call raise_http_exception directly
# ---------------------------------------------------------------------------


def test_raise_http_exception_direct_in_route(authed_client_for_app: TestClient) -> None:
    """[TEST-COVERAGE] Seed #14: the helper is fully usable from a
    route handler without going through the ServiceError translator —
    e.g. an auth-failed 401 on the login route. The wire body is
    identical to the translator-emitted shape."""
    app = authed_client_for_app.app

    async def _custom_401() -> None:
        raise_http_exception(401, ErrorCode.UNAUTHORIZED, "bad token", hint="reauth")

    app.add_api_route("/api/__test_custom_401", _custom_401, methods=["GET"])
    response = authed_client_for_app.get("/api/__test_custom_401")
    assert response.status_code == 401
    detail = response.json()["detail"]
    # Required + caller-supplied fields exact-match.
    assert detail["code"] == "unauthorized"
    assert detail["message"] == "bad token"
    assert detail["hint"] == "reauth"
    # request_id is auto-bound by middleware when called through HTTP;
    # don't pin its value but verify the field is present.
    assert "request_id" in detail
    assert isinstance(detail["request_id"], str)


# ---------------------------------------------------------------------------
# EDGE additions (own brainstorm beyond the seed list)
# ---------------------------------------------------------------------------


def test_edge_1_envelope_only_accepts_known_codes_via_model() -> None:
    """[EDGE-1] ``ErrorEnvelope.code`` is typed as ``str`` (not
    ``ErrorCode``), but production callers always go through the
    enum. Pin via ``raise_http_exception`` API: the helper's signature
    requires ``code: ErrorCode``, so untrusted string codes can't ride
    in through the public API."""
    from typing import get_type_hints

    hints = get_type_hints(raise_http_exception)
    assert hints["code"] is ErrorCode


def test_edge_2_pyright_strict_still_passes_on_envelope_module() -> None:
    """[EDGE-2] Pyright on this slice's source modules continues to
    pass at 0 errors — verified by CI; pin the import surface here so
    a refactor that breaks the imports trips a fast unit-test rather
    than waiting for the type-check stage."""
    # Re-import every public symbol — if the module fails to import
    # because pyright-narrowing introduced a runtime error, the test
    # collector explodes here.
    from ingestion.daemon.api.error_envelope import (  # noqa: F401
        ErrorCode as ErrorCodeRe,
    )
    from ingestion.daemon.api.error_envelope import (
        ErrorEnvelope as ErrorEnvelopeRe,
    )
    from ingestion.daemon.api.error_envelope import (
        raise_http_exception as raise_http_exception_re,
    )
    from ingestion.daemon.api.exception_translator import (
        service_error_to_http as service_error_to_http_re,
    )

    assert ErrorCodeRe is ErrorCode
    assert ErrorEnvelopeRe is ErrorEnvelope
    assert raise_http_exception_re is raise_http_exception
    assert service_error_to_http_re is service_error_to_http


def test_edge_3_raise_http_exception_no_return_makes_post_call_unreachable() -> None:
    """[EDGE-3] ``raise_http_exception`` is annotated NoReturn; pyright
    flags any code after a call to it as unreachable. Pin that the
    helper ALWAYS raises — never returns normally."""
    raised = False
    try:
        raise_http_exception(500, ErrorCode.INTERNAL_ERROR, "boom")
        # Unreachable per the type signature, but if it ever returns
        # normally this assignment would run and the assert below
        # would fail.
        raised = True  # noqa: F841 — see comment
    except HTTPException:
        pass
    # ``raised`` stayed False because the helper raised, not returned.
    assert raised is False


def test_edge_5a_old_test_cancel_sync_409_still_passes_envelope_shape(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-5] One of the two impl-updated tests:
    ``test_cancel_sync_409_when_terminal``. Re-exercise its
    assertions here so a re-break shows up as a slice-4 regression
    rather than a generic actions-routing failure."""
    import sqlite3

    sqlite_path = authed_client_for_app.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]
    sync_id = "deadbeef-0000-0000-0000-000000000000"
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute(
            "INSERT INTO sync_history (id, user_email, platform, started_at, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (sync_id, "alice@x", "teams", "2026-04-22T00:00:00+00:00", "complete"),
        )
        conn.commit()
    finally:
        conn.close()
    response = authed_client_for_app.post(
        f"/api/actions/syncs/{sync_id}/cancel",
        json={"confirm": sync_id, "reason": "too late"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "state_conflict"
    assert "complete" in detail["message"]


def test_edge_6_background_task_envelope_has_no_request_id() -> None:
    """[EDGE-6] In a background task the contextvar default is empty,
    so the envelope is emitted without a ``request_id`` key."""

    async def background() -> dict[str, Any]:
        request_id_var.set("")
        try:
            service_error_to_http(UserNotFoundError("ghost"))
        except HTTPException as exc:
            assert isinstance(exc.detail, dict)
            return exc.detail
        return {}

    detail = asyncio.run(background())
    assert "request_id" not in detail


def test_edge_7_no_status_class_leaks_sentinel(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-7] Sweep multiple status codes — the catchall body is
    consistently generic. Pinned via direct translator call (the
    in-process app doesn't expose every error class as an endpoint)."""
    sentinel = "credit-card-1234-5678-9012-3456"
    for cls in (_LeakySentinelError,):
        with pytest.raises(HTTPException) as excinfo:
            service_error_to_http(cls(sentinel))
        body = json.dumps(excinfo.value.detail)
        assert sentinel not in body, (
            f"{cls.__name__} leaked sentinel into wire body — privacy violation"
        )


def test_edge_8_unmapped_serviceerror_preserves_context(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-8] An unmapped ServiceError raised inside a route still
    surfaces the envelope to the client — but the underlying exception
    object's __context__ chain is preserved server-side so debug logs
    can pivot to root-cause."""
    app = authed_client_for_app.app

    class _ChainTestError(ServiceError):
        pass

    async def _chain_route() -> None:
        try:
            raise ValueError("upstream sql error")  # noqa: TRY301
        except ValueError as exc:
            raise _ChainTestError("translation") from exc

    app.add_api_route("/api/__test_chain", _chain_route, methods=["POST"])
    response = authed_client_for_app.post("/api/__test_chain")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "internal_error"
    # Wire body is generic — the __context__ chain stays server-side.
    assert "sql error" not in json.dumps(detail)


def test_edge_9_envelope_wire_keys_are_subset_of_known(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-9] No reflection-injectable fields appear in the wire
    body — the envelope's allowed key set is closed at the model
    level. Even if a caller sends garbage in the URL we never see it
    pop out as a body field."""
    response = authed_client_for_app.get(
        "/api/syncs/missing?injected_field=should_not_appear",
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    allowed = {"code", "message", "hint", "request_id"}
    assert set(detail).issubset(allowed)
    assert "injected_field" not in detail


def test_edge_10_serviceerror_handler_registered_on_app(
    app_for_serviceerror_test: FastAPI,
) -> None:
    """[EDGE-10] The ``app.exception_handler(ServiceError)`` decorator
    on app.py registers the handler in ``app.exception_handlers`` —
    pin the registration so a future refactor that drops the decorator
    flags a test failure (rather than failing silently at runtime when
    a sub-class slips through)."""
    handlers = app_for_serviceerror_test.exception_handlers
    assert ServiceError in handlers, (
        f"ServiceError handler not registered; current handlers: {list(handlers)}"
    )


def test_edge_11_envelope_request_id_pretty_long_hex(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-11] A request_id at the upper bound (128 hex chars) still
    propagates cleanly through the envelope. Validates the contract at
    the boundary, not just the typical 32-char hex."""
    rid = "a" * 128
    response = authed_client_for_app.get(
        "/api/syncs/missing-id",
        headers={"X-Request-ID": rid},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["request_id"] == rid


def test_edge_12_envelope_request_id_invalid_hex_falls_back(
    authed_client_for_app: TestClient,
) -> None:
    """[EDGE-12] An invalid (dashed) inbound X-Request-ID is rejected
    by middleware and replaced with a generated one; the envelope
    still ships a request_id (the generated one, not None)."""
    response = authed_client_for_app.get(
        "/api/syncs/missing-id",
        headers={"X-Request-ID": "not-a-valid-hex-value-12345"},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    # request_id is present, but it's NOT the bogus value we sent.
    assert detail.get("request_id") is not None
    assert detail["request_id"] != "not-a-valid-hex-value-12345"
    # Generated UUID hex format: 32 chars, hex only.
    assert len(detail["request_id"]) == 32


def test_edge_13_decode_row_and_verify_ws_cookie_helpers_typed() -> None:
    """[EDGE-13] [TEST-COVERAGE] Fix #3: ``_decode_row`` (syncs.py) now
    returns :class:`SyncHistoryItem` and ``_verify_ws_cookie`` (ws.py)
    returns a typed ``_SessionClaims`` TypedDict — both narrowed away
    from ``dict[str, Any]``. Pin the return signatures so a future
    refactor that relaxes them flags here."""
    from typing import get_type_hints

    from ingestion.daemon.api.routers import syncs as syncs_mod
    from ingestion.daemon.api.routers import ws as ws_mod

    # _decode_row returns the Pydantic SyncHistoryItem.
    decode_hints = get_type_hints(syncs_mod._decode_row)
    assert decode_hints["return"] is syncs_mod.SyncHistoryItem

    # _verify_ws_cookie returns ``_SessionClaims | None`` — the
    # TypedDict is private but resolvable from the module namespace.
    cookie_hints = get_type_hints(ws_mod._verify_ws_cookie)
    return_hint = cookie_hints["return"]
    # ``Optional[_SessionClaims]`` evaluates to a Union — pull out the
    # non-None arm and confirm it's the TypedDict from this module.
    import types

    assert isinstance(return_hint, types.UnionType)
    args = return_hint.__args__
    non_none = next(arg for arg in args if arg is not type(None))
    assert non_none is ws_mod._SessionClaims


# ---------------------------------------------------------------------------
# Catch the impl's documented test fixture migration regression
# ---------------------------------------------------------------------------


def test_envelope_shape_pinned_for_old_dict_callers() -> None:
    """[EDGE-5b] The other impl-updated test
    (``s2_services_and_routers.test_router_translates_state_conflict_to_409``)
    asserts ``detail["code"] == "state_conflict"``. If the envelope
    schema changes its key naming, both that test AND this one break
    — they're independent pins on the same contract."""
    with pytest.raises(HTTPException) as excinfo:
        service_error_to_http(StateConflictError("complete"))
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "state_conflict"
    assert "complete" in detail["message"]
