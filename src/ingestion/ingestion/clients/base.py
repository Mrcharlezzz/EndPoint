"""Base HTTP client with retry, exponential backoff, and per-instance rate limiting.

All platform-specific clients inherit from this base and override
template methods for response parsing and error detection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from ingestion.exceptions import (
    InsufficientScopeError,
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class BaseHTTPClient:
    """Async HTTP client with retry, exponential backoff, and rate limiting.

    Subclasses override template methods:
        _parse_response: Platform-specific response parsing (e.g. Moodle error-in-200).
        _parse_retry_after: Platform-specific Retry-After extraction.
        _platform_name: For error messages.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        timeout: int = 30,
        requests_per_second: float = 10.0,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._timeout = timeout
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last_request_time: float = 0.0
        self._rate_lock = asyncio.Lock()
        self._sleep = sleep_func or asyncio.sleep
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    # --- Template methods (override in subclasses) ---

    def _parse_response(self, response: httpx.Response, *, url: str) -> Any:
        """Parse a successful HTTP response. Override for platform-specific logic.

        Default: return JSON body on 200.
        Moodle: detect error-in-200 body.
        Telegram: unwrap {ok: true, result: ...} envelope.
        """
        return response.json()

    def _parse_retry_after(self, response: httpx.Response) -> float:
        """Extract retry delay from a 429 response. Override for platform-specific logic.

        Default: read Retry-After header, fallback to 5s.
        Telegram: read retry_after from JSON body.
        """
        raw = response.headers.get("Retry-After", "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return 5.0

    @property
    def _platform_name(self) -> str:
        """Platform name for error messages. Override in subclasses."""
        return "unknown"

    # --- Core request logic ---

    async def _rate_limit(self) -> None:
        """Enforce per-instance rate limiting."""
        if self._min_interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                await self._sleep(self._min_interval - elapsed)
            self._last_request_time = time.monotonic()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute HTTP request with retry, backoff, and error handling.

        Args:
            method: HTTP method ("GET" or "POST").
            url: Full URL or path.
            params: Query parameters.
            data: Form data (for POST).
            headers: Additional headers.

        Returns:
            Parsed response (via _parse_response template method).

        Raises:
            TokenExpiredError: on 401.
            InsufficientScopeError: on 403.
            RateLimitError: on 429 after retries exhausted.
            PlatformUnavailableError: on 5xx or connection failures after retries.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            await self._rate_limit()

            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                )
            except httpx.ConnectError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "%s connection error on %s, retrying in %ds (attempt %d)",
                        self._platform_name,
                        url,
                        wait,
                        attempt + 1,
                    )
                    await self._sleep(wait)
                    continue
                raise PlatformUnavailableError(
                    platform=self._platform_name,
                    message=f"Connection failed after {self._max_retries + 1} attempts: {exc}",
                ) from exc
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "%s timeout on %s, retrying in %ds (attempt %d)",
                        self._platform_name,
                        url,
                        wait,
                        attempt + 1,
                    )
                    await self._sleep(wait)
                    continue
                raise PlatformUnavailableError(
                    platform=self._platform_name,
                    message=f"Request timed out after {self._max_retries + 1} attempts: {exc}",
                ) from exc

            status = response.status_code

            if status == 401:
                raise TokenExpiredError(f"Token expired: {response.text[:200]}")

            if status == 403:
                raise InsufficientScopeError(
                    resource=url,
                    message=f"Forbidden: {response.text[:200]}",
                )

            if status == 429:
                retry_after = self._parse_retry_after(response)
                if attempt < self._max_retries:
                    logger.warning(
                        "%s rate limited on %s, retrying in %.1fs (attempt %d)",
                        self._platform_name,
                        url,
                        retry_after,
                        attempt + 1,
                    )
                    await self._sleep(retry_after)
                    continue
                raise RateLimitError(
                    retry_after=retry_after,
                    message=f"Rate limit exceeded after {self._max_retries + 1} attempts",
                )

            if 500 <= status < 600:
                last_error = PlatformUnavailableError(
                    platform=self._platform_name,
                    message=f"Server error {status}: {response.text[:200]}",
                )
                if attempt < self._max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "%s server error %d on %s, retrying in %ds (attempt %d)",
                        self._platform_name,
                        status,
                        url,
                        wait,
                        attempt + 1,
                    )
                    await self._sleep(wait)
                    continue
                raise last_error

            if status == 200:
                return self._parse_response(response, url=url)

            response.raise_for_status()

        raise PlatformUnavailableError(
            platform=self._platform_name,
            message=f"Request failed after {self._max_retries + 1} attempts: {last_error}",
        )

    # --- Lifecycle ---

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BaseHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
