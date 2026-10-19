"""Unit tests for ``ingestion.daemon.auth.moodle_strategy``.

Probe-not-rotate behaviour:

* HTTP 200 with valid site_info → SUCCESS, no payload write.
* HTTP 200 with token-error errorcode (invalidtoken / accessexception /
  invalidlogin / sitemaintenance) → EXPIRED.
* HTTP 200 with non-token errorcode → ERROR.
* HTTP 401/403 → EXPIRED (proxy short-circuit case).
* HTTP 5xx → ERROR.
* Network error (httpx.ConnectError, TimeoutException) → ERROR.
* Empty payload / missing token → NO_TOKENS.
* base_url override on the credential payload wins; falls back to
  config.base_url otherwise.

Tests inject a respx-mocked client_factory so no network is touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from ingestion.config import MoodleConfig
from ingestion.daemon.auth.moodle_strategy import MoodleRefreshStrategy
from ingestion.daemon.auth.strategy import RefreshOutcome


def _moodle_config(base_url: str = "https://moodle.example") -> MoodleConfig:
    """Build a MoodleConfig with a known base_url, ignoring env vars."""
    return MoodleConfig(_env_file=None, base_url=base_url)  # type: ignore[call-arg]


def _make_strategy(
    *,
    config: MoodleConfig | None = None,
    transport: httpx.MockTransport,
    timeout: float = 15.0,
) -> MoodleRefreshStrategy:
    """Inject a MockTransport via a client_factory wrapper."""

    class _Factory:
        def __init__(
            self,
            *,
            timeout: httpx.Timeout | float | None = None,
            follow_redirects: bool = False,
        ) -> None:
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, *exc_info: object) -> None:
            await self._client.aclose()

    return MoodleRefreshStrategy(
        config or _moodle_config(),
        client_factory=_Factory,  # type: ignore[arg-type]
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# is_refresh_supported
# ---------------------------------------------------------------------------


async def test_is_refresh_supported_returns_true() -> None:
    """Probe-not-rotate counts as supported — the bulk loop surfaces
    EXPIRED via the same path it does for Teams."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    s = _make_strategy(transport=transport)
    assert await s.is_refresh_supported() is True


# ---------------------------------------------------------------------------
# Empty payload short-circuits
# ---------------------------------------------------------------------------


async def test_empty_payload_returns_no_tokens() -> None:
    """current_payload={} → NO_TOKENS, no network."""
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "empty credential payload"
    assert requests == []


async def test_missing_token_returns_no_tokens() -> None:
    """Payload present but token absent → NO_TOKENS."""
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"other": "field"})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no webservice token"
    assert requests == []


async def test_token_must_be_non_empty_string() -> None:
    """An empty / non-string token rejects without a network call."""
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": ""})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert requests == []

    result = await s.refresh("u@x", current_payload={"token": 42})
    assert result.outcome is RefreshOutcome.NO_TOKENS


# ---------------------------------------------------------------------------
# 200-with-valid-site_info → SUCCESS
# ---------------------------------------------------------------------------


async def test_success_path_returns_success_no_payload() -> None:
    """A healthy site_info response → SUCCESS, no new_payload."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sitename": "Moodle", "userid": 42, "username": "alice"},
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "valid-token"})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.new_payload is None
    assert result.error_message is None
    assert result.latency_ms >= 0.0


async def test_success_targets_correct_endpoint_and_form() -> None:
    """Verify the wstoken / wsfunction / format params hit the right
    endpoint."""
    seen: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(
        config=_moodle_config(base_url="https://moodle.example.com"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"token": "tok-abc"})
    assert "moodle.example.com" in seen["url"]
    assert "/webservice/rest/server.php" in seen["url"]
    assert "wstoken=tok-abc" in seen["body"]
    assert "core_webservice_get_site_info" in seen["body"]
    assert "moodlewsrestformat=json" in seen["body"]


# ---------------------------------------------------------------------------
# 200-with-errorcode → EXPIRED for token errors, ERROR otherwise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "errorcode",
    ["invalidtoken", "accessexception", "invalidlogin", "sitemaintenance"],
)
async def test_token_errorcode_returns_expired(errorcode: str) -> None:
    """Each member of the token-error set flips to EXPIRED."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errorcode": errorcode,
                "exception": "moodle_exception",
                "message": "token revoked",
            },
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert errorcode in result.error_message


async def test_non_token_errorcode_returns_error() -> None:
    """Any other Moodle errorcode (platform problem, not an auth issue)
    surfaces as ERROR — the operator pivots from the audit log."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorcode": "dml_write_exception", "exception": "foo"})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "dml_write_exception" in result.error_message


async def test_empty_string_errorcode_treated_as_no_error() -> None:
    """`errorcode` field with empty string is not a token error —
    falls through to userid check."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorcode": "", "userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.SUCCESS


async def test_non_string_errorcode_ignored() -> None:
    """`errorcode: 42` (non-string) is ignored — falls through to
    userid check."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorcode": 42, "userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.SUCCESS


# ---------------------------------------------------------------------------
# HTTP error statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_http_401_403_returns_expired(status: int) -> None:
    """Proxies that short-circuit before the JSON envelope return raw
    401/403 — strategy treats both as EXPIRED."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="Unauthorized")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert str(status) in result.error_message


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_http_5xx_returns_error(status: int) -> None:
    """Server errors → ERROR, not EXPIRED."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server error")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert str(status) in result.error_message


async def test_http_400_returns_error() -> None:
    """A non-201/non-200 / non-401 / non-403 status surfaces as ERROR."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


async def test_connect_error_returns_error() -> None:
    """httpx.ConnectError → ERROR with `network:` prefix in message."""

    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "network" in result.error_message
    assert "ConnectError" in result.error_message


async def test_timeout_exception_returns_error() -> None:
    """httpx.TimeoutException → ERROR."""

    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "network" in result.error_message


async def test_other_httpx_error_returns_error() -> None:
    """A different httpx.HTTPError subclass takes the second except
    branch."""

    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ProtocolError("protocol violation")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "httpx" in result.error_message


# ---------------------------------------------------------------------------
# JSON / shape validation
# ---------------------------------------------------------------------------


async def test_invalid_json_response_returns_error() -> None:
    """Non-JSON body → ERROR with parse error in message."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login page</html>")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "invalid json" in result.error_message


async def test_response_without_userid_returns_error() -> None:
    """Healthy site_info responses always include userid — defensive
    check rejects misrouted endpoints."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"some_other_field": "x"})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message == "site_info missing userid"


async def test_non_dict_response_body_returns_error() -> None:
    """A 200 with a JSON list (not a dict) → ERROR."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected", "list"])

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR


# ---------------------------------------------------------------------------
# base_url override
# ---------------------------------------------------------------------------


async def test_base_url_from_payload_overrides_config() -> None:
    """Per-institution credential payloads can carry a base_url
    override — wins over the config default."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(
        config=_moodle_config(base_url="https://default.example"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh(
        "u@x",
        current_payload={
            "token": "t",
            "base_url": "https://override.example.com",
        },
    )
    assert "override.example.com" in seen["url"]
    assert "default.example" not in seen["url"]


async def test_base_url_payload_non_string_falls_back_to_config() -> None:
    """Non-string base_url in payload → config wins (defensive)."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(
        config=_moodle_config(base_url="https://default.example"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"token": "t", "base_url": 42})
    assert "default.example" in seen["url"]


async def test_trailing_slash_base_url_normalised() -> None:
    """A trailing slash on the base_url doesn't double up against the
    `/webservice/rest/server.php` suffix."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(
        config=_moodle_config(base_url="https://moodle.example/"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"token": "t"})
    assert "moodle.example/webservice/rest/server.php" in seen["url"]


# ---------------------------------------------------------------------------
# Latency capture
# ---------------------------------------------------------------------------


async def test_latency_ms_captured_on_success_path() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "t"})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.latency_ms >= 0.0


async def test_latency_ms_captured_on_error_path() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "t"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.latency_ms >= 0.0


async def test_latency_ms_captured_on_short_circuit() -> None:
    """The empty-payload short-circuit still records latency."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    s = _make_strategy(transport=transport)
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Concurrent calls — strategy is reentrant
# ---------------------------------------------------------------------------


async def test_strategy_is_reentrant_across_concurrent_calls() -> None:
    """Same strategy instance handles parallel refreshes safely."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"userid": 1})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    results = await asyncio.gather(
        *[s.refresh(f"u{i}@x", current_payload={"token": f"t{i}"}) for i in range(5)]
    )
    assert all(r.outcome is RefreshOutcome.SUCCESS for r in results)
