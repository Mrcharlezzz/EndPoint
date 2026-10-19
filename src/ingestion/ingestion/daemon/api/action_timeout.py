"""Per-action timeout helper for destructive HTTP routes.

Slice 10 mandates that every destructive action handler runs under a
hard timeout so a single hung dependency cannot indefinitely hold an
HTTP request open. The default budget is
``config.api.action_timeout_seconds`` (30s).

On timeout the helper raises :class:`TransientFailureError`, which the
:func:`service_error_to_http` translator already maps to 502 + ``code:
transient_failure`` (slice 4). Clients see a structured envelope and
the audit log carries ``operation_timeout`` so operators can correlate
a 502 with the budget exceeded.

Use::

    return await with_action_timeout(
        ctx.config.api.action_timeout_seconds,
        sync_service.force_sync_one(...),
    )

The helper takes an awaitable rather than a callable so existing
service-layer call sites can pass ``sync_service.foo(args)`` directly
without changing their signature. ``asyncio.wait_for`` cancels the
underlying task on timeout, which propagates as
``asyncio.CancelledError`` through the coroutine — service-layer code
that holds a database connection should already use ``shield`` around
critical commits, but a transient-stop on cancel is acceptable for
operator actions (the action either lands or doesn't; partial
side-effects are caught in the audit row).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ingestion.daemon.services.exceptions import TransientFailureError

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def with_action_timeout[T](
    timeout_seconds: float,
    awaitable: Coroutine[object, object, T],
) -> T:
    """Run ``awaitable`` under a hard timeout.

    On expiration raises :class:`TransientFailureError` carrying
    ``{"reason": "operation_timeout", "timeout_seconds": ...}``. The
    underlying coroutine is cancelled cleanly via ``asyncio.wait_for``;
    the cancellation propagates to the work being timed out, so any
    held connection is released.

    A non-positive ``timeout_seconds`` is treated as "no timeout" so
    test harnesses can disable the cap without rewiring config. Pin
    this in the docstring rather than enforcing a minimum because the
    config schema allows operators to lower or raise the value
    intentionally.
    """
    if timeout_seconds <= 0:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise TransientFailureError(
            {
                "reason": "operation_timeout",
                "timeout_seconds": timeout_seconds,
            },
        ) from exc


__all__ = ["with_action_timeout"]
