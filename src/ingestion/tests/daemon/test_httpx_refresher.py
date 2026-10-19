"""HttpxTokenRefresher: silent OIDC chain control-flow.

No real HTTPS. Each test patches the two underlying primitives
(``silent_token_refresh`` and ``refresh_with_token``) on the
``ingestion.daemon.auth.refresh`` module so we can assert which path
the refresher took for a given input combination.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from ingestion.auth.silent_oidc import (
    InteractionRequiredError,
    SilentOIDCConfig,
    TokenBundle,
    TokenExchangeError,
)
from ingestion.daemon.auth.refresh import (
    TEAMS_WEB_CLIENT_ID,
    TEAMS_WEB_REDIRECT_URI,
    HttpxTokenRefresher,
)


def _scoped_jwt(*, exp_in: int = 3600) -> str:
    payload = {"appid": TEAMS_WEB_CLIENT_ID, "exp": int(time.time()) + exp_in}
    return jwt.encode(payload, "test-secret-key-32-bytes-long-aaa", algorithm="HS256")


def _bundle(access: str = "access-x", *, refresh: str | None = "refresh-y") -> TokenBundle:
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        id_token=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=3599),
        scope=[],
        raw={},
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Patch silent_token_refresh + refresh_with_token, capture call args.

    Each ``*_responses`` list is a FIFO of either ``TokenBundle`` (return
    that value) or ``Exception`` instances (raise that on the call).
    Calls beyond the queue's length default-return ``_bundle()``.
    Each ``*_calls`` list captures the full args tuple per invocation.
    """
    state: dict[str, list[Any]] = {
        "silent_calls": [],
        "silent_responses": [],
        "rt_calls": [],
        "rt_responses": [],
    }

    def fake_silent(
        cookies: list[dict[str, Any]], config: SilentOIDCConfig, *, timeout: float = 15.0
    ) -> TokenBundle:
        state["silent_calls"].append((cookies, config, timeout))
        if not state["silent_responses"]:
            return _bundle()
        nxt = state["silent_responses"].pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def fake_rt(
        refresh_token: str, config: SilentOIDCConfig, *, timeout: float = 15.0
    ) -> TokenBundle:
        state["rt_calls"].append((refresh_token, config, timeout))
        if not state["rt_responses"]:
            return _bundle()
        nxt = state["rt_responses"].pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr("ingestion.daemon.auth.refresh.silent_token_refresh", fake_silent)
    monkeypatch.setattr("ingestion.daemon.auth.refresh.refresh_with_token", fake_rt)
    return state


def _cookies() -> list[dict[str, Any]]:
    return [{"domain": ".login.microsoftonline.com", "name": "ESTSAUTHPERSISTENT", "value": "x"}]


# ---------------- steady state ----------------


async def test_steady_state_uses_refresh_token_grant_only(
    patched: dict[str, list[Any]],
) -> None:
    """When refresh_token is given and the grant works, no cookie bootstrap."""
    graph = _scoped_jwt()
    csa_token = "csa-x"
    patched["rt_responses"].extend(
        [
            _bundle(graph, refresh="rolled-1"),  # graph
            _bundle(csa_token, refresh="rolled-2"),  # csa
        ]
    )
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="https://teams.microsoft.com/",
        timeout_seconds=15,
        refresh_token="prior-refresh",
    )

    assert result.graph_token == graph
    assert result.csa_token == csa_token
    # Refresh token should be the rolled value from the most recent call
    # in the chain (CSA call rolls last; both Graph and CSA exchanges roll).
    assert result.refresh_token == "rolled-1"
    assert result.expired is False
    # No cookie bootstrap
    assert patched["silent_calls"] == []
    # Two RT calls: graph + csa
    assert len(patched["rt_calls"]) == 2
    graph_call_token, graph_cfg, _ = patched["rt_calls"][0]
    assert graph_call_token == "prior-refresh"
    assert "https://graph.microsoft.com/.default" in graph_cfg.scope
    csa_call_token, csa_cfg, _ = patched["rt_calls"][1]
    # CSA exchange uses the rolled refresh from the Graph call
    assert csa_call_token == "rolled-1"
    assert "https://chatsvcagg.teams.microsoft.com/.default" in csa_cfg.scope


# ---------------- bootstrap ----------------


async def test_bootstrap_when_no_refresh_token(patched: dict[str, list[Any]]) -> None:
    """No prior refresh_token: cookie bootstrap then graph chain."""
    skype = _bundle("skype-tok", refresh="skype-rt")
    graph = _bundle(_scoped_jwt(), refresh="graph-rt-rolled")
    csa = _bundle("csa-tok", refresh="csa-rt-rolled")
    patched["silent_responses"].append(skype)
    patched["rt_responses"].extend([graph, csa])
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="https://teams.microsoft.com/",
        timeout_seconds=15,
    )

    assert result.graph_token == graph.access_token
    assert result.csa_token == csa.access_token
    assert result.refresh_token == "graph-rt-rolled"
    assert result.expired is False
    # One silent OIDC bootstrap with Skype scope
    assert len(patched["silent_calls"]) == 1
    _, skype_cfg, _ = patched["silent_calls"][0]
    assert "https://api.spaces.skype.com/.default" in skype_cfg.scope
    # Two RT calls: graph chain + csa chain (both off the skype refresh)
    assert len(patched["rt_calls"]) == 2
    assert patched["rt_calls"][0][0] == "skype-rt"  # first chain uses skype's RT


async def test_bootstrap_when_refresh_token_grant_fails(
    patched: dict[str, list[Any]],
) -> None:
    """Refresh-token grant rejected: fall through to cookie bootstrap."""
    skype = _bundle("skype-tok", refresh="skype-rt")
    graph = _bundle(_scoped_jwt(), refresh="rolled")
    csa = _bundle("csa-tok", refresh="rolled")
    patched["silent_responses"].append(skype)
    # Sequence: 1st RT (steady state) raises, 2nd is graph chain, 3rd is csa.
    patched["rt_responses"].extend([InteractionRequiredError("revoked"), graph, csa])
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
        refresh_token="stale",
    )

    assert result.graph_token == graph.access_token
    assert result.expired is False
    # First RT failed, then cookie bootstrap, then graph chain + csa chain
    assert len(patched["silent_calls"]) == 1
    # 3 RT total: failed initial steady-state + graph chain + csa chain
    assert len(patched["rt_calls"]) == 3


# ---------------- expiry ----------------


async def test_expired_when_cookie_session_lapsed(
    patched: dict[str, list[Any]],
) -> None:
    """Cookies dead: silent_token_refresh raises InteractionRequiredError."""
    patched["silent_responses"].append(InteractionRequiredError("session lapsed"))
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
    )

    assert result.expired is True
    assert result.graph_token is None
    assert result.refresh_token is None
    assert len(patched["silent_calls"]) == 1
    assert patched["rt_calls"] == []


async def test_bootstrap_protocol_error_does_not_mark_expired(
    patched: dict[str, list[Any]],
) -> None:
    """Network/protocol error during bootstrap returns NO_TOKENS, not EXPIRED."""
    patched["silent_responses"].append(TokenExchangeError("connection reset"))
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
    )

    assert result.expired is False
    assert result.graph_token is None
    assert result.refresh_token is None


# ---------------- CSA control ----------------


async def test_include_csa_false_skips_csa_chain(
    patched: dict[str, list[Any]],
) -> None:
    graph = _bundle(_scoped_jwt(), refresh="rolled-1")
    patched["rt_responses"].append(graph)
    refresher = HttpxTokenRefresher(include_csa=False)

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
        refresh_token="prior",
    )

    assert result.graph_token == graph.access_token
    assert result.csa_token is None
    # Only one RT call (Graph), no CSA chain
    assert len(patched["rt_calls"]) == 1


async def test_csa_failure_does_not_invalidate_graph(
    patched: dict[str, list[Any]],
) -> None:
    """A CSA exchange failure leaves the Graph capture intact."""
    graph = _bundle(_scoped_jwt(), refresh="rolled-1")
    patched["rt_responses"].append(graph)
    patched["rt_responses"].append(TokenExchangeError("csa endpoint sad"))
    refresher = HttpxTokenRefresher()

    result = await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
        refresh_token="prior",
    )

    assert result.graph_token == graph.access_token
    assert result.csa_token is None
    assert result.refresh_token == "rolled-1"
    assert result.expired is False


# ---------------- config shape ----------------


async def test_user_email_propagates_as_login_hint(
    patched: dict[str, list[Any]],
) -> None:
    refresher = HttpxTokenRefresher()
    await refresher.capture_tokens(
        cookies=_cookies(),
        refresh_url="x",
        timeout_seconds=15,
        refresh_token="prior",
        user_email="alice@example.com",
    )
    _, graph_cfg, _ = patched["rt_calls"][0]
    assert graph_cfg.login_hint == "alice@example.com"
    assert graph_cfg.client_id == TEAMS_WEB_CLIENT_ID
    assert graph_cfg.redirect_uri == TEAMS_WEB_REDIRECT_URI
