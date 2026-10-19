"""Smoke test: verify /api/settings/about works through the full stack.

If this passes, the daemon subprocess, Postgres wiring, session cookie
auth, and router dispatch are all healthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import httpx


@pytest.mark.e2e
async def test_about_returns_daemon_info(admin_session: httpx.AsyncClient) -> None:
    resp = await admin_session.get("/api/settings/about")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # Just check the shape — exact fields can evolve; the key signal is that
    # an authed GET actually succeeds end-to-end.
    assert isinstance(payload, dict)
    assert payload  # non-empty


@pytest.mark.e2e
async def test_about_rejects_unauthenticated(http_client: httpx.AsyncClient) -> None:
    resp = await http_client.get("/api/settings/about")
    assert resp.status_code == 401
