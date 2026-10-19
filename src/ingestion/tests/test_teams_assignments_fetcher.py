"""Tests for Assignments fetcher — class assignments and resources."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.teams.assignments_fetcher import (
    fetch_assignment_resources,
    fetch_class_assignments,
)
from ingestion.clients.http import AsyncHTTPClient

ASSIGNMENTS_BASE = "https://assignments.test.edu.cloud.microsoft"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def assignments_client() -> AsyncHTTPClient:
    return AsyncHTTPClient(
        base_url=ASSIGNMENTS_BASE,
        token="edu-token",
        max_retries=1,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


class TestFetchClassAssignments:
    @respx.mock
    async def test_returns_assignments(self, assignments_client: AsyncHTTPClient) -> None:
        url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/c1/assignments"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "a1", "displayName": "Homework 1"},
                        {"id": "a2", "displayName": "Homework 2"},
                    ]
                },
            )
        )
        assignments = await fetch_class_assignments(
            assignments_client, "c1", base_url=ASSIGNMENTS_BASE
        )
        assert len(assignments) == 2
        assert assignments[0]["id"] == "a1"
        assert assignments[1]["displayName"] == "Homework 2"

    @respx.mock
    async def test_403_returns_empty(self, assignments_client: AsyncHTTPClient) -> None:
        url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/c1/assignments"
        respx.get(url).mock(return_value=httpx.Response(403, text="Forbidden"))
        assignments = await fetch_class_assignments(
            assignments_client, "c1", base_url=ASSIGNMENTS_BASE
        )
        assert assignments == []


class TestFetchAssignmentResources:
    @respx.mock
    async def test_returns_resources(self, assignments_client: AsyncHTTPClient) -> None:
        url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/c1/assignments/a1/resources"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "r1", "resource": {"displayName": "essay.pdf"}},
                        {"id": "r2", "resource": {"displayName": "slides.pptx"}},
                    ]
                },
            )
        )
        resources = await fetch_assignment_resources(
            assignments_client, "c1", "a1", base_url=ASSIGNMENTS_BASE
        )
        assert len(resources) == 2
        assert resources[0]["resource"]["displayName"] == "essay.pdf"

    @respx.mock
    async def test_403_returns_empty(self, assignments_client: AsyncHTTPClient) -> None:
        url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/c1/assignments/a1/resources"
        respx.get(url).mock(return_value=httpx.Response(403, text="Forbidden"))
        resources = await fetch_assignment_resources(
            assignments_client, "c1", "a1", base_url=ASSIGNMENTS_BASE
        )
        assert resources == []

    @respx.mock
    async def test_5xx_returns_empty(self, assignments_client: AsyncHTTPClient) -> None:
        url = f"{ASSIGNMENTS_BASE}/api/v1.0/edu/classes/c1/assignments/a1/resources"
        respx.get(url).mock(return_value=httpx.Response(500, text="Internal Server Error"))
        resources = await fetch_assignment_resources(
            assignments_client, "c1", "a1", base_url=ASSIGNMENTS_BASE
        )
        assert resources == []
