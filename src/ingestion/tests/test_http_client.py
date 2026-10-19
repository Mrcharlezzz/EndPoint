"""Tests for the shared async HTTP client.

This IS a case where respx mocking is appropriate — we're testing
retry/backoff/error handling behavior of the HTTP layer itself.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.clients.http import AsyncHTTPClient
from ingestion.exceptions import (
    InsufficientScopeError,
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)


@pytest.fixture
def base_url() -> str:
    return "https://api.example.com"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def client(base_url: str) -> AsyncHTTPClient:
    return AsyncHTTPClient(
        base_url=base_url,
        token="test-token",
        max_retries=2,
        timeout=5,
        requests_per_second=0,  # disable rate limiting for tests
        sleep_func=_noop_sleep,
    )


@respx.mock
async def test_successful_get(client: AsyncHTTPClient, base_url: str) -> None:
    respx.get(f"{base_url}/data").mock(return_value=httpx.Response(200, json={"value": [1, 2, 3]}))
    result = await client.get("/data")
    assert result == {"value": [1, 2, 3]}


@respx.mock
async def test_401_raises_token_expired(client: AsyncHTTPClient, base_url: str) -> None:
    respx.get(f"{base_url}/data").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(TokenExpiredError):
        await client.get("/data")


@respx.mock
async def test_403_raises_insufficient_scope(client: AsyncHTTPClient, base_url: str) -> None:
    respx.get(f"{base_url}/data").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(InsufficientScopeError) as exc_info:
        await client.get("/data")
    assert exc_info.value.resource == "/data"


@respx.mock
async def test_429_retries_then_raises(client: AsyncHTTPClient, base_url: str) -> None:
    route = respx.get(f"{base_url}/data").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    with pytest.raises(RateLimitError):
        await client.get("/data")
    assert route.call_count == 3  # initial + 2 retries


@respx.mock
async def test_429_retries_then_succeeds(client: AsyncHTTPClient, base_url: str) -> None:
    route = respx.get(f"{base_url}/data")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": True}),
    ]
    result = await client.get("/data")
    assert result == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_5xx_retries_then_raises(client: AsyncHTTPClient, base_url: str) -> None:
    route = respx.get(f"{base_url}/data").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    with pytest.raises(PlatformUnavailableError):
        await client.get("/data")
    assert route.call_count == 3  # initial + 2 retries


@respx.mock
async def test_5xx_retries_then_succeeds(client: AsyncHTTPClient, base_url: str) -> None:
    route = respx.get(f"{base_url}/data")
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json={"recovered": True}),
    ]
    result = await client.get("/data")
    assert result == {"recovered": True}
    assert route.call_count == 2


@respx.mock
async def test_pagination_follows_next_link(client: AsyncHTTPClient, base_url: str) -> None:
    respx.get(url__eq=f"{base_url}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": 1}, {"id": 2}],
                "@odata.nextLink": f"{base_url}/items?page=2",
            },
        )
    )
    respx.get(url__eq=f"{base_url}/items?page=2").mock(
        return_value=httpx.Response(
            200,
            json={"value": [{"id": 3}]},
        )
    )

    items = await client.get_paginated("/items")
    assert len(items) == 3
    assert items[0]["id"] == 1
    assert items[2]["id"] == 3


@respx.mock
async def test_pagination_single_page(client: AsyncHTTPClient, base_url: str) -> None:
    respx.get(f"{base_url}/items").mock(
        return_value=httpx.Response(200, json={"value": [{"id": 1}]})
    )
    items = await client.get_paginated("/items")
    assert len(items) == 1


@respx.mock
async def test_connection_error_retries(base_url: str) -> None:
    client = AsyncHTTPClient(
        base_url=base_url,
        max_retries=1,
        timeout=1,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )
    route = respx.get(f"{base_url}/data").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(PlatformUnavailableError):
        await client.get("/data")
    assert route.call_count == 2  # initial + 1 retry


@respx.mock
async def test_bearer_token_sent(client: AsyncHTTPClient, base_url: str) -> None:
    route = respx.get(f"{base_url}/me").mock(
        return_value=httpx.Response(200, json={"name": "test"})
    )
    await client.get("/me")
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"
