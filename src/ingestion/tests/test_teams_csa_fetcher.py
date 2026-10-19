"""Tests for CSA fetcher — continuationToken pagination and error handling."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.teams.csa_fetcher import fetch_channel_messages
from ingestion.clients.http import AsyncHTTPClient

CSA_BASE = "https://csa.test.api/v2"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def csa_client() -> AsyncHTTPClient:
    return AsyncHTTPClient(
        base_url=CSA_BASE,
        token="csa-token",
        max_retries=1,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


def _csa_url(thread_id: str, channel_id: str) -> str:
    return f"{CSA_BASE}/teams/{thread_id}/channels/{channel_id}"


def _csa_response(
    messages: list[dict],
    *,
    has_more: bool = False,
    continuation_token: str | None = None,
) -> dict:
    return {
        "replyChains": [{"messages": messages}],
        "hasMore": has_more,
        "continuationToken": continuation_token,
    }


class TestFetchChannelMessages:
    @respx.mock
    async def test_single_page(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json=_csa_response([{"id": "m1", "content": "hello"}]),
            )
        )
        messages, token = await fetch_channel_messages(csa_client, "t1", "c1", base_url=CSA_BASE)
        assert len(messages) == 1
        assert messages[0]["id"] == "m1"
        assert token is None

    @respx.mock
    async def test_pagination_follows_continuation_token(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        route = respx.get(url).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_csa_response(
                        [{"id": "m1"}],
                        has_more=True,
                        continuation_token="tok-page2",
                    ),
                ),
                httpx.Response(
                    200,
                    json=_csa_response([{"id": "m2"}]),
                ),
            ]
        )
        messages, token = await fetch_channel_messages(csa_client, "t1", "c1", base_url=CSA_BASE)
        assert len(messages) == 2
        assert messages[0]["id"] == "m1"
        assert messages[1]["id"] == "m2"
        assert token is None
        assert route.call_count == 2

    @respx.mock
    async def test_multiple_reply_chains(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "replyChains": [
                        {"messages": [{"id": "m1"}, {"id": "m2"}]},
                        {"messages": [{"id": "m3"}]},
                    ],
                    "hasMore": False,
                },
            )
        )
        messages, _ = await fetch_channel_messages(csa_client, "t1", "c1", base_url=CSA_BASE)
        assert len(messages) == 3

    @respx.mock
    async def test_empty_response(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"replyChains": [], "hasMore": False},
            )
        )
        messages, token = await fetch_channel_messages(csa_client, "t1", "c1", base_url=CSA_BASE)
        assert messages == []
        assert token is None

    @respx.mock
    async def test_page_size_param_sent(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        route = respx.get(url).mock(
            return_value=httpx.Response(200, json=_csa_response([{"id": "m1"}]))
        )
        await fetch_channel_messages(csa_client, "t1", "c1", page_size=25, base_url=CSA_BASE)
        request = route.calls[0].request
        assert "pageSize=25" in str(request.url)

    @respx.mock
    async def test_existing_continuation_token_used(self, csa_client: AsyncHTTPClient) -> None:
        url = _csa_url("t1", "c1")
        route = respx.get(url).mock(
            return_value=httpx.Response(200, json=_csa_response([{"id": "m1"}]))
        )
        await fetch_channel_messages(
            csa_client,
            "t1",
            "c1",
            continuation_token="resume-tok",
            base_url=CSA_BASE,
        )
        request = route.calls[0].request
        assert "continuationToken=resume-tok" in str(request.url)

    @respx.mock
    async def test_continuation_token_in_second_page_params(
        self, csa_client: AsyncHTTPClient
    ) -> None:
        """Verify the second page request includes the continuationToken."""
        url = _csa_url("t1", "c1")
        route = respx.get(url).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_csa_response(
                        [{"id": "m1"}],
                        has_more=True,
                        continuation_token="page2-tok",
                    ),
                ),
                httpx.Response(200, json=_csa_response([{"id": "m2"}])),
            ]
        )
        await fetch_channel_messages(csa_client, "t1", "c1", base_url=CSA_BASE)
        second_request = route.calls[1].request
        assert "continuationToken=page2-tok" in str(second_request.url)
