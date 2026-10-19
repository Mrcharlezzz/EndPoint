"""Unit tests for StubServer — exercised in-process, no daemon.

These verify the stub-server machinery itself before we rely on it in
session-scoped fixtures.
"""

from __future__ import annotations

import aiohttp
import pytest

from tests.e2e.stubs.base import StubServer


@pytest.mark.asyncio
async def test_stub_server_returns_pushed_response():
    stub = StubServer(name="test")
    await stub.start()
    try:
        stub.push("GET", "/widgets", status=200, json={"ok": True, "widgets": [1, 2]})
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{stub.base_url}/widgets") as resp,
        ):
            assert resp.status == 200
            payload = await resp.json()
            assert payload == {"ok": True, "widgets": [1, 2]}
        recorded = stub.recorded_requests()
        assert len(recorded) == 1
        assert recorded[0]["method"] == "GET"
        assert recorded[0]["path"] == "/widgets"
    finally:
        await stub.stop()


@pytest.mark.asyncio
async def test_stub_server_404_when_no_push():
    stub = StubServer(name="test")
    await stub.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{stub.base_url}/nothing") as resp,
        ):
            assert resp.status == 404
    finally:
        await stub.stop()


@pytest.mark.asyncio
async def test_stub_server_reset_clears_queues_and_recorded_requests():
    stub = StubServer(name="test")
    await stub.start()
    try:
        stub.push("GET", "/x", status=200, json={})
        async with aiohttp.ClientSession() as session:
            await session.get(f"{stub.base_url}/x")
        assert len(stub.recorded_requests()) == 1
        stub.reset()
        assert stub.recorded_requests() == []
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"{stub.base_url}/x") as resp,
        ):
            assert resp.status == 404  # queue was reset
    finally:
        await stub.stop()


@pytest.mark.asyncio
async def test_stub_server_recorded_json_decodes_request_body():
    stub = StubServer(name="test")
    await stub.start()
    try:
        stub.push("POST", "/items", status=201, json={"id": 1})
        async with (
            aiohttp.ClientSession() as session,
            session.post(f"{stub.base_url}/items", json={"name": "widget", "qty": 3}) as resp,
        ):
            assert resp.status == 201
        assert stub.recorded_json() == {"name": "widget", "qty": 3}
        assert stub.recorded_json(index=0) == {"name": "widget", "qty": 3}
    finally:
        await stub.stop()
