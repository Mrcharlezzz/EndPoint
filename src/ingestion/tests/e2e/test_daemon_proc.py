"""Smoke tests for the session-scoped daemon + http_client fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import httpx

    from tests.e2e.daemon_proc import DaemonProcess


@pytest.mark.e2e
async def test_daemon_about_endpoint_responds(http_client: httpx.AsyncClient) -> None:
    resp = await http_client.get("/api/settings/about")
    assert resp.status_code in (200, 401), resp.text


@pytest.mark.e2e
async def test_daemon_ws_base_url_derives_from_http(daemon: DaemonProcess) -> None:
    assert daemon.ws_base_url == daemon.base_url.replace("http://", "ws://")
