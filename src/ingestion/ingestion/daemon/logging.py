"""structlog configuration for the daemon.

JSON-line output on stdout. Correlation IDs bound via contextvars so every
log line inside a sync run carries the ``sync_id``, ``user_email``,
``platform``, and ``scope`` without callers having to pass them around.

Usage::

    from ingestion.daemon.logging import bind_sync_context, configure_logging, get_logger

    configure_logging("INFO")
    log = get_logger(__name__)

    with bind_sync_context(sync_id="abc", user_email="x@y", platform="teams", scope="messages"):
        log.info("sync.started")
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars

if TYPE_CHECKING:
    from collections.abc import Iterator

    from structlog.stdlib import BoundLogger


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for JSON output on stdout.

    Idempotent: safe to call more than once (later calls reset the chain).

    ``structlog.contextvars.merge_contextvars`` (first in the chain)
    auto-merges every bound contextvar onto each log record, including
    ``request_id`` set by
    :class:`ingestion.daemon.api.middleware.RequestIdMiddleware` and the
    sync-correlation fields bound by :func:`bind_sync_context`. No
    per-var declaration is needed; the import below is here to
    initialise the contextvar at module-load time so a logger call
    that races middleware install still finds the var (default is the
    empty string, so it renders as ``request_id=""`` rather than
    raising LookupError).
    """
    # Import for the side effect of defining ``request_id_var`` on the
    # contextvars module. The ContextVar default is the empty string so
    # logs outside an HTTP scope (background workers, scheduler ticks)
    # render cleanly without a missing-var warning. The ``_ =`` line
    # silences pyright's reportUnusedImport — we do touch the symbol.
    from ingestion.daemon.api.middleware.request_id import request_id_var

    _ = request_id_var

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> BoundLogger:
    return structlog.get_logger(name)


@contextmanager
def bind_sync_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for the duration of a ``with`` block.

    Unbinds only the keys it added so nested contexts don't stomp each other.
    Typical keys: ``sync_id``, ``user_email``, ``platform``, ``scope``.
    """
    bind_contextvars(**fields)
    try:
        yield
    finally:
        unbind_contextvars(*fields.keys())
