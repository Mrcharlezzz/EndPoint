"""Tests for the headless auth FastAPI surface and refresh-token mint.

Covers Phase 2 contract:
- ``LoginSession.refresh_token`` is populated on the success path.
- ``LoginSession.refresh_token`` stays ``None`` when the silent-OIDC
  bootstrap raises ``InteractionRequiredError`` (and the session is still
  marked ``CAPTURED``, not ``FAILED``).
- ``GET /auth/teams/{id}`` excludes ``tokens``, ``team_ids``, and
  ``refresh_token`` from the public response.
- ``GET /internal/auth/teams/{id}/payload`` requires the
  ``X-Internal-Secret`` header and returns the full payload on match.

All silent-OIDC calls are monkey-patched: no network, no Playwright, no
real sleeps. Suite must stay well under the daemon's 2-second budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.auth.silent_oidc import (
    InteractionRequiredError,
    TokenBundle,
)
from ingestion.headless import api as headless_api
from ingestion.headless import session as headless_session
from ingestion.headless.api import _INTERNAL_SECRET_ENV, app
from ingestion.headless.session import LoginSession, SessionStatus, filter_sso_cookies

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sessions() -> Any:
    """Wipe the in-memory session registry between tests."""
    headless_api._sessions.clear()
    yield
    headless_api._sessions.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake_session() -> LoginSession:
    """A pre-populated CAPTURED session with tokens, cookies, team_ids."""
    s = LoginSession()
    s.status = SessionStatus.CAPTURED
    s.tokens = {"graph_token": "g.tok.aaa", "csa_token": "c.tok.bbb"}
    s.team_ids = ["team-1", "team-2"]
    s.refresh_token = "rt.zzz"
    s.cookies = [
        # SSO cookie — should be exposed in /internal payload
        {
            "name": "ESTSAUTHPERSISTENT",
            "value": "abc",
            "domain": ".login.microsoftonline.com",
            "path": "/",
        },
        # Non-SSO cookie — should be filtered out
        {
            "name": "TSREMEMBERME",
            "value": "xyz",
            "domain": "teams.microsoft.com",
            "path": "/",
        },
    ]
    headless_api._sessions[s.id] = s
    return s


# ------------------------------------------------------------------
# refresh_token mint
# ------------------------------------------------------------------


def _bundle_with_rt(rt: str | None) -> TokenBundle:
    return TokenBundle(
        access_token="acc.123",
        refresh_token=rt,
        id_token=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=3599),
        scope=["openid", "profile"],
    )


def test_mint_refresh_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap returning a refresh_token is propagated unchanged."""
    calls: list[Any] = []

    def fake_silent_refresh(cookies: Any, config: Any) -> TokenBundle:
        calls.append((cookies, config))
        return _bundle_with_rt("rt.success")

    monkeypatch.setattr("ingestion.auth.silent_oidc.silent_token_refresh", fake_silent_refresh)
    sso = [
        {
            "name": "ESTSAUTHPERSISTENT",
            "value": "v",
            "domain": ".login.microsoftonline.com",
            "path": "/",
        }
    ]
    slog = _StubLog()
    rt = headless_session._mint_refresh_token(sso, slog)  # type: ignore[arg-type]
    assert rt == "rt.success"
    assert len(calls) == 1
    assert "minted" in slog.find("REFRESH_TOKEN")


def test_mint_refresh_token_interaction_required_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InteractionRequiredError is logged and swallowed → None."""

    def fake_silent_refresh(cookies: Any, config: Any) -> TokenBundle:
        raise InteractionRequiredError("session expired")

    monkeypatch.setattr("ingestion.auth.silent_oidc.silent_token_refresh", fake_silent_refresh)
    sso = [
        {
            "name": "ESTSAUTHPERSISTENT",
            "value": "v",
            "domain": ".login.microsoftonline.com",
            "path": "/",
        }
    ]
    slog = _StubLog()
    rt = headless_session._mint_refresh_token(sso, slog)  # type: ignore[arg-type]
    assert rt is None
    assert "interaction required" in slog.find("REFRESH_TOKEN")


def test_mint_refresh_token_no_cookies_returns_none() -> None:
    """No SSO cookies short-circuits before any network call."""
    rt = headless_session._mint_refresh_token([], _StubLog())  # type: ignore[arg-type]
    assert rt is None


def test_mint_refresh_token_bundle_without_rt_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle that lacks ``refresh_token`` should be treated as failure."""

    def fake_silent_refresh(cookies: Any, config: Any) -> TokenBundle:
        return _bundle_with_rt(None)

    monkeypatch.setattr("ingestion.auth.silent_oidc.silent_token_refresh", fake_silent_refresh)
    sso = [
        {
            "name": "ESTSAUTHPERSISTENT",
            "value": "v",
            "domain": ".login.microsoftonline.com",
            "path": "/",
        }
    ]
    rt = headless_session._mint_refresh_token(sso, _StubLog())  # type: ignore[arg-type]
    assert rt is None


def test_filter_sso_cookies_keeps_only_allowlisted_domains() -> None:
    cookies: list[Any] = [
        {"name": "a", "value": "1", "domain": ".login.microsoftonline.com"},
        {"name": "b", "value": "2", "domain": "login.microsoftonline.com"},
        {"name": "c", "value": "3", "domain": ".login.live.com"},
        {"name": "d", "value": "4", "domain": "teams.microsoft.com"},
        {"name": "e", "value": "5", "domain": ""},
        "not-a-dict",
    ]
    out = filter_sso_cookies(cookies)
    assert {c["name"] for c in out} == {"a", "b", "c"}


# ------------------------------------------------------------------
# Public GET /auth/teams/{id}
# ------------------------------------------------------------------


def test_get_session_omits_sensitive_fields(client: TestClient, fake_session: LoginSession) -> None:
    r = client.get(f"/auth/teams/{fake_session.id}")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "session_id": fake_session.id,
        "status": "captured",
        "error": None,
    }
    # Defence in depth — these keys must never leak.
    assert "tokens" not in body
    assert "team_ids" not in body
    assert "refresh_token" not in body
    assert "cookies" not in body


def test_get_session_404_when_unknown(client: TestClient) -> None:
    r = client.get("/auth/teams/does-not-exist")
    assert r.status_code == 404


# ------------------------------------------------------------------
# Internal GET /internal/auth/teams/{id}/payload
# ------------------------------------------------------------------


def test_internal_payload_503_when_secret_unset(
    client: TestClient,
    fake_session: LoginSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_INTERNAL_SECRET_ENV, raising=False)
    r = client.get(
        f"/internal/auth/teams/{fake_session.id}/payload",
        headers={"X-Internal-Secret": "anything"},
    )
    assert r.status_code == 503


def test_internal_payload_401_when_header_missing(
    client: TestClient,
    fake_session: LoginSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_INTERNAL_SECRET_ENV, "expected-secret")
    r = client.get(f"/internal/auth/teams/{fake_session.id}/payload")
    assert r.status_code == 401


def test_internal_payload_401_when_header_wrong(
    client: TestClient,
    fake_session: LoginSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_INTERNAL_SECRET_ENV, "expected-secret")
    r = client.get(
        f"/internal/auth/teams/{fake_session.id}/payload",
        headers={"X-Internal-Secret": "wrong"},
    )
    assert r.status_code == 401


def test_internal_payload_404_when_session_missing_after_secret_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secret check runs first; an authed caller still gets 404 for unknown ids."""
    monkeypatch.setenv(_INTERNAL_SECRET_ENV, "expected-secret")
    r = client.get(
        "/internal/auth/teams/no-such-id/payload",
        headers={"X-Internal-Secret": "expected-secret"},
    )
    assert r.status_code == 404


def test_internal_payload_returns_full_capture(
    client: TestClient,
    fake_session: LoginSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_INTERNAL_SECRET_ENV, "expected-secret")
    r = client.get(
        f"/internal/auth/teams/{fake_session.id}/payload",
        headers={"X-Internal-Secret": "expected-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == fake_session.id
    assert body["status"] == "captured"
    assert body["tokens"] == {"graph_token": "g.tok.aaa", "csa_token": "c.tok.bbb"}
    assert body["refresh_token"] == "rt.zzz"
    assert body["team_ids"] == ["team-1", "team-2"]
    # cookies are SSO-filtered: TSREMEMBERME (teams.microsoft.com) is dropped.
    cookie_names = {c["name"] for c in body["cookies"]}
    assert cookie_names == {"ESTSAUTHPERSISTENT"}
    assert body["error"] is None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


class _StubLog:
    """Captures slog.write calls so tests can assert on the audit trail."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def write(self, tag: str, msg: str) -> None:
        self.entries.append((tag, msg))

    def find(self, tag: str) -> str:
        return " | ".join(msg for t, msg in self.entries if t == tag)
