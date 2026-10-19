"""Tests for Graph fetcher — channels, assignments, drive files."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.teams.graph_fetcher import (
    fetch_channels,
    fetch_drive_items_delta,
    fetch_drive_items_recursive,
    fetch_team_drives,
)
from ingestion.clients.http import AsyncHTTPClient

GRAPH_BASE = "https://graph.test.api/v1.0"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def graph_client() -> AsyncHTTPClient:
    return AsyncHTTPClient(
        base_url=GRAPH_BASE,
        token="graph-token",
        max_retries=1,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


class TestFetchChannels:
    @respx.mock
    async def test_returns_channels(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/teams/t1/channels"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "c1", "displayName": "General"},
                        {"id": "c2", "displayName": "Homework"},
                    ]
                },
            )
        )
        channels = await fetch_channels(graph_client, "t1", base_url=GRAPH_BASE)
        assert len(channels) == 2
        assert channels[0]["id"] == "c1"
        assert channels[1]["displayName"] == "Homework"

    @respx.mock
    async def test_empty_team(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/teams/t1/channels"
        respx.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        channels = await fetch_channels(graph_client, "t1", base_url=GRAPH_BASE)
        assert channels == []


class TestFetchTeamDrives:
    @respx.mock
    async def test_returns_drives(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/groups/t1/drives"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"value": [{"id": "d1", "name": "Documents"}]},
            )
        )
        drives = await fetch_team_drives(graph_client, "t1", base_url=GRAPH_BASE)
        assert len(drives) == 1
        assert drives[0]["name"] == "Documents"

    @respx.mock
    async def test_403_returns_empty(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/groups/t1/drives"
        respx.get(url).mock(return_value=httpx.Response(403, text="Forbidden"))
        drives = await fetch_team_drives(graph_client, "t1", base_url=GRAPH_BASE)
        assert drives == []


class TestFetchDriveItemsRecursive:
    @respx.mock
    async def test_flat_files(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/children"
        respx.get(url).mock(
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
                        },
                        {
                            "id": "f2",
                            "name": "slides.pptx",
                            "size": 2048,
                            "file": {"mimeType": "application/pptx"},
                            "@microsoft.graph.downloadUrl": "https://dl/f2",
                        },
                    ]
                },
            )
        )
        items = await fetch_drive_items_recursive(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 2
        assert items[0]["name"] == "notes.pdf"

    @respx.mock
    async def test_recursive_folder(self, graph_client: AsyncHTTPClient) -> None:
        root_url = f"{GRAPH_BASE}/drives/d1/root/children"
        folder_url = f"{GRAPH_BASE}/drives/d1/items/folder-1/children"

        respx.get(root_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "folder-1", "name": "Week1", "folder": {"childCount": 1}},
                    ]
                },
            )
        )
        respx.get(folder_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "hw1.pdf",
                            "size": 512,
                            "file": {"mimeType": "application/pdf"},
                            "@microsoft.graph.downloadUrl": "https://dl/f1",
                        }
                    ]
                },
            )
        )
        items = await fetch_drive_items_recursive(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 1
        assert items[0]["name"] == "hw1.pdf"

    @respx.mock
    async def test_empty_drive(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/children"
        respx.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        items = await fetch_drive_items_recursive(graph_client, "d1", base_url=GRAPH_BASE)
        assert items == []

    @respx.mock
    async def test_403_returns_empty(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/children"
        respx.get(url).mock(return_value=httpx.Response(403, text="Forbidden"))
        items = await fetch_drive_items_recursive(graph_client, "d1", base_url=GRAPH_BASE)
        assert items == []


class TestFetchDriveItemsDelta:
    @respx.mock
    async def test_returns_files_only(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/delta"
        respx.get(url).mock(
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
                        },
                        {
                            "id": "folder-1",
                            "name": "Week1",
                            "folder": {"childCount": 3},
                        },
                        {
                            "id": "f2",
                            "name": "slides.pptx",
                            "size": 2048,
                            "file": {"mimeType": "application/pptx"},
                            "@microsoft.graph.downloadUrl": "https://dl/f2",
                        },
                    ]
                },
            )
        )
        items = await fetch_drive_items_delta(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 2
        assert items[0]["name"] == "notes.pdf"
        assert items[1]["name"] == "slides.pptx"

    @respx.mock
    async def test_filters_deleted_items(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/delta"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "active.pdf",
                            "file": {"mimeType": "application/pdf"},
                        },
                        {
                            "id": "f2",
                            "name": "removed.pdf",
                            "file": {"mimeType": "application/pdf"},
                            "deleted": {"state": "deleted"},
                        },
                    ]
                },
            )
        )
        items = await fetch_drive_items_delta(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 1
        assert items[0]["name"] == "active.pdf"

    @respx.mock
    async def test_empty_drive(self, graph_client: AsyncHTTPClient) -> None:
        url = f"{GRAPH_BASE}/drives/d1/root/delta"
        respx.get(url).mock(return_value=httpx.Response(200, json={"value": []}))
        items = await fetch_drive_items_delta(graph_client, "d1", base_url=GRAPH_BASE)
        assert items == []

    @respx.mock
    async def test_403_falls_back_to_recursive(self, graph_client: AsyncHTTPClient) -> None:
        delta_url = f"{GRAPH_BASE}/drives/d1/root/delta"
        root_url = f"{GRAPH_BASE}/drives/d1/root/children"

        respx.get(delta_url).mock(return_value=httpx.Response(403, text="Forbidden"))
        respx.get(root_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "fallback.pdf",
                            "file": {"mimeType": "application/pdf"},
                        }
                    ]
                },
            )
        )
        items = await fetch_drive_items_delta(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 1
        assert items[0]["name"] == "fallback.pdf"

    @respx.mock
    async def test_pagination(self, graph_client: AsyncHTTPClient) -> None:
        page1_url = f"{GRAPH_BASE}/drives/d1/root/delta"
        page2_url = f"{GRAPH_BASE}/drives/d1/delta-next?token=page2"

        respx.get(url__eq=page1_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f1", "name": "a.pdf", "file": {"mimeType": "application/pdf"}},
                    ],
                    "@odata.nextLink": page2_url,
                },
            )
        )
        respx.get(url__eq=page2_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f2", "name": "b.pdf", "file": {"mimeType": "application/pdf"}},
                    ],
                },
            )
        )
        items = await fetch_drive_items_delta(graph_client, "d1", base_url=GRAPH_BASE)
        assert len(items) == 2
        assert items[0]["name"] == "a.pdf"
        assert items[1]["name"] == "b.pdf"
