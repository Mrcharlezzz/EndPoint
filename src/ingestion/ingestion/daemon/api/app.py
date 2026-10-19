"""FastAPI app factory for the daemon's control-panel API.

``create_app(runtime)`` returns a fully-wired app. The runtime provides the
shared config + cipher + store; tests can pass a test-only context via
``create_app_for_testing``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ingestion.daemon.api.auth import AuthProvider, AuthSettings, make_require_admin
from ingestion.daemon.api.deps import ApiContext
from ingestion.daemon.api.error_envelope import ErrorCode, ErrorEnvelope
from ingestion.daemon.api.exception_translator import service_error_to_http
from ingestion.daemon.api.middleware import (
    DeprecationHeaderMiddleware,
    IdempotencyMiddleware,
    RequestIdMiddleware,
)
from ingestion.daemon.api.middleware.request_id import request_id_var
from ingestion.daemon.api.middlewares import (
    LoginRateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from ingestion.daemon.api.pubsub import EventBus
from ingestion.daemon.api.routers import (
    about_router,
    actions_admin,
    actions_audit,
    actions_maintenance,
    actions_pause,
    actions_processor,
    actions_sync,
    alerts_router,
    audit,
    auth_router,
    config_router,
    credentials,
    errors,
    health_router,
    ingest,
    items,
    logs,
    metrics_router,
    processor,
    queue_router,
    schedule,
    syncs,
    users,
    ws,
)
from ingestion.daemon.api.routers import (
    settings as settings_router,
)
from ingestion.daemon.logging import get_logger
from ingestion.daemon.services.exceptions import ServiceError

if TYPE_CHECKING:
    from ingestion.daemon.config import DaemonConfig
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.store.local import LocalStore


_log = get_logger(__name__)


def _envelope_response(
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    hint: str | None = None,
) -> JSONResponse:
    """Build a JSONResponse carrying the canonical error envelope.

    Wraps the envelope in FastAPI's canonical ``{"detail": ...}`` shape
    so wire format matches the in-router translation path. Reads
    ``request_id`` from the contextvar so background-task scopes (no
    request) emit envelopes without the field — same drop-on-None
    semantics as :func:`raise_http_exception`.
    """
    bound = request_id_var.get()
    envelope = ErrorEnvelope(
        code=code.value,
        message=message,
        hint=hint,
        request_id=bound or None,
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": envelope.model_dump(exclude_none=True)},
    )


def create_app(
    *,
    config: DaemonConfig,
    store: LocalStore,
    cipher: CredentialCipher,
    auth_settings: AuthSettings | None = None,
    events: EventBus | None = None,
    user_refresh_locks: dict[str, asyncio.Lock] | None = None,
) -> FastAPI:
    """Build and return the FastAPI app. Routes require an admin cookie
    except for ``/api/auth/login`` and the OpenAPI docs.
    """
    settings = auth_settings or AuthSettings(
        session_ttl_seconds=config.api.session_ttl_hours * 3600,
    )
    auth_provider = AuthProvider(settings)
    bus = events or EventBus()

    app = FastAPI(
        title="Endpoint Ingestor Daemon",
        # Canonical OpenAPI URL lives under /v1/. The legacy /api/
        # openapi.json + /api/docs paths are mounted as alias routes
        # below so existing tooling that fetches the schema from the
        # old path keeps working for one deprecation cycle.
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )
    app.state.api_context = ApiContext(
        config=config,
        store=store,
        cipher=cipher,
        auth=auth_provider,
        events=bus,
        # Honor the caller's dict if supplied (so the runtime can share it
        # with the refresh loop); otherwise let ApiContext default-factory
        # build a fresh one (test/standalone-app behavior).
        **({"user_refresh_locks": user_refresh_locks} if user_refresh_locks is not None else {}),
    )

    # Middlewares stack in reverse add order (last added is outermost).
    # We want rate-limit to run *inside* the size-limit so an oversized
    # login body gets 413 without burning a token bucket slot. Then CORS
    # wraps both so preflights are handled first.
    app.add_middleware(
        LoginRateLimitMiddleware,
        sqlite_path=config.sqlite_path,
        per_minute=config.api.login_rate_limit_per_minute,
    )
    # IdempotencyMiddleware sits between size-limit (outer, rejects
    # oversized bodies) and rate-limit (inner, blocks brute force on
    # login). Placed here so the request body has already passed the
    # size cap before we buffer it for hashing — guards against an
    # oversized body forcing a multi-megabyte cache lookup. Only
    # mutating verbs with an Idempotency-Key are intercepted; every
    # other request passes straight through.
    app.add_middleware(
        IdempotencyMiddleware,
        sqlite_path=config.sqlite_path,
        ttl_seconds=config.api.idempotency_ttl_seconds,
    )
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=config.api.max_request_bytes,
    )

    if config.api.cors_origins:
        for origin in config.api.cors_origins:
            if origin == "*":
                raise RuntimeError(
                    "cors_origins cannot contain '*' while allow_credentials=True; "
                    "list the exact origins (scheme+host+port) the dashboard runs on"
                )
            if "://" not in origin:
                raise RuntimeError(
                    f"cors_origins entry {origin!r} is missing the scheme; "
                    "use the full origin form e.g. https://dashboard.example.com"
                )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # DeprecationHeaderMiddleware sits between RequestId (outermost) and
    # the body-handling layers. It only injects headers on responses for
    # ``/api/...`` paths so callers that already moved to ``/v1/...``
    # see no change. Adding it before RequestIdMiddleware here means it
    # runs AFTER RequestIdMiddleware on the inbound path (Starlette's
    # add order is reverse-stack order), which is the right order: the
    # request ID is bound first, then the deprecation wrapper rewrites
    # outbound headers on the response start.
    app.add_middleware(DeprecationHeaderMiddleware)

    # RequestIdMiddleware is added LAST so it sits OUTERMOST in the stack
    # (Starlette's add order is reverse-stack order). The ID must be bound
    # before any other middleware runs so rate-limit / size-limit / CORS
    # logs all carry the correlation field, and so the outbound response
    # gets the X-Request-ID header even when an inner middleware short-
    # circuits (e.g. 413 from RequestSizeLimitMiddleware).
    app.add_middleware(RequestIdMiddleware)

    # Defense-in-depth: if a service-layer exception escapes the router
    # (forgot to translate, raised from an inner helper that the router
    # didn't anticipate), this handler still wraps it in the envelope so
    # the dashboard never sees a raw 500-with-Python-traceback page.
    # Registered before routers; FastAPI dispatches in registration order
    # but exception handlers are dict-keyed by exception class so order
    # doesn't matter here — what matters is that the handler is in place
    # by the time the first request arrives.
    @app.exception_handler(ServiceError)
    async def _service_error_handler(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        exc: ServiceError,
    ) -> JSONResponse:
        # ``service_error_to_http`` is ``NoReturn`` — it always raises an
        # ``HTTPException``. We catch it here, convert to a JSONResponse
        # (exception handlers must return a Response, not raise), and
        # ship the envelope wrapped in FastAPI's canonical
        # ``{"detail": ...}`` shape so wire format is identical to the
        # in-router translation path.
        try:
            service_error_to_http(exc)
        except HTTPException as http_exc:
            return JSONResponse(
                status_code=http_exc.status_code,
                content={"detail": http_exc.detail},
                headers=http_exc.headers,
            )
        # Unreachable — service_error_to_http is NoReturn — but a fallback
        # JSONResponse keeps the type-checker happy and gives a defensive
        # 500 instead of None if the contract is ever violated by a future
        # refactor.
        return _envelope_response(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="internal error",
        )

    # Translate FastAPI's pydantic 422 body (a list of error dicts under
    # ``detail``) into the unified envelope shape. Without this handler
    # the dashboard's fetch wrapper sees a heterogeneous error shape:
    # business-rule 422s emit the envelope but body-validation 422s emit
    # a list. The original error list is logged for the operator at WARN
    # but NOT echoed back to the client to keep the wire shape closed.
    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # Render the error list compactly into a single message for the
        # envelope. ``loc`` is a tuple of (body|query|path, fieldname,
        # ...) — keep the shape small enough to surface in a toast.
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = first.get("loc") or ()
        msg = first.get("msg") or "request validation failed"
        # Drop the leading "body" / "query" / "path" segment when present
        # to keep the message focused on the field path.
        loc_str = ".".join(str(segment) for segment in loc[1:]) if len(loc) > 1 else ""
        message = f"{loc_str}: {msg}" if loc_str else str(msg)
        _log.warning(
            "api.request_validation_error",
            error_count=len(errors),
            first_loc=list(loc),
            first_msg=msg,
        )
        return _envelope_response(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=message,
        )

    # Catchall for any uncaught exception (including bare Python
    # ``RuntimeError`` / ``ValueError`` raised outside a typed
    # ``ServiceError``). Without this handler Starlette's default 500
    # handler returns the bare text ``"Internal Server Error"`` and the
    # dashboard's envelope-aware fetch wrapper crashes on the missing
    # ``detail`` key. Log the original at ERROR with full context for
    # operators; the wire body is generic and never echoes the original
    # message — internal errors can carry sensitive context (paths,
    # table names, partial credentials).
    @app.exception_handler(Exception)
    async def _uncaught_exception_handler(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        _log.error(
            "api.uncaught_exception",
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            exc_info=exc,
        )
        return _envelope_response(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="internal error",
        )

    require_admin = make_require_admin(auth_provider)

    # Slice 5: every HTTP router is mounted twice — once under the
    # canonical ``/v1`` prefix and once under the legacy ``/api`` alias.
    # The two prefixes register separate routes in FastAPI's router
    # table; ``DeprecationHeaderMiddleware`` adds the RFC 8594 headers
    # to responses on the legacy alias so clients see a clear sunset
    # signal. Each router file declares its own per-resource prefix
    # (e.g. ``/auth``, ``/users``) so the final paths render as
    # ``/v1/auth/login`` and ``/api/auth/login``.
    canonical_prefix = "/v1"
    legacy_prefix = "/api"

    # Health, readiness, and liveness probes are intentionally PUBLIC: k8s
    # and compose orchestrators can't carry the admin cookie, and the
    # Prometheus convention is unauthenticated. Mounted before the auth
    # router so its routes win against any future wildcard collisions.
    # K8s probes typically expect stable paths so we keep BOTH ``/v1/health``
    # and ``/api/health`` live for one deprecation cycle.
    app.include_router(health_router.router, prefix=canonical_prefix)
    app.include_router(health_router.router, prefix=legacy_prefix)
    # /metrics is public for the same reason — Prometheus scrapes the
    # endpoint without auth. Body is the canonical text exposition format.
    app.include_router(metrics_router.router, prefix=canonical_prefix)
    app.include_router(metrics_router.router, prefix=legacy_prefix)

    # Slice 14 part B: /v1/about returns build + uptime metadata. Public
    # so on-call operators can `curl /v1/about` without a session
    # cookie. No DB calls — must answer on a cold pod even if SQLite
    # is unreachable (that's what /v1/ready signals). Mounted under
    # both the canonical /v1 and legacy /api prefixes per slice-5
    # convention.
    app.include_router(about_router.router, prefix=canonical_prefix)
    app.include_router(about_router.router, prefix=legacy_prefix)

    # Auth router carries login + logout which are intentionally public so
    # unauthenticated clients can obtain a cookie. /whoami lives on a
    # separate router so we can mount it under ``require_admin`` below; the
    # dashboard uses /whoami to gate its chrome and must not trust it
    # without a valid session.
    app.include_router(auth_router.router, prefix=canonical_prefix)
    app.include_router(auth_router.router, prefix=legacy_prefix)

    # Admin-only routers share a common dependency list.
    # The four ``actions_*`` routers all mount under ``<prefix>/actions``;
    # FastAPI dispatches by exact path, so registration order has no
    # functional effect for these (no overlapping wildcards).
    protected = [
        auth_router.whoami_router,
        users.router,
        syncs.router,
        logs.router,
        processor.router,
        audit.router,
        alerts_router.router,
        config_router.router,
        credentials.router,
        errors.router,
        ingest.router,
        items.router,
        queue_router.router,
        schedule.router,
        settings_router.router,
        actions_pause.router,
        actions_sync.router,
        actions_processor.router,
        actions_maintenance.router,
        actions_audit.router,
        actions_admin.router,
    ]
    for r in protected:
        # Attach the admin dependency to every route in each protected router
        # by mounting them with a prerequisite dependency. Each protected
        # router is mounted twice: once under /v1 (canonical) and once
        # under /api (legacy alias with sunset headers).
        app.include_router(r, prefix=canonical_prefix, dependencies=[require_admin])
        app.include_router(r, prefix=legacy_prefix, dependencies=[require_admin])

    # Websockets authenticate themselves (the cookie dependency isn't
    # automatically applied to WS routes in FastAPI the same way); router
    # handler verifies the cookie directly. The dashboard already uses
    # the bare ``/ws/...`` paths and they predate the /api prefix, so we
    # leave them at the root and skip versioning for one cycle.
    app.include_router(ws.router)

    # Legacy OpenAPI alias. ``FastAPI(openapi_url="/v1/openapi.json")``
    # already registers the canonical schema route; we add a short-
    # circuit handler at the legacy path that 200s with the same payload
    # so existing tooling (the dashboard's openapi-typescript codegen
    # in particular) keeps working until the sunset date.
    @app.get("/api/openapi.json", include_in_schema=False)
    async def _legacy_openapi() -> Any:  # pyright: ignore[reportUnusedFunction]
        return app.openapi()

    # Customise the OpenAPI schema so every mutating operation
    # documents the Idempotency-Key header. The middleware enforces
    # the contract globally; this is purely for typed-client codegen
    # so dashboards / external SDKs surface the header in their
    # parameter lists.
    _patch_openapi_with_idempotency_header(app)

    return app


def _patch_openapi_with_idempotency_header(app: FastAPI) -> None:
    """Mutate the cached OpenAPI dict to document the global header.

    FastAPI computes the schema once on first access and caches it on
    ``app.openapi_schema``. We override the ``openapi`` callable so
    the header is added on each (re)compute. The header is an
    OPTIONAL request parameter — codegen treats it as a nullable
    string in the typed client.

    Applies to every operation under POST / PUT / PATCH / DELETE on
    every documented path (both the /v1/ canonical and the /api/
    alias). GET / HEAD / OPTIONS are skipped because the middleware
    short-circuits on those methods.
    """
    original_openapi = app.openapi

    def _custom_openapi() -> dict[str, Any]:
        schema = original_openapi()
        paths = schema.get("paths", {})
        if not isinstance(paths, dict):
            return schema
        header_param = {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "Optional 16-128 hex token. Repeating a request with the same key, "
                "method, path, and body within 24h returns the cached response with "
                "Idempotent-Replay: true. A retry with the same key but a different "
                "body returns 422 idempotency_conflict. See "
                "docs/ingestion/IDEMPOTENCY.md."
            ),
            "schema": {
                "type": "string",
                "pattern": "^[0-9a-fA-F]{16,128}$",
            },
        }
        mutating = {"post", "put", "patch", "delete"}
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in mutating or not isinstance(operation, dict):
                    continue
                params = operation.setdefault("parameters", [])
                if not isinstance(params, list):
                    continue
                # Don't double-add when an operation already declares
                # the header explicitly (future-proofing for routes
                # that pin a tighter format).
                already_present = any(
                    isinstance(p, dict)
                    and p.get("name", "").lower() == "idempotency-key"
                    and p.get("in") == "header"
                    for p in params
                )
                if not already_present:
                    params.append(header_param)
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]
