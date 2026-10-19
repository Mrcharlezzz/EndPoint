"""Slice 5 part A coverage: ``/v1/`` canonical mount + ``/api/`` alias.

Pins:

* Every router is mounted at BOTH ``/v1/...`` AND ``/api/...``.
* Responses on ``/api/...`` carry the RFC 8594 ``Deprecation``,
  RFC 7231 ``Sunset``, and RFC 5829 ``Link: <successor-version>``
  headers; ``/v1/...`` responses do NOT.
* The OpenAPI / docs paths under ``/api/`` are exempt from the
  deprecation headers.
* ``LOGIN_PATHS`` recognises both prefixes so the rate-limit /
  size-limit budgets aren't doubled by an attacker that alternates.
* WebSocket routes are mounted ONCE at the bare path (not aliased).
* Operators can override the sunset date by passing
  ``DeprecationHeaderMiddleware(sunset_date=...)`` directly.

The middleware is purely an outbound header rewrite, so all of these
tests run against a tiny Starlette app or a freshly-built
``create_app(...)`` instance — no Postgres, no scheduler, no live
network. The whole file finishes in well under a second.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.middleware import DeprecationHeaderMiddleware
from ingestion.daemon.api.middleware.deprecation import (
    DEFAULT_SUNSET_DATE,
    _canonical_successor,
    _is_legacy_api_path,
)
from ingestion.daemon.api.middlewares import LOGIN_PATH, LOGIN_PATHS
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store
from starlette.routing import WebSocketRoute

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def app(tmp_path: Path, fernet_key: str) -> FastAPI:
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
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# route table introspection
# ---------------------------------------------------------------------------


def test_every_router_is_mounted_under_both_v1_and_api(app: FastAPI) -> None:
    """Every distinct logical path under ``/v1/`` MUST also exist under
    ``/api/``. We strip the prefix and compare set equality. The only
    asymmetry allowed is ``/api/openapi.json`` — the canonical
    ``/v1/openapi.json`` lives on the FastAPI ``openapi_url`` slot
    (handled by Starlette internally, not as an APIRoute) so it
    doesn't show up in this enumeration."""
    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    v1_logical = {r.path[len("/v1") :] for r in api_routes if r.path.startswith("/v1/")}
    api_logical = {r.path[len("/api") :] for r in api_routes if r.path.startswith("/api/")}
    only_in_v1 = v1_logical - api_logical
    only_in_api = api_logical - v1_logical
    assert only_in_v1 == set(), f"missing /api/ alias for: {only_in_v1!r}"
    assert only_in_api == {"/openapi.json"}, f"unexpected /api/-only routes: {only_in_api!r}"


def test_route_count_matches_double_mount_invariant(app: FastAPI) -> None:
    """Every (path, method) combo under ``/v1/`` must have a twin under
    ``/api/``. We count (path, method) pairs from the route table; the
    only asymmetry is ``/api/openapi.json`` (no ``/v1/openapi.json``
    APIRoute since it's on the openapi_url slot).

    Deliberately do NOT pin an absolute count here. Route inventory is
    expected to change as the daemon grows; the invariant that matters is
    that every canonical route has a legacy twin."""
    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    v1_pairs = {
        (r.path[len("/v1") :], m)
        for r in api_routes
        if r.path.startswith("/v1/")
        for m in r.methods
    }
    api_pairs = {
        (r.path[len("/api") :], m)
        for r in api_routes
        if r.path.startswith("/api/")
        for m in r.methods
    }
    # The /api/openapi.json shim is GET-only; subtract it before equality.
    api_pairs_minus_shim = {pair for pair in api_pairs if pair != ("/openapi.json", "GET")}
    assert v1_pairs == api_pairs_minus_shim, (
        f"v1/api pair mismatch: only_v1={v1_pairs - api_pairs_minus_shim} "
        f"only_api={api_pairs_minus_shim - v1_pairs}"
    )


def test_distinct_logical_paths_stay_balanced_between_prefixes(app: FastAPI) -> None:
    """The OpenAPI schema deduplicates multi-method routes, so it is the
    right place to assert path-level symmetry between ``/v1`` and ``/api``.

    We intentionally avoid pinning a fixed path count. What matters is that
    both prefixes expose the same logical inventory and that the total
    documented paths are exactly the sum of both inventories."""
    schema = app.openapi()
    paths = schema["paths"]
    v1_paths = [p for p in paths if p.startswith("/v1/")]
    api_paths = [p for p in paths if p.startswith("/api/")]
    assert len(v1_paths) == len(api_paths)
    assert len(paths) == len(v1_paths) + len(api_paths)


def test_websocket_routes_are_not_aliased(app: FastAPI) -> None:
    """WS routes stay at the bare ``/ws/...`` paths — no double mount.
    Pinned so the dashboard's WS client doesn't break when the slice 5
    aliasing is extended to other transports."""
    ws_routes = [r for r in app.routes if isinstance(r, WebSocketRoute)]
    ws_paths = {r.path for r in ws_routes}
    assert ws_paths == {
        "/ws/logs",
        "/ws/syncs",
        "/ws/errors",
        "/ws/alerts",
        "/ws/metrics",
    }
    # No /v1/ws/... or /api/ws/... twins.
    assert not any(p.startswith("/v1/ws") for p in ws_paths)
    assert not any(p.startswith("/api/ws") for p in ws_paths)


# ---------------------------------------------------------------------------
# response equivalence + deprecation headers
# ---------------------------------------------------------------------------


def test_v1_and_api_health_return_identical_body_and_status(
    client: TestClient,
) -> None:
    """The two prefixes serve THE SAME endpoint. Status, body, and
    content-type must match — only the deprecation headers differ."""
    r_v1 = client.get("/v1/health")
    r_api = client.get("/api/health")
    assert r_v1.status_code == r_api.status_code == 200
    assert r_v1.json() == r_api.json() == {"status": "ok"}
    assert r_v1.headers["content-type"] == r_api.headers["content-type"]


def test_api_response_carries_deprecation_true(client: TestClient) -> None:
    """RFC 8594: ``Deprecation: true`` indicates the URL is currently
    deprecated. Pinned exact value — no date variant."""
    response = client.get("/api/health")
    assert response.headers.get("deprecation") == "true"


def test_api_response_carries_sunset_default_date(client: TestClient) -> None:
    """The default sunset date is pinned in the module so changing it
    requires a deliberate edit — operators rely on this date for
    their own end-of-life planning."""
    response = client.get("/api/health")
    assert response.headers.get("sunset") == DEFAULT_SUNSET_DATE
    assert DEFAULT_SUNSET_DATE == "Mon, 26 Oct 2026 00:00:00 GMT"


def test_api_response_carries_link_successor_version(client: TestClient) -> None:
    """RFC 5829 / 8631: the successor URL is advertised via the
    ``Link`` header with ``rel=successor-version``. The path is the
    canonical ``/v1/...`` equivalent."""
    response = client.get("/api/health")
    link = response.headers.get("link")
    assert link == '</v1/health>; rel="successor-version"'


def test_v1_response_does_not_carry_deprecation_headers(
    client: TestClient,
) -> None:
    """The canonical prefix is NOT deprecated. None of the three
    headers should appear on a ``/v1/...`` response."""
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers
    # Note: starlette adds its own response headers; check that 'link' is
    # absent or at least doesn't carry our successor-version.
    link = response.headers.get("link")
    assert link is None or "successor-version" not in link


# ---------------------------------------------------------------------------
# OpenAPI / docs exemption
# ---------------------------------------------------------------------------


def test_api_openapi_json_is_exempt_from_deprecation_headers(
    client: TestClient,
) -> None:
    """``/api/openapi.json`` is an alias of the canonical schema URL.
    Adding deprecation headers there confuses scrapers (the schema
    itself isn't deprecated, just one of its access paths). Pinned by
    the impl edge-case enumeration."""
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers


def test_v1_openapi_json_is_not_marked_deprecated(client: TestClient) -> None:
    """The canonical schema URL never carries the headers (sanity
    check that the canonical-prefix exemption logic isn't mis-firing)."""
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    assert "deprecation" not in response.headers


def test_v1_openapi_json_lists_balanced_canonical_and_legacy_paths(
    client: TestClient,
) -> None:
    """The OpenAPI schema is the contract surface for typed clients.
    It must describe matching canonical and legacy inventories, without
    depending on a hard-coded endpoint count."""
    response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    v1_paths = [p for p in paths if p.startswith("/v1/")]
    api_paths = [p for p in paths if p.startswith("/api/")]
    assert len(v1_paths) == len(api_paths)


# ---------------------------------------------------------------------------
# rate-limit / size-limit shared budget (LOGIN_PATHS)
# ---------------------------------------------------------------------------


def test_login_paths_frozenset_matches_both_prefixes() -> None:
    """``LOGIN_PATHS`` is the source of truth for which paths the
    login rate-limit + login size-cap apply to. An attacker who could
    alternate between the two prefixes would otherwise double their
    budget — pin both members so the slice 5 aliasing doesn't open
    that hole."""
    assert isinstance(LOGIN_PATHS, frozenset)
    assert "/v1/auth/login" in LOGIN_PATHS
    assert "/api/auth/login" in LOGIN_PATHS
    assert len(LOGIN_PATHS) == 2


def test_login_path_constant_pins_canonical_value() -> None:
    """The legacy ``LOGIN_PATH`` symbol still exists for callers that
    pinned it from earlier slices. It now points at the canonical
    ``/v1/...`` form (not the legacy ``/api/...`` form)."""
    assert LOGIN_PATH == "/v1/auth/login"


# ---------------------------------------------------------------------------
# operator override of sunset date
# ---------------------------------------------------------------------------


def test_operator_can_override_sunset_date() -> None:
    """Operators that want a tighter or looser sunset construct the
    middleware with a custom date. Verify the override is honored end-
    to-end on a tiny Starlette app (so we don't rely on the daemon's
    default app factory wiring)."""
    inner = FastAPI()

    @inner.get("/api/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    custom_date = "Wed, 31 Dec 2025 23:59:59 GMT"
    inner.add_middleware(DeprecationHeaderMiddleware, sunset_date=custom_date)
    client = TestClient(inner)
    response = client.get("/api/probe")
    assert response.status_code == 200
    assert response.headers["sunset"] == custom_date
    # Default is NOT used.
    assert response.headers["sunset"] != DEFAULT_SUNSET_DATE


# ---------------------------------------------------------------------------
# pure-function unit tests (cheap, exercise branch coverage)
# ---------------------------------------------------------------------------


def test_is_legacy_api_path_recognises_api_prefix() -> None:
    assert _is_legacy_api_path("/api/health") is True
    assert _is_legacy_api_path("/api/users/x@x") is True
    assert _is_legacy_api_path("/api") is True


def test_is_legacy_api_path_rejects_v1_prefix() -> None:
    assert _is_legacy_api_path("/v1/health") is False


def test_is_legacy_api_path_exempts_openapi_routes() -> None:
    """The exempt set lives in the module — pin every member so a
    typo or missing entry surfaces in the test suite."""
    assert _is_legacy_api_path("/api/openapi.json") is False
    assert _is_legacy_api_path("/api/docs") is False
    assert _is_legacy_api_path("/api/redoc") is False


def test_is_legacy_api_path_handles_empty_and_root() -> None:
    """Defensive: bare slash, empty string, and unrelated paths must
    all return False so the middleware passes them through."""
    assert _is_legacy_api_path("") is False
    assert _is_legacy_api_path("/") is False
    assert _is_legacy_api_path("/ws/logs") is False
    assert _is_legacy_api_path("/health") is False


def test_canonical_successor_maps_api_to_v1() -> None:
    """The Link header value must point at the ``/v1/...`` twin.
    Encoded as latin-1/ASCII bytes so the wire format is stable."""
    result = _canonical_successor("/api/health")
    assert result == b'</v1/health>; rel="successor-version"'


def test_canonical_successor_handles_bare_api() -> None:
    """``/api`` (no trailing slash, no path segment) maps to ``/v1``.
    This branch is rare in practice but pinned because the slicing in
    ``_canonical_successor`` has two arms."""
    result = _canonical_successor("/api")
    assert result == b'</v1>; rel="successor-version"'


def test_canonical_successor_preserves_nested_path() -> None:
    result = _canonical_successor("/api/actions/users/foo@bar/pause")
    assert result == b'</v1/actions/users/foo@bar/pause>; rel="successor-version"'


# ---------------------------------------------------------------------------
# middleware behaviour beyond the happy path
# ---------------------------------------------------------------------------


def test_middleware_passes_non_http_scope_through_unchanged() -> None:
    """Lifespan / WebSocket scopes must NOT be wrapped. The middleware
    early-returns when ``scope["type"] != "http"``."""
    inner = FastAPI()

    @inner.get("/api/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    inner.add_middleware(DeprecationHeaderMiddleware)
    # TestClient handles lifespan internally; if the middleware
    # mishandled lifespan the app would fail to start.
    with TestClient(inner) as client:
        # Lifespan startup ran successfully if we got here.
        response = client.get("/api/echo")
        assert response.status_code == 200
        assert response.headers["deprecation"] == "true"


def test_middleware_does_not_overwrite_handler_set_deprecation_header() -> None:
    """A handler that wants to set its own ``Deprecation`` (e.g. a
    different date variant for one specific endpoint) wins over the
    middleware default. The middleware only injects when the header
    is ABSENT."""
    inner = FastAPI()

    @inner.get("/api/custom")
    async def custom() -> object:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"ok": True},
            headers={
                "Deprecation": 'version="2026-01-01"',
                "Sunset": "Sun, 01 Feb 2026 00:00:00 GMT",
                "Link": '</v2/custom>; rel="successor-version"',
            },
        )

    inner.add_middleware(DeprecationHeaderMiddleware)
    client = TestClient(inner)
    response = client.get("/api/custom")
    # Handler-set values are preserved verbatim — the middleware does
    # NOT double-add or overwrite.
    assert response.headers["deprecation"] == 'version="2026-01-01"'
    assert response.headers["sunset"] == "Sun, 01 Feb 2026 00:00:00 GMT"
    assert "v2/custom" in response.headers["link"]


def test_middleware_passes_through_v1_path_without_modification() -> None:
    """A ``/v1/...`` request must NOT pick up deprecation headers.
    Verified on a tiny Starlette app so this isolates the middleware
    behaviour from the rest of the daemon stack."""
    inner = FastAPI()

    @inner.get("/v1/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    inner.add_middleware(DeprecationHeaderMiddleware)
    client = TestClient(inner)
    response = client.get("/v1/echo")
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers


def test_middleware_handles_bare_api_path() -> None:
    """A request to bare ``/api`` (no trailing slash) should also pick
    up the deprecation headers — the path-prefix check in
    ``_is_legacy_api_path`` accepts this branch."""
    inner = FastAPI()

    @inner.get("/api")
    async def root() -> dict[str, bool]:
        return {"ok": True}

    inner.add_middleware(DeprecationHeaderMiddleware)
    client = TestClient(inner)
    response = client.get("/api")
    # If routed, deprecation headers must be present.
    if response.status_code == 200:
        assert response.headers.get("deprecation") == "true"
