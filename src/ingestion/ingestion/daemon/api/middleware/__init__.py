"""Request-scoped ASGI middlewares.

Distinct from :mod:`ingestion.daemon.api.middlewares` (plural) which
holds the broader API-protection layer (rate limit, request size).
This package is for cross-cutting middlewares that bind correlation
state — currently just request IDs, but the package gives future
tracing / metrics middlewares a home.
"""

from ingestion.daemon.api.middleware.deprecation import (
    DEFAULT_SUNSET_DATE,
    DeprecationHeaderMiddleware,
)
from ingestion.daemon.api.middleware.idempotency import (
    DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    IdempotencyMiddleware,
)
from ingestion.daemon.api.middleware.idempotency import (
    prune_expired as prune_idempotency_expired,
)
from ingestion.daemon.api.middleware.request_id import (
    RequestIdMiddleware,
    request_id_var,
)

__all__ = [
    "DEFAULT_IDEMPOTENCY_TTL_SECONDS",
    "DEFAULT_SUNSET_DATE",
    "DeprecationHeaderMiddleware",
    "IdempotencyMiddleware",
    "RequestIdMiddleware",
    "prune_idempotency_expired",
    "request_id_var",
]
