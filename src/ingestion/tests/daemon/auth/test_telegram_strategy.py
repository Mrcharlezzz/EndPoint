"""Unit tests for ``ingestion.daemon.auth.telegram_strategy``.

Probe-not-rotate behaviour against Telegram's ``getMe``:

* HTTP 200 with ``{"ok": true, "result": {...}}`` → SUCCESS, no payload write.
* HTTP 200 with ``{"ok": false, "error_code": 401|403, ...}`` → EXPIRED.
* HTTP 200 with ``{"ok": false, "error_code": 500, ...}`` → ERROR.
* HTTP 401 / 403 → EXPIRED (proxy short-circuit).
* HTTP 5xx, network error, malformed JSON → ERROR.
* Empty payload / missing bot_token → NO_TOKENS.
* api_base_url override on credential payload wins over config default.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from ingestion.config import TelegramConfig
from ingestion.daemon.auth.strategy import RefreshOutcome
from ingestion.daemon.auth.telegram_strategy import TelegramRefreshStrategy


def _telegram_config(
    api_base_url: str = "https://api.telegram.org",
) -> TelegramConfig:
    return TelegramConfig(_env_file=None, api_base_url=api_base_url)  # type: ignore[call-arg]


def _make_strategy(
    *,
    config: TelegramConfig | None = None,
    transport: httpx.MockTransport,
    timeout: float = 15.0,
) -> TelegramRefreshStrategy:
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

    return TelegramRefreshStrategy(
        config or _telegram_config(),
        client_factory=_Factory,  # type: ignore[arg-type]
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# is_refresh_supported
# ---------------------------------------------------------------------------


async def test_is_refresh_supported_returns_true() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True, "result": {}})
    )
    s = _make_strategy(transport=transport)
    assert await s.is_refresh_supported() is True


# ---------------------------------------------------------------------------
# Empty payload short-circuits
# ---------------------------------------------------------------------------


async def test_empty_payload_returns_no_tokens() -> None:
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"ok": True, "result": {}})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "empty credential payload"
    assert requests == []


async def test_missing_bot_token_returns_no_tokens() -> None:
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"other": "field"})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.error_message == "no bot token"
    assert requests == []


async def test_bot_token_must_be_non_empty_string() -> None:
    requests: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": ""})
    assert result.outcome is RefreshOutcome.NO_TOKENS

    result = await s.refresh("u@x", current_payload={"bot_token": 42})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert requests == []


# ---------------------------------------------------------------------------
# 200 with ok=true → SUCCESS
# ---------------------------------------------------------------------------


async def test_success_path_returns_success_no_payload() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "BotName"}},
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "1234:abcd"})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.new_payload is None
    assert result.error_message is None
    assert result.latency_ms >= 0.0


async def test_success_targets_correct_endpoint() -> None:
    """Verify the bot{token}/getMe URL is hit."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(
        config=_telegram_config(api_base_url="https://api.telegram.org"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"bot_token": "9876:xyz"})
    assert "api.telegram.org/bot9876:xyz/getMe" in seen["url"]


# ---------------------------------------------------------------------------
# 200 with ok=false envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_code", [401, 403])
async def test_envelope_auth_error_code_returns_expired(error_code: int) -> None:
    """ok=false + error_code in {401, 403} → EXPIRED."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": error_code,
                "description": "Unauthorized",
            },
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert str(error_code) in result.error_message


async def test_envelope_other_error_code_returns_error() -> None:
    """ok=false with non-auth error_code (e.g. 500) → ERROR."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 500, "description": "internal"},
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "ok=false" in result.error_message
    assert "internal" in result.error_message


async def test_envelope_ok_false_without_description() -> None:
    """ok=false without a description → ERROR with bare message."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message == "ok=false"


async def test_envelope_ok_false_non_int_error_code_returns_error() -> None:
    """error_code field present but non-int (defensive) → ERROR not
    EXPIRED."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": "not-an-int",
                "description": "weird",
            },
        )

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR


async def test_envelope_description_truncated_at_100_chars_in_error() -> None:
    """Long descriptions are truncated to keep error_message tidy."""

    long_desc = "x" * 500

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 500, "description": long_desc})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    # The strategy slices description[:100]; the whole error_message
    # therefore stays short.
    assert len(result.error_message) <= 200


async def test_envelope_non_string_description_does_not_crash() -> None:
    """A non-string description (defensive) is treated as empty."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 401, "description": 42})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED


async def test_non_dict_response_body_returns_error() -> None:
    """Top-level JSON list / scalar → ERROR."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message == "non-dict response body"


# ---------------------------------------------------------------------------
# HTTP error statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_http_401_403_returns_expired(status: int) -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="Unauthorized")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert str(status) in result.error_message


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_http_5xx_returns_error(status: int) -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server error")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert str(status) in result.error_message


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


async def test_connect_error_returns_error() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "network" in result.error_message
    assert "ConnectError" in result.error_message


async def test_timeout_exception_returns_error() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "network" in result.error_message


async def test_other_httpx_error_returns_error() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ProtocolError("violation")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "httpx" in result.error_message


# ---------------------------------------------------------------------------
# JSON / shape validation
# ---------------------------------------------------------------------------


async def test_invalid_json_response_returns_error() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "invalid json" in result.error_message


# ---------------------------------------------------------------------------
# api_base_url override
# ---------------------------------------------------------------------------


async def test_api_base_url_from_payload_overrides_config() -> None:
    """Self-hosted Telegram-API instances can override the URL on the
    credential payload."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(
        config=_telegram_config(api_base_url="https://api.telegram.org"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh(
        "u@x",
        current_payload={
            "bot_token": "tok",
            "api_base_url": "https://my-self-hosted.example.com",
        },
    )
    assert "my-self-hosted.example.com" in seen["url"]


async def test_api_base_url_payload_non_string_falls_back_to_config() -> None:
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(
        config=_telegram_config(api_base_url="https://api.telegram.org"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"bot_token": "t", "api_base_url": 42})
    assert "api.telegram.org" in seen["url"]


async def test_trailing_slash_api_base_url_normalised() -> None:
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(
        config=_telegram_config(api_base_url="https://api.telegram.org/"),
        transport=httpx.MockTransport(_handler),
    )
    await s.refresh("u@x", current_payload={"bot_token": "t"})
    assert "api.telegram.org/bott/getMe" in seen["url"]


# ---------------------------------------------------------------------------
# Latency capture
# ---------------------------------------------------------------------------


async def test_latency_ms_captured_on_success_path() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "t"})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.latency_ms >= 0.0


async def test_latency_ms_captured_on_error_path() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "t"})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.latency_ms >= 0.0


async def test_latency_ms_captured_on_short_circuit() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    s = _make_strategy(transport=transport)
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Concurrent calls — strategy is reentrant
# ---------------------------------------------------------------------------


async def test_strategy_is_reentrant_across_concurrent_calls() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    results = await asyncio.gather(
        *[s.refresh(f"u{i}@x", current_payload={"bot_token": f"t{i}"}) for i in range(5)]
    )
    assert all(r.outcome is RefreshOutcome.SUCCESS for r in results)


# ---------------------------------------------------------------------------
# get_me probe is idempotent — repeated calls give same outcome
# ---------------------------------------------------------------------------


async def test_repeated_calls_yield_same_outcome() -> None:
    """The probe is read-only — calling refresh twice in a row works
    and returns the same SUCCESS outcome."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    s = _make_strategy(transport=httpx.MockTransport(_handler))
    payload: dict[str, Any] = {"bot_token": "tok"}
    r1 = await s.refresh("u@x", current_payload=payload)
    r2 = await s.refresh("u@x", current_payload=payload)
    assert r1.outcome is RefreshOutcome.SUCCESS
    assert r2.outcome is RefreshOutcome.SUCCESS
