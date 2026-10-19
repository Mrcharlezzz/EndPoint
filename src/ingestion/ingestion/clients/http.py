"""Async HTTP client for Graph API and generic REST endpoints.

Inherits retry/backoff/rate-limiting from BaseHTTPClient.
Adds Bearer token auth, GET convenience, and OData pagination.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ingestion.clients.base import BaseHTTPClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class AsyncHTTPClient(BaseHTTPClient):
    """HTTP client for Microsoft Graph API and generic Bearer-token REST APIs.

    Decision #23: Per-instance rate limiting via requests_per_second.
    """

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        max_retries: int = 3,
        timeout: int = 30,
        requests_per_second: float = 10.0,
        headers: dict[str, str] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

        default_headers: dict[str, str] = {}
        if token:
            default_headers["Authorization"] = f"Bearer {token}"
        if headers:
            default_headers.update(headers)

        client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=default_headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

        super().__init__(
            max_retries=max_retries,
            timeout=timeout,
            requests_per_second=requests_per_second,
            sleep_func=sleep_func,
            client=client,
        )

    @property
    def _platform_name(self) -> str:
        return self._base_url

    async def get(self, url: str, params: dict | None = None) -> dict:
        """GET with retry, backoff, and error handling."""
        return await self._request("GET", url, params=params)

    async def get_paginated(
        self,
        url: str,
        params: dict | None = None,
        page_key: str = "value",
        next_link_key: str = "@odata.nextLink",
    ) -> list[dict]:
        """Follow OData-style pagination, collecting all items."""
        items: list[dict] = []
        current_url = url
        current_params = params

        while current_url:
            data = await self.get(current_url, params=current_params)
            items.extend(data.get(page_key, []))

            next_link = data.get(next_link_key)
            if next_link:
                current_url = next_link
                current_params = None
            else:
                break

        return items
