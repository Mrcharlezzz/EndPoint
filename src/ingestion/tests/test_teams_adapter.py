"""Tests for TeamsAdapter — scope routing, credential validation, error boundary."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.teams.adapter import TeamsAdapter
from ingestion.config import TeamsConfig
from ingestion.schemas import (
    ContentType,
    ErrorCode,
    FetchResult,
    FetchScope,
    FileRef,
)

GRAPH_BASE = "https://graph.test.api/v1.0"
CSA_BASE = "https://csa.test.api/v2"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def config() -> TeamsConfig:
    return TeamsConfig(
        graph_base_url=GRAPH_BASE,
        csa_base_url=CSA_BASE,
        csa_page_size=10,
        max_retries=0,
        request_timeout=5,
        requests_per_second=0,
    )


@pytest.fixture
def adapter(config: TeamsConfig) -> TeamsAdapter:
    return TeamsAdapter(config, sleep_func=_noop_sleep)


@pytest.fixture
def credentials() -> dict:
    return {
        "graph_token": "graph-tok",
        "csa_token": "csa-tok",
        "team_ids": ["team-1"],
    }


def _graph_channels_response(channels: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"value": channels})


def _csa_messages_response(messages: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "replyChains": [{"messages": messages}],
            "hasMore": False,
        },
    )


class TestProtocolCompliance:
    def test_platform_attribute(self, adapter: TeamsAdapter) -> None:
        assert adapter.platform == "teams"

    @respx.mock
    async def test_test_connection_success(self, adapter: TeamsAdapter, credentials: dict) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(
            return_value=httpx.Response(200, json={"displayName": "Test"})
        )
        assert await adapter.test_connection(credentials) is True

    @respx.mock
    async def test_test_connection_failure(self, adapter: TeamsAdapter, credentials: dict) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
        assert await adapter.test_connection(credentials) is False


class TestCredentialValidation:
    async def test_missing_graph_token(self, adapter: TeamsAdapter) -> None:
        result = await adapter.fetch({"team_ids": ["t1"]})
        assert isinstance(result, FetchResult)
        assert len(result.errors) == 1
        assert result.errors[0].code == ErrorCode.UNKNOWN
        assert "graph_token" in result.errors[0].message.lower()

    async def test_missing_team_ids(self, adapter: TeamsAdapter) -> None:
        result = await adapter.fetch({"graph_token": "tok"})
        assert len(result.errors) == 1
        assert "team_ids" in result.errors[0].message.lower()

    async def test_empty_team_ids(self, adapter: TeamsAdapter) -> None:
        result = await adapter.fetch({"graph_token": "tok", "team_ids": []})
        assert len(result.errors) == 1


class TestScopeRouting:
    @respx.mock
    async def test_messages_scope_fetches_csa(
        self, adapter: TeamsAdapter, credentials: dict
    ) -> None:
        """MESSAGES scope: fetch channels via Graph, messages via CSA."""
        respx.get(f"{GRAPH_BASE}/teams/team-1/channels").mock(
            return_value=_graph_channels_response([{"id": "ch-1", "displayName": "General"}])
        )
        respx.get(f"{CSA_BASE}/teams/ch-1/channels/ch-1").mock(
            return_value=_csa_messages_response(
                [
                    {
                        "id": "m1",
                        "imDisplayName": "Alice",
                        "content": "<p>Hello</p>",
                        "messageType": "RichText/Html",
                        "composeTime": "2026-03-12T10:00:00Z",
                        "properties": {},
                    }
                ]
            )
        )

        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert len(result.items) == 1
        assert result.items[0].content_type == ContentType.RAW_MESSAGE
        assert result.items[0].author == "Alice"
        assert result.items[0].body == "Hello"

    async def test_messages_without_csa_falls_back_to_graph(self, adapter: TeamsAdapter) -> None:
        """Without csa_token, MESSAGES scope is skipped — returns 0 items."""
        creds = {"graph_token": "g-tok", "team_ids": ["team-1"]}

        result = await adapter.fetch(creds, scope=frozenset({FetchScope.MESSAGES}))
        assert len(result.items) == 0

    @respx.mock
    async def test_files_scope(self, adapter: TeamsAdapter, credentials: dict) -> None:
        respx.get(f"{GRAPH_BASE}/groups/team-1/drives").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "d1", "name": "Documents"}]})
        )
        respx.get(f"{GRAPH_BASE}/drives/d1/root/delta").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "notes.pdf",
                            "size": 1024,
                            "file": {"mimeType": "application/pdf"},
                            "@microsoft.graph.downloadUrl": "https://dl/f1",
                        }
                    ]
                },
            )
        )

        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.FILES}))
        assert len(result.files) == 1
        assert isinstance(result.files[0], FileRef)
        assert result.files[0].filename == "notes.pdf"
        assert result.items == []  # files don't produce NormalizedItems

    @respx.mock
    async def test_unsupported_scope_skipped(
        self, adapter: TeamsAdapter, credentials: dict
    ) -> None:
        """FORUMS scope is unsupported — skipped with no error."""
        # Still need channels for MESSAGES to work, but let's just test FORUMS alone
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.FORUMS}))
        assert result.items == []
        assert result.errors == []


class TestErrorBoundary:
    @respx.mock
    async def test_token_expired_captured(self, adapter: TeamsAdapter, credentials: dict) -> None:
        """401 from Graph becomes a FetchError, not an exception."""
        respx.get(f"{GRAPH_BASE}/teams/team-1/channels").mock(
            return_value=httpx.Response(401, text="Token expired")
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert len(result.errors) >= 1
        token_errors = [e for e in result.errors if e.code == ErrorCode.TOKEN_EXPIRED]
        assert len(token_errors) >= 1
        assert token_errors[0].recoverable is False


class TestCursorManagement:
    @respx.mock
    async def test_cursor_contains_last_sync_at(
        self, adapter: TeamsAdapter, credentials: dict
    ) -> None:
        respx.get(f"{GRAPH_BASE}/teams/team-1/channels").mock(
            return_value=_graph_channels_response([{"id": "ch-1", "displayName": "General"}])
        )
        respx.get(f"{CSA_BASE}/teams/ch-1/channels/ch-1").mock(
            return_value=_csa_messages_response([])
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert result.cursor is not None
        assert "last_sync_at" in result.cursor


class TestStats:
    @respx.mock
    async def test_stats_populated(self, adapter: TeamsAdapter, credentials: dict) -> None:
        respx.get(f"{GRAPH_BASE}/teams/team-1/channels").mock(
            return_value=_graph_channels_response([{"id": "ch-1", "displayName": "General"}])
        )
        respx.get(f"{CSA_BASE}/teams/ch-1/channels/ch-1").mock(
            return_value=_csa_messages_response(
                [
                    {
                        "id": "m1",
                        "imDisplayName": "A",
                        "content": "hi",
                        "messageType": "Text",
                        "composeTime": "2026-03-12T10:00:00Z",
                        "properties": {},
                    }
                ]
            )
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert result.stats.items_fetched == 1
