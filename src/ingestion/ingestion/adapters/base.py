"""Base adapter with shared scope resolution, credential validation, and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ingestion.schemas import (
    ErrorCode,
    FetchError,
    FetchScope,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class BaseAdapter:
    """Base class for platform adapters.

    Subclasses must set:
        platform: str
        _supported_scopes: frozenset[FetchScope]
        _required_credential_keys: tuple[str, ...]

    Provides:
        _resolve_scopes: Filter requested scopes to supported ones.
        _validate_credentials: Check required keys are present.
        _semaphore: asyncio.Semaphore for concurrent request limiting.
        Context manager support (__aenter__/__aexit__).
    """

    platform: str = ""
    _supported_scopes: frozenset[FetchScope] = frozenset()
    _required_credential_keys: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        max_concurrent_requests: int = 5,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._sleep_func = sleep_func

    def _resolve_scopes(self, scope: frozenset[FetchScope] | None) -> frozenset[FetchScope]:
        """Resolve requested scopes to the set this adapter supports.

        None means all supported scopes. Unsupported scopes log a warning.
        """
        if scope is None:
            return self._supported_scopes

        effective = scope & self._supported_scopes
        unsupported = scope - self._supported_scopes
        for s in unsupported:
            logger.warning(
                "%s adapter: scope %r is not supported, skipping.",
                self.platform,
                s,
            )
        return effective

    def _validate_credentials(self, credentials: dict) -> FetchError | None:
        """Return a FetchError if any required credential key is missing, else None."""
        for key in self._required_credential_keys:
            if not credentials.get(key):
                return FetchError(
                    code=ErrorCode.UNKNOWN,
                    message=f"Missing required credential: {key}",
                    recoverable=False,
                )
        return None

    async def __aenter__(self) -> BaseAdapter:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass
