"""Exponential backoff with jitter for retrying transient failures.

The :func:`with_backoff` helper wraps a fallible coroutine in a retry
loop with capped exponential backoff and optional jitter. Jitter is
critical: without it, N callers all retrying after the same failure
window would re-storm the upstream at the same instant (the thundering
herd). With jitter, each caller's retry lands at a random offset
within the window so the recovery storm spreads out.

Caller-supplied :class:`RetryPolicy` lets the auth refresh path use
short delays (1s base) while the pipeline fetch path uses longer
delays (2s base) — fetch retries are more expensive so we want fewer,
slower retries.

The helper does NOT decide what's retryable: callers pass in only the
work they want retried. A non-retryable exception (e.g.
:class:`ingestion.exceptions.TokenExpiredError`) should be raised
unwrapped from inside the callable so the helper sees it and re-raises
on the first attempt without consuming the retry budget. Use the
``retryable`` predicate to filter classes that should NOT trigger a
retry.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Typed config for :func:`with_backoff` so callers can pin and
    pass concrete configurations instead of repeating kwargs.

    ``base_delay_seconds`` is the delay applied after the FIRST
    failure. Subsequent failures double the delay, capped at
    ``max_delay_seconds``. With ``jitter=True`` the actual delay is
    sampled uniformly from ``[0, computed_delay]`` so concurrent
    callers spread their retries.

    ``max_attempts`` is the total attempt budget — first call counts.
    A value of 1 means no retries; the function is invoked exactly
    once and the original exception propagates on failure.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter: bool = True


_DEFAULT_POLICY = RetryPolicy()


def _compute_delay(
    attempt: int,
    *,
    base_delay_seconds: float,
    max_delay_seconds: float,
    jitter: bool,
    rng: random.Random | None,
) -> float:
    """Return the delay (seconds) to wait BEFORE attempt ``attempt``.

    ``attempt`` is zero-based: 0 means "before the first attempt"
    (always 0; no delay). ``attempt=1`` is the first retry.

    The computed-then-jittered delay is bounded by
    ``max_delay_seconds`` so a high attempt count cannot overflow
    into multi-hour waits.
    """
    if attempt <= 0:
        return 0.0
    raw = base_delay_seconds * (2 ** (attempt - 1))
    capped = min(raw, max_delay_seconds)
    if not jitter:
        return capped
    r = rng or random
    # Uniform jitter on [0, capped] is the simplest safe shape.
    # Equal-jitter and decorrelated-jitter variants exist but the
    # simple form is sufficient at our scale (low N callers, low
    # retry budget).
    return r.uniform(0.0, capped)


async def with_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 30.0,
    jitter: bool = True,
    retryable: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Invoke ``fn`` with exponential backoff and optional jitter.

    On exception, sleeps for ``base * 2**(attempt-1)`` seconds (capped
    at ``max_delay_seconds`` and optionally jittered) before retrying.
    After ``max_attempts`` failures the last exception is re-raised.

    ``retryable`` is an optional predicate that returns False for
    exceptions that should NOT be retried (auth errors, validation
    failures). When the predicate returns False the original
    exception is re-raised immediately and the rest of the budget is
    discarded.

    ``sleep`` is injected so tests can replace ``asyncio.sleep`` with
    a no-op deterministic stub. ``rng`` is the random number generator
    for jitter; tests can pass a seeded ``random.Random`` for
    repeatable delays.

    Cancellation is honoured: an ``asyncio.CancelledError`` propagates
    immediately without consuming the retry budget. The helper does
    not synthesise a final wrapping exception; whichever original was
    raised on the last attempt surfaces unchanged.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    log = get_logger(__name__)
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        delay = _compute_delay(
            attempt,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            jitter=jitter,
            rng=rng,
        )
        if delay > 0:
            try:
                await sleep(delay)
            except asyncio.CancelledError:
                raise
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — caller-tunable via retryable
            last_exc = exc
            if retryable is not None and not retryable(exc):
                # Non-retryable; surface immediately without burning
                # the rest of the budget.
                raise
            if attempt + 1 >= max_attempts:
                # Out of attempts; re-raise the last exception so the
                # caller sees the original cause, not a wrapper.
                raise
            log.warning(
                "backoff.retry",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                exc_type=type(exc).__name__,
                # Truncate so a noisy traceback doesn't blow up the
                # structured-log payload size.
                exc_message=str(exc)[:200],
                next_delay_seconds=round(
                    _compute_delay(
                        attempt + 1,
                        base_delay_seconds=base_delay_seconds,
                        max_delay_seconds=max_delay_seconds,
                        jitter=False,
                        rng=rng,
                    ),
                    3,
                ),
            )
    # Defensive: the loop above either returns or re-raises; this
    # branch should be unreachable. Pyright still wants a terminal
    # statement. Coverage is excluded because the loop's structure
    # (validated ``max_attempts >= 1`` plus the unconditional
    # ``raise`` on the final attempt) makes this branch unreachable
    # at runtime, but every dynamic-shape exit-flow analyser still
    # demands a terminal statement here.
    if last_exc is not None:  # pragma: no cover - unreachable defensive guard
        raise last_exc
    raise RuntimeError(  # pragma: no cover - unreachable defensive guard
        "with_backoff exited without producing a result or exception",
    )


__all__ = [
    "RetryPolicy",
    "with_backoff",
    "_DEFAULT_POLICY",
]
