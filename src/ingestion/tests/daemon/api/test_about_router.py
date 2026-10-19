"""Slice 14 part B coverage: ``GET /v1/about`` build + uptime introspection.

Pins:

* Wire shape: ``{version, git_sha, built_at, python_version, uptime_seconds}``.
* Both ``/v1/about`` (canonical) and ``/api/about`` (legacy alias) work.
* ``AboutResponse`` declares ``extra="forbid"`` (extras are rejected).
* Build-time env vars (``ENDPOINT_VERSION`` / ``_GIT_SHA`` / ``_BUILT_AT``)
  populate when set; absent / empty / whitespace-only values fall back
  to the literal string ``"unknown"`` (never the ``"$ENV_VAR"`` placeholder).
* ``python_version`` is the actual ``sys.version_info`` joined.
* ``uptime_seconds`` advances when the process-start anchor moves backwards
  (i.e. real time elapses) and is non-negative.
* The endpoint is PUBLIC: a request with no session cookie still returns 200.
* Non-GET verbs (POST / PUT / DELETE) return 405.
* ``/v1/about`` does NOT carry slice-5 deprecation headers; ``/api/about``
  carries the trio (``Deprecation`` + ``Sunset`` + ``Link``).
* CLI ``status`` integration: ``endpoint-admin status`` enriches the JSON
  payload with the daemon's about block, and gracefully degrades to
  ``about=null`` on a 404 from an older daemon (rolling-deploy case).

Goal: 100% line + branch coverage of ``about_router.py``.

The whole file is pure Starlette / FastAPI test wiring — no Postgres, no
scheduler. Runs in well under a second on a cold cache.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from ingestion.cli import admin
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.routers import about_router as about_module
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store
from pydantic import ValidationError
from typer.testing import CliRunner

if TYPE_CHECKING:
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Build-env scrubber: every test enters the suite without ENDPOINT_VERSION /
# _GIT_SHA / _BUILT_AT polluted from the host. Tests that need a value set
# it explicitly via monkeypatch.setenv. autouse so a forgotten test still
# starts clean.
# ---------------------------------------------------------------------------


_BUILD_ENV_VARS = ("ENDPOINT_VERSION", "ENDPOINT_GIT_SHA", "ENDPOINT_BUILT_AT")


@pytest.fixture(autouse=True)
def _scrub_build_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in _BUILD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


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
# Wire shape: GET /v1/about returns 200 + the canonical five fields
# ---------------------------------------------------------------------------


_REQUIRED_KEYS = frozenset({"version", "git_sha", "built_at", "python_version", "uptime_seconds"})


def test_v1_about_returns_200_with_canonical_shape(client: TestClient) -> None:
    """[TEST-COVERAGE] The canonical endpoint always returns the five
    required fields and nothing else. We pin set-equality so any
    accidental new field surfaces here before clients start consuming it."""
    response = client.get("/v1/about")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == _REQUIRED_KEYS


def test_api_about_alias_returns_same_body(client: TestClient) -> None:
    """[TEST-COVERAGE] Slice 5's double-mount convention: the legacy
    ``/api/...`` prefix is a logical alias of ``/v1/...``. The bodies
    must match (deprecation headers diverge — pinned separately)."""
    response_v1 = client.get("/v1/about")
    response_api = client.get("/api/about")
    assert response_v1.status_code == response_api.status_code == 200
    # uptime_seconds may differ by a millisecond between the two calls; pin
    # the rest of the keys for byte-exact equality, then assert uptime is
    # close enough that the field clearly comes from the same anchor.
    body_v1 = response_v1.json()
    body_api = response_api.json()
    for key in _REQUIRED_KEYS - {"uptime_seconds"}:
        assert body_v1[key] == body_api[key], f"{key} divergent"
    assert abs(body_v1["uptime_seconds"] - body_api["uptime_seconds"]) <= 1


# ---------------------------------------------------------------------------
# Pydantic response model: extra="forbid"
# ---------------------------------------------------------------------------


def test_about_response_model_forbids_extras() -> None:
    """[TEST-COVERAGE] ``AboutResponse`` carries ``ConfigDict(extra='forbid')``
    so unknown keys raise rather than silently round-trip. Walk the
    config rather than relying on a runtime probe."""
    extra = about_module.AboutResponse.model_config.get("extra")
    assert extra == "forbid", f"extra is {extra!r} not 'forbid'"


def test_about_response_model_rejects_unknown_field() -> None:
    """[TEST-COVERAGE] Sanity check that the ``extra='forbid'`` config
    actually trips a ValidationError. Not strictly necessary on top of
    the config-walk above, but pins the runtime contract too — protects
    against a future Pydantic upgrade where the keyword shape changes."""
    with pytest.raises(ValidationError):
        about_module.AboutResponse.model_validate(
            {
                "version": "1.0.0",
                "git_sha": "abc",
                "built_at": "2026-04-26",
                "python_version": "3.12.4",
                "uptime_seconds": 1,
                "smuggled": "value",
            }
        )


# ---------------------------------------------------------------------------
# Build-meta env var resolution
# ---------------------------------------------------------------------------


def test_build_env_vars_populate_when_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] When the three Dockerfile ARG-derived env vars are
    set, the response echoes them verbatim. This is the happy path for
    a CI-tagged image build."""
    monkeypatch.setenv("ENDPOINT_VERSION", "1.4.2")
    monkeypatch.setenv("ENDPOINT_GIT_SHA", "9a0c3f1b2d4e")
    monkeypatch.setenv("ENDPOINT_BUILT_AT", "2026-04-26T14:30:00Z")
    body = client.get("/v1/about").json()
    assert body["version"] == "1.4.2"
    assert body["git_sha"] == "9a0c3f1b2d4e"
    assert body["built_at"] == "2026-04-26T14:30:00Z"


def test_build_env_vars_fall_back_to_unknown_when_absent(client: TestClient) -> None:
    """[TEST-COVERAGE] A local ``docker build`` with no --build-arg flags
    leaves the three vars at their image default. The endpoint MUST
    answer ``"unknown"`` rather than the literal placeholder so on-call
    operators see the missing-tag signal at a glance."""
    # autouse fixture above guarantees the vars are unset.
    body = client.get("/v1/about").json()
    assert body["version"] == "unknown"
    assert body["git_sha"] == "unknown"
    assert body["built_at"] == "unknown"


def test_empty_env_var_falls_back_to_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Edge: ``ENDPOINT_VERSION=""`` (an explicitly empty
    Dockerfile ARG) is treated identically to "absent" — the wire value
    is ``"unknown"``, never the empty string. Keeps the wire shape
    closed: clients always see a non-empty string for this field."""
    monkeypatch.setenv("ENDPOINT_VERSION", "")
    body = client.get("/v1/about").json()
    assert body["version"] == "unknown"


def test_whitespace_only_env_var_falls_back_to_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Edge: a value of ``"   "`` (operator typo'd a
    Docker --build-arg into a literal whitespace string) folds the
    same way as an absent value — the strip + emptiness check in
    ``_lookup_build_meta`` is what closes this hole."""
    monkeypatch.setenv("ENDPOINT_VERSION", "   ")
    monkeypatch.setenv("ENDPOINT_GIT_SHA", "\t\n ")
    body = client.get("/v1/about").json()
    assert body["version"] == "unknown"
    assert body["git_sha"] == "unknown"


def test_partial_env_vars_resolve_independently(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Edge: a CI pipeline that sets two of the three
    vars (e.g. forgot BUILT_AT) gets two real values and one
    ``"unknown"``. Per-field resolution is independent — pin so a
    future refactor that batches lookups doesn't cross-contaminate."""
    monkeypatch.setenv("ENDPOINT_VERSION", "2.0.0")
    monkeypatch.setenv("ENDPOINT_GIT_SHA", "deadbeef")
    body = client.get("/v1/about").json()
    assert body["version"] == "2.0.0"
    assert body["git_sha"] == "deadbeef"
    assert body["built_at"] == "unknown"


def test_lookup_build_meta_pure_function() -> None:
    """[TEST-COVERAGE] Direct probe of the helper. Guards branch
    coverage for the strip-then-emptiness check without going through
    the FastAPI stack, so the path is exercised even if the router is
    refactored to use the helper differently."""
    import os

    # Empty -> "unknown".
    os.environ.pop("PYTEST_ABOUT_PROBE", None)
    assert about_module._lookup_build_meta("PYTEST_ABOUT_PROBE") == "unknown"
    # Whitespace -> "unknown".
    os.environ["PYTEST_ABOUT_PROBE"] = "   "
    try:
        assert about_module._lookup_build_meta("PYTEST_ABOUT_PROBE") == "unknown"
    finally:
        os.environ.pop("PYTEST_ABOUT_PROBE", None)
    # Real value -> stripped value.
    os.environ["PYTEST_ABOUT_PROBE"] = "  v1.0  "
    try:
        assert about_module._lookup_build_meta("PYTEST_ABOUT_PROBE") == "v1.0"
    finally:
        os.environ.pop("PYTEST_ABOUT_PROBE", None)


# ---------------------------------------------------------------------------
# python_version field
# ---------------------------------------------------------------------------


def test_python_version_matches_runtime(client: TestClient) -> None:
    """[TEST-COVERAGE] The python_version field is sourced from
    ``platform.python_version()`` — an exact string match against the
    interpreter that's actually serving the request. This catches a
    bug where someone hard-coded a version string."""
    body = client.get("/v1/about").json()
    expected = platform.python_version()
    assert body["python_version"] == expected
    # And independently spot-check that the X.Y prefix matches sys.
    expected_major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert body["python_version"].startswith(expected_major_minor)


# ---------------------------------------------------------------------------
# uptime_seconds: monotonically non-decreasing across calls
# ---------------------------------------------------------------------------


def test_uptime_seconds_is_non_negative(client: TestClient) -> None:
    """[TEST-COVERAGE] ``max(0, int(...))`` floors at 0. Pin the
    contract so a clock-skew bug or a refactor that drops the floor
    can't ship a negative integer to clients."""
    body = client.get("/v1/about").json()
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_uptime_seconds_advances_when_anchor_moves_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] We can't freeze time without freezegun (not in
    deps), but we CAN move the process-start anchor backwards by
    monkeypatching the imported reference. Two sequential probes with
    a 100-second backwards shift between them must show a strictly
    larger uptime in the second response. This is the production
    invariant: as the process runs longer, ``time.monotonic() -
    _PROCESS_START`` grows."""
    # First call: anchor stays at the import-time value.
    body1 = client.get("/v1/about").json()
    uptime1 = body1["uptime_seconds"]

    # Move the anchor 100s backwards. The router computes
    # ``time.monotonic() - _PROCESS_START_MONOTONIC`` where
    # _PROCESS_START_MONOTONIC was imported into the about_router
    # module's namespace from metrics_router. Patch the local symbol
    # in about_router so the next request sees the rewound anchor.
    monkeypatch.setattr(
        about_module,
        "_PROCESS_START_MONOTONIC",
        about_module._PROCESS_START_MONOTONIC - 100.0,
    )
    body2 = client.get("/v1/about").json()
    uptime2 = body2["uptime_seconds"]

    # uptime2 must be at least 100 greater (allow >= because the
    # monotonic clock also moved forward while the test ran).
    assert uptime2 >= uptime1 + 100


# ---------------------------------------------------------------------------
# Auth: endpoint is public (no session cookie required)
# ---------------------------------------------------------------------------


def test_about_is_public_no_session_cookie_required(client: TestClient) -> None:
    """[TEST-COVERAGE] The endpoint is mounted before the auth router and
    is intentionally never auth-gated — k8s probes and curl-from-bastion
    both rely on this. Verify by hitting the path WITHOUT performing the
    /api/auth/login dance first; a 200 confirms no admin cookie was
    required."""
    # The default `client` fixture has not logged in.
    assert client.cookies.get("endpoint_admin_session") is None
    response = client.get("/v1/about")
    assert response.status_code == 200
    response_api = client.get("/api/about")
    assert response_api.status_code == 200


# ---------------------------------------------------------------------------
# HTTP method discipline: only GET is allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_methods_return_405(client: TestClient, method: str) -> None:
    """[TEST-COVERAGE] The router declares only ``@router.get(...)``;
    every other verb must hit Starlette's 405 handler. Pinned so an
    accidental ``router.api_route(...)`` wildcard refactor surfaces
    here."""
    response = client.request(method, "/v1/about")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Slice 5 deprecation header pairing on /api/about vs /v1/about
# ---------------------------------------------------------------------------


def test_v1_about_carries_no_deprecation_headers(client: TestClient) -> None:
    """[TEST-COVERAGE] The canonical prefix is NOT deprecated; none of
    the three RFC headers should appear on a ``/v1/about`` response.
    Mirrors the parallel pin on /v1/health from the slice-5 suite."""
    response = client.get("/v1/about")
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers
    link = response.headers.get("link")
    assert link is None or "successor-version" not in link


def test_api_about_carries_deprecation_headers(client: TestClient) -> None:
    """[TEST-COVERAGE] ``/api/about`` is the legacy alias and MUST carry
    the slice-5 deprecation trio so clients see the sunset signal."""
    response = client.get("/api/about")
    assert response.status_code == 200
    assert response.headers.get("deprecation") == "true"
    assert response.headers.get("sunset")  # default sunset date present
    link = response.headers.get("link", "")
    assert "/v1/about" in link
    assert 'rel="successor-version"' in link


# ---------------------------------------------------------------------------
# CLI integration (httpx.MockTransport)
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client_factory(handler: Handler) -> Callable[[Any], httpx.Client]:
    """Mirrors the production ``_DaemonClient._client`` shape but threads a
    ``MockTransport`` through the underlying httpx.Client. Cookies are
    seeded at construction (httpx 0.28 deprecated the per-request
    ``cookies=`` argument) and the constructed client is cached on
    ``self._http``."""

    def factory(self: Any) -> httpx.Client:
        cached = getattr(self, "_http", None)
        if cached is not None:
            return cached
        cookies: dict[str, str] = {}
        cookie_value = getattr(self, "_cookie", None)
        if cookie_value:
            cookies[admin._SESSION_COOKIE_NAME] = cookie_value
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=self._opts.base_url,
            timeout=admin._HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            cookies=cookies,
        )
        self._http = client
        return client

    return factory


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Patch ``_DaemonClient._client`` to route through the mock."""
    captured: list[httpx.Request] = []

    def wrap(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(wrap))
    return captured


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _scrub_cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (
        "ENDPOINT_ADMIN_PASSWORD",
        "ENDPOINT_DAEMON_SESSION_COOKIE",
        "ENDPOINT_DAEMON_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_cli_status_includes_about_payload(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] CLI integration with slice 14 part B: the
    ``status`` command now hits ``/api/about`` in addition to health
    and ready. The about payload surfaces under the ``about`` key in
    the JSON output and the ``cli_version`` is still present so an
    operator can spot CLI-vs-daemon skew."""
    about_payload = {
        "version": "1.4.2",
        "git_sha": "9a0c3f1b2d4e",
        "built_at": "2026-04-26T14:30:00Z",
        "python_version": "3.12.4",
        "uptime_seconds": 3742,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/ready":
            return httpx.Response(200, json={"status": "ok", "checks": {"db": "ok"}})
        if request.url.path == "/api/about":
            return httpx.Response(200, json=about_payload)
        return httpx.Response(404, json={"detail": {"code": "not_found", "message": "no"}})

    _install_mock(monkeypatch, handler)
    result = runner.invoke(admin.app, ["status"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["about"] == about_payload
    assert "cli_version" in body
    assert body["health"] == {"status": "ok"}
    assert body["ready"]["status"] == "ok"


def test_cli_status_gracefully_degrades_about_to_null_on_404(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Rolling deploy case: a CLI built from a newer
    commit may hit a daemon that pre-dates slice 14 (no /api/about
    route, returns 404 mapped to EXIT_API_ERROR). The status command
    must catch that single exit code and surface ``about=null`` rather
    than failing the whole command."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/ready":
            return httpx.Response(200, json={"status": "ok", "checks": {"db": "ok"}})
        if request.url.path == "/api/about":
            # Older daemon: no such route.
            return httpx.Response(
                404, json={"detail": {"code": "not_found", "message": "no route"}}
            )
        return httpx.Response(404, json={"detail": {"code": "not_found", "message": "no"}})

    _install_mock(monkeypatch, handler)
    result = runner.invoke(admin.app, ["status"])
    # The about miss does NOT degrade to a non-zero exit — ready is OK.
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["about"] is None
    # The other two probes still produced their normal blobs.
    assert body["health"] == {"status": "ok"}
    assert body["ready"]["status"] == "ok"
