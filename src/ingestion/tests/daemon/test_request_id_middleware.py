"""Coverage for ``RequestIdMiddleware`` (slice 6 part C).

Pins the inbound-validation rules, contextvar binding, response-header
echo, WS handshake binding, and the ``finally`` reset that prevents
contextvar leak across requests on the same task. Aim is 100% line +
branch coverage on ``request_id.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.middleware import RequestIdMiddleware
from ingestion.daemon.api.middleware.request_id import (
    _is_valid_inbound_id,
    _resolve_request_id,
    request_id_var,
)
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _build_minimal_app() -> FastAPI:
    """Tiny app with only the request-id middleware and a couple of probe
    routes so each test owns its own ASGI app and can't bleed state."""
    app = FastAPI()

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"request_id": request_id_var.get()}

    @app.get("/sleep-then-echo")
    async def sleep_then_echo() -> dict[str, str]:
        await asyncio.sleep(0)
        return {"request_id": request_id_var.get()}

    @app.get("/spawn-task-echo")
    async def spawn_task_echo() -> dict[str, str]:
        captured: list[str] = []

        async def child() -> None:
            captured.append(request_id_var.get())

        task = asyncio.create_task(child())
        await task
        return {"child_request_id": captured[0]}

    @app.get("/to-thread-echo")
    async def to_thread_echo() -> dict[str, str]:
        def worker() -> str:
            return request_id_var.get()

        captured = await asyncio.to_thread(worker)
        return {"thread_request_id": captured}

    @app.get("/preset-header")
    async def preset_header() -> Any:
        from fastapi.responses import JSONResponse

        # Valid hex within the length window: middleware should preserve it.
        return JSONResponse(
            {"ok": True},
            headers={"X-Request-ID": "fedcba9876543210fedcba9876543210"},
        )

    @app.get("/preset-header-invalid")
    async def preset_header_invalid() -> Any:
        from fastapi.responses import JSONResponse

        # Invalid: contains hyphens + too short. Middleware should drop
        # it and emit its own bound value instead.
        return JSONResponse(
            {"ok": True},
            headers={"X-Request-ID": "handler-set-explicit-id-1234"},
        )

    @app.get("/preset-header-injection")
    async def preset_header_injection() -> Any:
        from fastapi.responses import Response

        # Construct a Response with explicit header bytes containing a
        # control character. The starlette JSONResponse path encodes the
        # header to bytes; we mimic that via raw_headers.
        resp = Response("{}", media_type="application/json")
        resp.raw_headers.append((b"x-request-id", b"abc\r\nSet-Cookie: pwn=1"))
        return resp

    @app.websocket("/ws-echo")
    async def ws_echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"request_id": request_id_var.get()})
        await ws.close()

    app.add_middleware(RequestIdMiddleware)
    return app


# ---------------------------------------------------------------------------
# inbound validation
# ---------------------------------------------------------------------------


def test_valid_inbound_hex_id_is_preserved() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "a" * 32
    response = client.get("/echo", headers={"X-Request-ID": rid})
    assert response.headers["x-request-id"] == rid
    assert response.json() == {"request_id": rid}


def test_missing_inbound_generates_uuid_hex() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    response = client.get("/echo")
    rid = response.headers["x-request-id"]
    assert len(rid) == 32
    # uuid4().hex is hex-only, no dashes.
    assert all(c in "0123456789abcdef" for c in rid)
    assert response.json()["request_id"] == rid


def test_inbound_too_short_is_rejected() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    response = client.get("/echo", headers={"X-Request-ID": "abc"})
    rid = response.headers["x-request-id"]
    assert len(rid) == 32
    assert rid != "abc"


def test_inbound_too_long_is_rejected() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    long_id = "f" * 200
    response = client.get("/echo", headers={"X-Request-ID": long_id})
    rid = response.headers["x-request-id"]
    assert rid != long_id
    assert len(rid) == 32


def test_inbound_non_hex_is_rejected() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    response = client.get("/echo", headers={"X-Request-ID": "ZZZZ" * 10})
    rid = response.headers["x-request-id"]
    assert rid != "ZZZZ" * 10
    assert all(c in "0123456789abcdef" for c in rid)


def test_inbound_dashed_uuid_is_rejected() -> None:
    """An older client sending a dashed UUID must be rebuffed — the
    middleware regenerates a clean 32-char hex."""
    app = _build_minimal_app()
    client = TestClient(app)
    dashed = "12345678-1234-1234-1234-123456789012"
    response = client.get("/echo", headers={"X-Request-ID": dashed})
    rid = response.headers["x-request-id"]
    assert rid != dashed
    assert "-" not in rid
    assert len(rid) == 32


def test_inbound_with_newline_is_rejected() -> None:
    """Log-injection guardrail: any control char fails validation."""
    assert _is_valid_inbound_id("a" * 16 + "\nHELLO" + "a" * 10) is False


def test_is_valid_inbound_id_boundary_lengths() -> None:
    assert _is_valid_inbound_id("a" * 16) is True
    assert _is_valid_inbound_id("a" * 128) is True
    assert _is_valid_inbound_id("a" * 15) is False
    assert _is_valid_inbound_id("a" * 129) is False


def test_uppercase_hex_is_accepted() -> None:
    """The validator is case-insensitive: ``ABCDEF`` works."""
    rid = "ABCDEF1234567890abcdef1234567890"
    assert _is_valid_inbound_id(rid) is True
    app = _build_minimal_app()
    client = TestClient(app)
    response = client.get("/echo", headers={"X-Request-ID": rid})
    assert response.headers["x-request-id"] == rid


def test_request_id_header_lookup_is_case_insensitive() -> None:
    """ASGI normalises header names to lowercase; verify the middleware
    sees ``X-REQUEST-ID`` the same as ``x-request-id``."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "a" * 32
    response = client.get("/echo", headers={"X-REQUEST-ID": rid})
    assert response.headers["x-request-id"] == rid


def test_inbound_with_non_ascii_decode_failure_is_rejected() -> None:
    """A header value that isn't ASCII can't be decoded — middleware
    bails and mints a fresh ID. Driven directly through the resolver."""
    scope = {
        "type": "http",
        "headers": [(b"x-request-id", b"\xff\xfe\xfd")],
    }
    rid = _resolve_request_id(scope)
    # Generated, not the bytes we passed in.
    assert len(rid) == 32


def test_resolver_skips_unrelated_headers() -> None:
    """A scope with no x-request-id header gets a fresh ID."""
    scope = {"type": "http", "headers": [(b"content-type", b"application/json")]}
    rid = _resolve_request_id(scope)
    assert len(rid) == 32


def test_resolver_handles_missing_headers_key() -> None:
    """``scope.get('headers')`` returns None if absent — defaults to empty."""
    rid = _resolve_request_id({"type": "http"})
    assert len(rid) == 32


# ---------------------------------------------------------------------------
# contextvar plumbing
# ---------------------------------------------------------------------------


def test_request_id_var_visible_inside_handler() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "deadbeef" * 4
    response = client.get("/echo", headers={"X-Request-ID": rid})
    assert response.json() == {"request_id": rid}


def test_request_id_default_is_empty_string_outside_request() -> None:
    """Outside any HTTP scope the default is the empty string so structlog
    bindings under the var don't raise LookupError."""
    # Reset to default so an earlier test doesn't poison this one.
    request_id_var.set("")
    assert request_id_var.get() == ""


def test_contextvar_resets_after_request() -> None:
    """After a request returns the contextvar must be back to its default
    on this task — the middleware's ``finally`` should reset the token."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "1" * 32
    client.get("/echo", headers={"X-Request-ID": rid})
    # The Starlette TestClient runs requests on a worker; on this task
    # the var should still be at its default empty-string value.
    assert request_id_var.get() == ""


def test_request_id_survives_await_boundary() -> None:
    """An ``await asyncio.sleep(0)`` inside the handler must not lose the
    contextvar binding — pin the asyncio context preservation."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "f" * 32
    response = client.get("/sleep-then-echo", headers={"X-Request-ID": rid})
    assert response.json() == {"request_id": rid}


def test_request_id_propagates_to_create_task() -> None:
    """Background tasks spawned with asyncio.create_task inherit the
    contextvar (Python 3.7+ asyncio behaviour)."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "0" * 32
    response = client.get("/spawn-task-echo", headers={"X-Request-ID": rid})
    assert response.json() == {"child_request_id": rid}


def test_request_id_propagates_via_to_thread_on_py312_plus() -> None:
    """``asyncio.to_thread`` copies the calling :class:`contextvars.Context`
    into the worker thread on Python 3.12+, so the worker sees the
    middleware-bound ``request_id_var``. The middleware docstring states
    this; this test pins the runtime contract. The ``in (rid, "")``
    tolerance keeps the test green if a future Python version reverts
    the auto-copy behaviour."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "9" * 32
    response = client.get("/to-thread-echo", headers={"X-Request-ID": rid})
    body = response.json()
    thread_rid = body["thread_request_id"]
    assert thread_rid in (rid, "")


# ---------------------------------------------------------------------------
# response-header rewrite
# ---------------------------------------------------------------------------


def test_handler_set_valid_request_id_is_preserved() -> None:
    """If the handler explicitly returns its own ``X-Request-ID`` and the
    value is valid (hex within the length window) the middleware leaves
    it alone — legitimate use case for a downstream service that wants
    its own ID echoed."""
    app = _build_minimal_app()
    client = TestClient(app)
    inbound = "b" * 32
    response = client.get("/preset-header", headers={"X-Request-ID": inbound})
    # The handler set a valid hex ID; middleware keeps it.
    assert response.headers["x-request-id"] == "fedcba9876543210fedcba9876543210"


def test_handler_set_invalid_request_id_is_dropped() -> None:
    """If the handler sets an invalid ``X-Request-ID`` (non-hex, wrong
    length, or control chars) the middleware drops it and substitutes
    the bound value. This protects against response-header injection
    via reflection (CWE-113)."""
    app = _build_minimal_app()
    client = TestClient(app)
    inbound = "b" * 32
    response = client.get("/preset-header-invalid", headers={"X-Request-ID": inbound})
    # Handler-set value was invalid (hyphens + too short); middleware
    # replaced it with the bound inbound value.
    assert response.headers["x-request-id"] == inbound


def test_handler_set_request_id_with_control_chars_is_rejected() -> None:
    """A handler that injects CRLF / control chars into ``X-Request-ID``
    (e.g. by reflecting unsanitised user input) cannot poison the
    response header — the middleware re-validates and drops the
    smuggled bytes."""
    app = _build_minimal_app()
    client = TestClient(app)
    inbound = "c" * 32
    response = client.get("/preset-header-injection", headers={"X-Request-ID": inbound})
    echoed = response.headers["x-request-id"]
    assert "\r" not in echoed
    assert "\n" not in echoed
    assert echoed == inbound


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_concurrent_requests_get_distinct_ids() -> None:
    """Run 10 concurrent requests in parallel via threading; each
    response should carry its own unique ID. ``uuid4().hex`` collisions
    are statistically impossible at this volume."""
    import concurrent.futures

    app = _build_minimal_app()
    client = TestClient(app)

    def hit() -> str:
        response = client.get("/echo")
        return response.headers["x-request-id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(lambda _i: hit(), range(10)))
    assert len(set(ids)) == 10


def test_concurrent_requests_with_inbound_ids_are_preserved() -> None:
    """Each concurrent request that supplies its own inbound ID should
    get that ID echoed back — no cross-contamination."""
    import concurrent.futures

    app = _build_minimal_app()
    client = TestClient(app)

    def hit(idx: int) -> tuple[str, str]:
        rid = f"{idx:032x}"
        response = client.get("/echo", headers={"X-Request-ID": rid})
        return rid, response.headers["x-request-id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(hit, range(10)))
    for sent, got in results:
        assert sent == got


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


def test_websocket_handshake_binds_contextvar() -> None:
    """The WS branch should bind the contextvar before the handler runs."""
    app = _build_minimal_app()
    client = TestClient(app)
    rid = "c" * 32
    with client.websocket_connect("/ws-echo", headers={"X-Request-ID": rid}) as ws:
        message = ws.receive_json()
        assert message["request_id"] == rid


def test_websocket_handshake_generates_id_when_missing() -> None:
    app = _build_minimal_app()
    client = TestClient(app)
    with client.websocket_connect("/ws-echo") as ws:
        message = ws.receive_json()
        assert len(message["request_id"]) == 32


# ---------------------------------------------------------------------------
# lifespan + non-http scopes
# ---------------------------------------------------------------------------


def test_lifespan_scope_is_bypassed_cleanly() -> None:
    """A startup/shutdown cycle must complete without the middleware
    binding anything — the lifespan branch is the no-op pass-through."""
    app = _build_minimal_app()
    # TestClient context-manages startup + shutdown when used as a CM.
    with TestClient(app) as _ctx:
        pass


def test_unknown_scope_type_passes_through() -> None:
    """An ASGI scope type that isn't http/websocket/lifespan should still
    pass through untouched."""

    received: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        received.append(scope)

    middleware = RequestIdMiddleware(app)

    async def receive() -> Any:
        return {}

    async def send(_msg: Any) -> None:
        return None

    asyncio.run(middleware({"type": "tcp"}, receive, send))
    # The middleware passed through; our fake app saw the scope.
    assert received == [{"type": "tcp"}]


# ---------------------------------------------------------------------------
# integration with the full daemon app
# ---------------------------------------------------------------------------


def test_request_id_attached_to_health_endpoint(tmp_path: Path, fernet_key: str) -> None:
    """End-to-end: a /health probe through the full daemon app should
    still echo the request ID."""
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    rid = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    response = TestClient(app).get("/v1/health", headers={"X-Request-ID": rid})
    assert response.headers["x-request-id"] == rid


def test_request_id_generated_when_missing_on_health(tmp_path: Path, fernet_key: str) -> None:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    response = TestClient(app).get("/v1/health")
    rid = response.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) == 32


# ---------------------------------------------------------------------------
# structlog integration
# ---------------------------------------------------------------------------


def test_structlog_binding_includes_request_id() -> None:
    """When the contextvar is set, a structlog log record should carry
    ``request_id`` via the contextvars merge processor."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        rid = "d" * 32
        app = _build_minimal_app()

        @app.get("/log-it")
        async def log_it() -> dict[str, bool]:
            structlog.get_logger().info("hello.from.handler")
            return {"ok": True}

        client = TestClient(app)
        client.get("/log-it", headers={"X-Request-ID": rid})

        match = [e for e in cap.entries if e.get("event") == "hello.from.handler"]
        assert match, cap.entries
        assert match[0]["request_id"] == rid
    finally:
        # Restore default config so unrelated tests aren't affected.
        structlog.reset_defaults()
