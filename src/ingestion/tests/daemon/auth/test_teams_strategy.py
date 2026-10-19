"""Unit tests for ``ingestion.daemon.auth.teams_strategy``.

Covers the TeamsRefreshStrategy adapter wrap around HttpxTokenRefresher:

* SUCCESS path with a fresh capture → RefreshResult with merged payload.
* NO_TOKENS short-circuits: empty payload, no cookies, no SSO cookies,
  driver returned no usable tokens.
* EXPIRED when the driver flagged expired.
* Driver TimeoutError surfaces as NO_TOKENS.
* Latency captured.
* The strategy reports refresh-supported.

Tests inject a fake refresher so no network is touched. The merge logic
is tested in test_bulk_refresh.py — here we only pin the strategy's
contract surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
from ingestion.daemon.auth.refresh import REQUIRED_GRAPH_SCOPE, CapturedTokens
from ingestion.daemon.auth.strategy import RefreshOutcome
from ingestion.daemon.auth.teams_strategy import TeamsRefreshStrategy


def _scoped_jwt(scope: str = REQUIRED_GRAPH_SCOPE, *, exp_in: int = 3600) -> str:
    """Mint an unsigned JWT carrying ``scope`` with an exp claim.

    Mirrors the helper in tests/daemon/test_bulk_refresh.py — the
    strategy's freshness check parses the JWT exp/scopes without
    verifying the signature.
    """
    import jwt

    payload = {"scp": scope, "exp": int(time.time()) + exp_in}
    return jwt.encode(payload, "test-secret", algorithm="HS256")


@dataclass
class _FakeRefresher:
    """Test double that returns a queued CapturedTokens (or raises)."""

    outputs: list[CapturedTokens | Exception]
    calls: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def capture_tokens(
        self,
        *,
        cookies: list[dict[str, Any]],
        refresh_url: str,
        timeout_seconds: int,
        refresh_token: str | None = None,
        user_email: str | None = None,
    ) -> CapturedTokens:
        assert self.calls is not None
        self.calls.append(
            {
                "cookies": cookies,
                "refresh_url": refresh_url,
                "timeout_seconds": timeout_seconds,
                "refresh_token": refresh_token,
                "user_email": user_email,
            }
        )
        if not self.outputs:
            return CapturedTokens()
        nxt = self.outputs.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _sso_cookies() -> list[dict[str, Any]]:
    return [
        {"domain": "login.microsoftonline.com", "name": "ESTSAUTH", "value": "x"},
        {"domain": "not-sso.example.com", "name": "junk", "value": "y"},
    ]


# ---------------------------------------------------------------------------
# Construction + supports flag
# ---------------------------------------------------------------------------


def test_strategy_constructs_with_default_refresher() -> None:
    """No refresher → default ``HttpxTokenRefresher`` is used."""
    s = TeamsRefreshStrategy()
    # The internal field exists; we don't pin its type here.
    assert s is not None


async def test_is_refresh_supported_returns_true() -> None:
    """Teams supports refresh out-of-the-box (rotate-on-call)."""
    s = TeamsRefreshStrategy(refresher=_FakeRefresher(outputs=[]))
    assert await s.is_refresh_supported() is True


# ---------------------------------------------------------------------------
# NO_TOKENS short-circuits
# ---------------------------------------------------------------------------


async def test_empty_payload_returns_no_tokens_without_network() -> None:
    """current_payload={} → NO_TOKENS, no driver call, latency > 0."""
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.new_payload is None
    assert result.error_message == "empty credential payload"
    assert refresher.calls == []
    assert result.latency_ms >= 0.0


async def test_payload_without_cookies_returns_no_tokens() -> None:
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"refresh_token": "x"})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no session cookies"
    assert refresher.calls == []


async def test_cookies_not_a_list_returns_no_tokens() -> None:
    """Defensive: `cookies` field present but malformed (not a list)."""
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": "not-a-list"})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no session cookies"


async def test_no_sso_cookies_returns_no_tokens() -> None:
    """Cookies present but none on the SSO domain → NO_TOKENS."""
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh(
        "alice@x",
        current_payload={"cookies": [{"domain": "junk.com", "name": "x"}]},
    )
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no sso cookies"


async def test_non_dict_cookie_entries_filtered_out() -> None:
    """A cookies list with non-dict entries (defensive) is ignored."""
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": ["string-not-dict", 42]})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no sso cookies"


# ---------------------------------------------------------------------------
# Driver outcomes
# ---------------------------------------------------------------------------


async def test_driver_expired_signals_returns_expired_outcome() -> None:
    """driver returns ``expired=True`` → strategy returns EXPIRED."""
    refresher = _FakeRefresher(
        outputs=[CapturedTokens(expired=True, cookies=tuple(_sso_cookies()))]
    )
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": _sso_cookies()})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.new_payload is None
    assert result.error_message == "cookie session lapsed"


async def test_driver_returns_no_tokens_when_capture_empty() -> None:
    """Driver returned a CapturedTokens with no tokens → NO_TOKENS."""
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": _sso_cookies()})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no usable tokens captured"


async def test_driver_returns_unusable_capture_returns_no_tokens() -> None:
    """capture has refresh_token, but matches the existing one and no
    graph_token / csa_token. capture_is_fresh returns False."""
    existing_refresh = "rt-old"
    refresher = _FakeRefresher(outputs=[CapturedTokens(refresh_token=existing_refresh)])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh(
        "alice@x",
        current_payload={
            "cookies": _sso_cookies(),
            "refresh_token": existing_refresh,
        },
    )
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no usable tokens captured"


async def test_driver_success_returns_merged_payload() -> None:
    """Happy path: driver returned fresh tokens, strategy returns
    SUCCESS with merged payload."""
    fresh_graph = _scoped_jwt()
    refresher = _FakeRefresher(
        outputs=[
            CapturedTokens(
                graph_token=fresh_graph,
                refresh_token="rt-new",
                cookies=tuple(_sso_cookies()),
            )
        ]
    )
    s = TeamsRefreshStrategy(refresher=refresher)
    existing = {
        "cookies": _sso_cookies(),
        "team_ids": ["t1", "t2"],
        "refresh_token": "rt-old",
    }
    result = await s.refresh("alice@x", current_payload=existing)
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.new_payload is not None
    # Merge preserved team_ids and rolled the refresh_token forward.
    assert result.new_payload["graph_token"] == fresh_graph
    assert result.new_payload["refresh_token"] == "rt-new"
    assert result.new_payload["team_ids"] == ["t1", "t2"]
    assert result.error_message is None
    assert result.latency_ms >= 0.0


async def test_driver_called_with_sso_cookies_and_email() -> None:
    """The strategy filters cookies to SSO-only and forwards the
    email + refresh_token to the driver."""
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher, timeout_seconds=20)
    await s.refresh(
        "alice@x",
        current_payload={
            "cookies": _sso_cookies(),
            "refresh_token": "rt-stored",
            "refresh_url": "https://teams.microsoft.com/?some=qs",
        },
    )
    assert refresher.calls is not None
    assert len(refresher.calls) == 1
    call = refresher.calls[0]
    # Only the SSO cookie passes through the filter.
    assert all(c["domain"].endswith("login.microsoftonline.com") for c in call["cookies"])
    assert call["refresh_url"] == "https://teams.microsoft.com/?some=qs"
    assert call["refresh_token"] == "rt-stored"
    assert call["user_email"] == "alice@x"
    assert call["timeout_seconds"] == 20


async def test_payload_refresh_token_must_be_non_empty_string() -> None:
    """An empty / non-string refresh_token in the payload is treated as
    missing — driver gets None."""
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    await s.refresh(
        "alice@x",
        current_payload={
            "cookies": _sso_cookies(),
            "refresh_token": "",  # empty string
        },
    )
    assert refresher.calls is not None
    assert refresher.calls[0]["refresh_token"] is None


async def test_payload_refresh_token_non_string_is_dropped() -> None:
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    await s.refresh(
        "alice@x",
        current_payload={
            "cookies": _sso_cookies(),
            "refresh_token": 42,  # non-string
        },
    )
    assert refresher.calls is not None
    assert refresher.calls[0]["refresh_token"] is None


async def test_refresh_url_falls_back_to_default_when_not_string() -> None:
    """Non-string refresh_url in payload → falls back to the Teams
    home URL."""
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    await s.refresh(
        "alice@x",
        current_payload={
            "cookies": _sso_cookies(),
            "refresh_url": 42,  # non-string
        },
    )
    assert refresher.calls is not None
    assert refresher.calls[0]["refresh_url"] == "https://teams.microsoft.com"


async def test_refresh_url_default_when_missing() -> None:
    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    await s.refresh(
        "alice@x",
        current_payload={"cookies": _sso_cookies()},
    )
    assert refresher.calls is not None
    assert refresher.calls[0]["refresh_url"] == "https://teams.microsoft.com"


# ---------------------------------------------------------------------------
# Driver timeouts
# ---------------------------------------------------------------------------


async def test_driver_timeout_returns_no_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver hang past timeout_seconds + 10 surfaces as NO_TOKENS
    rather than propagating the TimeoutError. We monkey-patch
    asyncio.wait_for in the strategy module to fire immediately so the
    test doesn't actually wait 10s."""

    async def _instant_timeout(coro: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        # Close the coroutine so we don't leak a "never awaited" warning.
        coro.close()
        raise TimeoutError

    from ingestion.daemon.auth import teams_strategy as ts_mod

    monkeypatch.setattr(ts_mod.asyncio, "wait_for", _instant_timeout)

    refresher = _FakeRefresher(outputs=[CapturedTokens()])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": _sso_cookies()})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "driver timeout"


# ---------------------------------------------------------------------------
# Latency capture
# ---------------------------------------------------------------------------


async def test_latency_ms_is_captured() -> None:
    """Every result records perf-counter wall-clock latency."""
    refresher = _FakeRefresher(outputs=[])
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={})
    assert isinstance(result.latency_ms, float)
    assert result.latency_ms >= 0.0


async def test_latency_ms_present_on_success_path() -> None:
    """Latency is recorded on the SUCCESS branch too."""
    refresher = _FakeRefresher(
        outputs=[
            CapturedTokens(
                graph_token=_scoped_jwt(),
                cookies=tuple(_sso_cookies()),
            )
        ]
    )
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": _sso_cookies()})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Result shape — error_message / new_payload contract
# ---------------------------------------------------------------------------


async def test_success_result_has_no_error_message() -> None:
    """SUCCESS path: error_message is None, payload is set."""
    refresher = _FakeRefresher(
        outputs=[
            CapturedTokens(
                graph_token=_scoped_jwt(),
                cookies=tuple(_sso_cookies()),
            )
        ]
    )
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload={"cookies": _sso_cookies()})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.error_message is None
    assert result.new_payload is not None


@pytest.mark.parametrize(
    "outcome",
    [
        RefreshOutcome.NO_TOKENS,
        RefreshOutcome.EXPIRED,
    ],
)
async def test_non_success_results_have_no_payload(outcome: RefreshOutcome) -> None:
    """Probe / no-tokens / expired never carry new_payload — only
    SUCCESS does."""
    if outcome is RefreshOutcome.EXPIRED:
        outputs: list[CapturedTokens | Exception] = [CapturedTokens(expired=True)]
        payload = {"cookies": _sso_cookies()}
    else:
        outputs = []
        payload = {}
    refresher = _FakeRefresher(outputs=outputs)
    s = TeamsRefreshStrategy(refresher=refresher)
    result = await s.refresh("alice@x", current_payload=payload)
    assert result.outcome is outcome
    assert result.new_payload is None
