"""HTTP client for the headless Teams-login service's internal payload endpoint."""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings


class HeadlessError(RuntimeError):
    pass


class HeadlessNotFound(HeadlessError):
    pass


class HeadlessUnauthorized(HeadlessError):
    pass


class HeadlessUnconfigured(HeadlessError):
    pass


def fetch_payload(session_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Pull the full token + cookie payload for a captured headless session.

    Returns the raw JSON response (``{session_id, status, tokens, refresh_token,
    team_ids, cookies, error}``). Raises :class:`HeadlessNotFound` on 404,
    :class:`HeadlessUnauthorized` on 401, :class:`HeadlessUnconfigured` on
    503, and :class:`HeadlessError` on anything else.
    """
    base = settings.HEADLESS_INTERNAL_URL.rstrip("/")
    secret = settings.HEADLESS_INTERNAL_SECRET
    if not secret:
        raise HeadlessUnconfigured("HEADLESS_INTERNAL_SECRET is not set")

    url = f"{base}/internal/auth/teams/{session_id}/payload"
    try:
        resp = httpx.get(
            url,
            headers={"X-Internal-Secret": secret, "Accept": "application/json"},
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise HeadlessError(f"headless service unreachable: {exc}") from exc

    if resp.status_code == 404:
        raise HeadlessNotFound("headless session not found")
    if resp.status_code == 401:
        raise HeadlessUnauthorized("headless rejected internal secret")
    if resp.status_code == 503:
        raise HeadlessUnconfigured("headless internal endpoint disabled")
    if resp.status_code >= 400:
        raise HeadlessError(f"headless returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if not isinstance(data, dict):
        raise HeadlessError("headless payload was not a JSON object")
    return data
