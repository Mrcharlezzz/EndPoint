"""Ingestion pipeline exception hierarchy.

All custom exceptions inherit from IngestionError so callers can
catch the entire family with a single except clause.
"""

from __future__ import annotations


class IngestionError(Exception):
    """Base exception for all ingestion pipeline errors."""


class TokenExpiredError(IngestionError):
    """HTTP 401 — token expired or invalid. Caller should mark token expired and notify user."""


class InsufficientScopeError(IngestionError):
    """HTTP 403 — missing permissions for this resource. Skip resource and continue."""

    def __init__(self, resource: str, message: str = "") -> None:
        self.resource = resource
        super().__init__(message or f"Insufficient scope for resource: {resource}")


class RateLimitError(IngestionError):
    """HTTP 429 — rate limit exceeded after all retries exhausted."""

    def __init__(self, retry_after: float | None = None, message: str = "") -> None:
        self.retry_after = retry_after
        super().__init__(message or f"Rate limit exceeded (retry_after={retry_after}s)")


class PlatformUnavailableError(IngestionError):
    """HTTP 5xx after retries — platform is down or unreachable."""

    def __init__(self, platform: str, message: str = "") -> None:
        self.platform = platform
        super().__init__(message or f"Platform unavailable: {platform}")


class NormalizationError(IngestionError):
    """Malformed data from API — skip item, log, continue."""

    def __init__(self, item_id: str, message: str = "") -> None:
        self.item_id = item_id
        super().__init__(message or f"Failed to normalize item: {item_id}")
