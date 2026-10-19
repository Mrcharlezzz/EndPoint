"""API middlewares: request-size cap + per-IP login rate limit."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.middlewares import _TokenBucketRegistry
from ingestion.daemon.config import ApiConfig, DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store


def _test_with_xrw(client: TestClient) -> TestClient:
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    return client


def _build_app(tmp_path: Path, fernet_key: str, *, api: ApiConfig | None = None):
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path, api=api or ApiConfig())
    return create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)


def _client(app, *, client_ip: str = "testclient") -> TestClient:
    return _test_with_xrw(TestClient(app, client=(client_ip, 50000)))


# ---------------------------------------------------------------------------
# request-size middleware
# ---------------------------------------------------------------------------


def test_oversize_login_body_returns_413(tmp_path: Path, fernet_key: str) -> None:
    app = _build_app(tmp_path, fernet_key)
    client = _client(app)
    # 256 KB login cap + 1 byte; actual POST body is the giant JSON below.
    huge_password = "x" * (300 * 1024)
    response = client.post("/api/auth/login", json={"password": huge_password})
    assert response.status_code == 413
    assert response.json()["detail"] == "request body too large"


def test_oversize_general_body_returns_413(tmp_path: Path, fernet_key: str) -> None:
    # 2 KiB cap so the test payload stays small.
    api = ApiConfig(max_request_bytes=2048)
    app = _build_app(tmp_path, fernet_key, api=api)
    client = _client(app)
    # Login first so the request reaches the overlay endpoint.
    login = client.post("/api/auth/login", json={"password": "hunter2-strong"})
    assert login.status_code == 200
    huge_value = "y" * 5000
    response = client.put(
        "/api/config/overlay/scheduler.drain_mode",
        json={"value": huge_value},
    )
    assert response.status_code == 413


def test_under_limit_body_passes_through(tmp_path: Path, fernet_key: str) -> None:
    app = _build_app(tmp_path, fernet_key)
    client = _client(app)
    response = client.post("/api/auth/login", json={"password": "hunter2-strong"})
    assert response.status_code == 200


def test_size_limit_uses_content_length_header(tmp_path: Path, fernet_key: str) -> None:
    # A too-large declared content-length on a non-login endpoint trips the
    # general cap, not the tighter login cap.
    api = ApiConfig(max_request_bytes=1024)
    app = _build_app(tmp_path, fernet_key, api=api)
    client = _client(app)
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    # 2 KiB body; TestClient sets the matching content-length header.
    response = client.put(
        "/api/config/overlay/scheduler.drain_mode",
        content=b"A" * 2048,
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# login rate-limit middleware
# ---------------------------------------------------------------------------


def test_login_rate_limit_trips_after_capacity(tmp_path: Path, fernet_key: str) -> None:
    api = ApiConfig(login_rate_limit_per_minute=3)
    app = _build_app(tmp_path, fernet_key, api=api)
    client = _client(app)
    # First three bad-password attempts should reach the handler (401),
    # the fourth should be short-circuited by the middleware (429).
    for _ in range(3):
        resp = client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401
    limited = client.post("/api/auth/login", json={"password": "wrong"})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "too many login attempts"
    retry_after = limited.headers.get("retry-after")
    assert retry_after is not None
    assert int(retry_after) >= 1


def test_login_rate_limit_is_per_ip(tmp_path: Path, fernet_key: str) -> None:
    api = ApiConfig(login_rate_limit_per_minute=2)
    app = _build_app(tmp_path, fernet_key, api=api)

    alice = _client(app, client_ip="10.0.0.1")
    bob = _client(app, client_ip="10.0.0.2")

    for _ in range(2):
        assert alice.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    # Alice is out of tokens.
    assert alice.post("/api/auth/login", json={"password": "wrong"}).status_code == 429
    # Bob has his own bucket and is untouched.
    assert bob.post("/api/auth/login", json={"password": "wrong"}).status_code == 401


def test_login_rate_limit_audits_on_trip(tmp_path: Path, fernet_key: str) -> None:
    api = ApiConfig(login_rate_limit_per_minute=1)
    app = _build_app(tmp_path, fernet_key, api=api)
    client = _client(app, client_ip="10.0.0.9")

    # Consume the lone token (401).
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    # Trip the limit.
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 429

    # Log in legitimately so we can read the audit feed.
    refresh = _client(app, client_ip="127.0.0.1")
    refresh.post("/api/auth/login", json={"password": "hunter2-strong"})
    audit = refresh.get("/api/audit", params={"action": "auth.login_rate_limited"}).json()
    assert any(entry["actor"] == "10.0.0.9" for entry in audit), audit


def test_login_rate_limit_only_affects_login_path(tmp_path: Path, fernet_key: str) -> None:
    api = ApiConfig(login_rate_limit_per_minute=1)
    app = _build_app(tmp_path, fernet_key, api=api)
    client = _client(app)
    # Burn the only login token then trip the limit to prove the bucket is
    # empty for this IP.
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 429
    # Logout (also under /api/auth) must NOT be limited by the login-only
    # middleware; the handler happily returns 200 without a cookie.
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# token bucket unit coverage
# ---------------------------------------------------------------------------


def test_token_bucket_refills_over_window() -> None:
    reg = _TokenBucketRegistry(capacity=2, window_seconds=60.0)
    # Burn both tokens at t=0.
    assert reg.consume("k", now=0.0) == (True, 0.0)
    assert reg.consume("k", now=0.0) == (True, 0.0)
    allowed, retry = reg.consume("k", now=0.0)
    assert allowed is False
    assert retry > 0.0
    # Half the window later, we've earned one token back.
    assert reg.consume("k", now=30.0) == (True, 0.0)


def test_token_bucket_keys_isolated() -> None:
    reg = _TokenBucketRegistry(capacity=1, window_seconds=60.0)
    assert reg.consume("a", now=0.0) == (True, 0.0)
    # "a" is empty, but "b" is untouched.
    assert reg.consume("a", now=0.0)[0] is False
    assert reg.consume("b", now=0.0) == (True, 0.0)
