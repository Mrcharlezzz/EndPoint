"""100% line + branch coverage for ``daemon.reliability.backoff``.

Locks the retry-with-backoff helper:

* attempt budget is honoured (``max_attempts`` failures -> last
  exception re-raised);
* invalid budgets reject up-front (``ValueError``);
* the geometric progression with cap is correct when jitter is off;
* jitter spreads delays across ``[0, computed]`` when on;
* the ``retryable`` predicate short-circuits on non-retryable errors;
* ``asyncio.CancelledError`` propagates immediately without consuming
  the budget;
* an injected ``sleep`` callable is awaited and a cancellation during
  the sleep raises through the call stack;
* the ``rng`` parameter is honoured for repeatable jitter sampling.

Tests inject a stub ``sleep`` that records its argument so the suite
runs in milliseconds even with multi-attempt scenarios. The real
``asyncio.sleep`` is only exercised in one cancellation test where a
short timeout is acceptable.
"""

from __future__ import annotations

import asyncio
import random
import statistics

import pytest
from ingestion.daemon.reliability.backoff import RetryPolicy, with_backoff

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _SleepRecorder:
    """Replacement for ``asyncio.sleep`` that records every duration."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_succeeds_after_two_failures() -> None:
    """Fails twice, succeeds on the third — returns the success value
    and invokes ``fn`` exactly three times."""
    sleeper = _SleepRecorder()
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("nope")
        return "ok"

    result = await with_backoff(fn, max_attempts=3, jitter=False, sleep=sleeper)
    assert result == "ok"
    assert attempts == 3
    # Two retries -> two sleeps.
    assert len(sleeper.calls) == 2


async def test_succeeds_on_first_attempt_no_sleep() -> None:
    """No retry loop when the first call succeeds — sleep never fires."""
    sleeper = _SleepRecorder()

    async def fn() -> str:
        return "ok"

    result = await with_backoff(fn, sleep=sleeper)
    assert result == "ok"
    assert sleeper.calls == []


# ---------------------------------------------------------------------------
# max_attempts edge cases
# ---------------------------------------------------------------------------


async def test_max_attempts_one_means_no_retry() -> None:
    """``max_attempts=1`` invokes the function exactly once and propagates
    the original exception unchanged on failure."""
    sleeper = _SleepRecorder()
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("first-and-only")

    with pytest.raises(RuntimeError, match="first-and-only"):
        await with_backoff(fn, max_attempts=1, sleep=sleeper)
    assert attempts == 1
    assert sleeper.calls == []


async def test_max_attempts_zero_raises_value_error_up_front() -> None:
    """A non-positive budget is caller misuse — fail fast with
    ``ValueError`` before ``fn`` is ever invoked."""
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        return "ok"

    with pytest.raises(ValueError, match="max_attempts"):
        await with_backoff(fn, max_attempts=0)
    assert attempts == 0


async def test_negative_max_attempts_raises_value_error() -> None:
    """Negative budgets share the same up-front rejection."""

    async def fn() -> str:
        return "ok"

    with pytest.raises(ValueError, match="max_attempts"):
        await with_backoff(fn, max_attempts=-1)


# ---------------------------------------------------------------------------
# Final exception propagation
# ---------------------------------------------------------------------------


async def test_exhausted_budget_re_raises_last_exception() -> None:
    """After ``max_attempts`` failures the ORIGINAL last exception
    re-raises — not a synthetic wrapper. Callers branch on the
    underlying type."""
    sleeper = _SleepRecorder()

    class _PinError(RuntimeError):
        pass

    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        raise _PinError(f"attempt {attempts}")

    with pytest.raises(_PinError, match="attempt 3"):
        await with_backoff(fn, max_attempts=3, jitter=False, sleep=sleeper)
    assert attempts == 3
    assert len(sleeper.calls) == 2


# ---------------------------------------------------------------------------
# Geometric progression and cap
# ---------------------------------------------------------------------------


async def test_no_jitter_geometric_progression() -> None:
    """With jitter off, sleep durations follow base * 2**(attempt-1)
    capped by ``max_delay_seconds``."""
    sleeper = _SleepRecorder()

    async def fn() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await with_backoff(
            fn,
            max_attempts=5,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
            jitter=False,
            sleep=sleeper,
        )
    # 4 retry-sleeps after attempts 1-4: delays = 1, 2, 4, 8.
    assert sleeper.calls == [1.0, 2.0, 4.0, 8.0]


async def test_max_delay_caps_long_progression() -> None:
    """With base=1 and max=5, attempts past 4 stay at 5 — the cap
    prevents multi-hour waits at large attempt counts."""
    sleeper = _SleepRecorder()

    async def fn() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await with_backoff(
            fn,
            max_attempts=10,
            base_delay_seconds=1.0,
            max_delay_seconds=5.0,
            jitter=False,
            sleep=sleeper,
        )
    # 9 retry sleeps. After attempt 4 onwards every delay is 5.
    assert all(c <= 5.0 for c in sleeper.calls)
    # Final tail should saturate at the cap.
    assert sleeper.calls[-1] == 5.0


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------


async def test_jitter_on_produces_variable_delays() -> None:
    """With jitter on, delays vary across runs even with the same
    inputs (uniform sample over [0, computed_delay])."""
    rng = random.Random(42)  # noqa: S311 — test-only seeded RNG
    sleeper = _SleepRecorder()

    async def fn() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await with_backoff(
            fn,
            max_attempts=10,
            base_delay_seconds=2.0,
            max_delay_seconds=8.0,
            jitter=True,
            sleep=sleeper,
            rng=rng,
        )
    # Jitter samples on [0, computed]; not all values can be identical.
    assert len(sleeper.calls) == 9
    assert len(set(sleeper.calls)) > 1
    # Every value bounded by max_delay.
    assert all(0.0 <= c <= 8.0 for c in sleeper.calls)


async def test_jitter_seeded_rng_is_repeatable() -> None:
    """Two runs with the same seed produce identical delay sequences —
    the helper's RNG is the only source of randomness."""

    async def boom() -> str:
        raise RuntimeError("nope")

    captured: list[list[float]] = []
    for _ in range(2):
        sleeper = _SleepRecorder()
        rng = random.Random(99)  # noqa: S311 — test-only seeded RNG
        with pytest.raises(RuntimeError):
            await with_backoff(
                boom,
                max_attempts=5,
                base_delay_seconds=1.0,
                max_delay_seconds=8.0,
                jitter=True,
                sleep=sleeper,
                rng=rng,
            )
        captured.append(list(sleeper.calls))
    assert captured[0] == captured[1]


async def test_jitter_distribution_mean_within_expected_range() -> None:
    """100 sampled jitter delays at base=4/max=4 (cap-saturated): the
    mean lands near 2.0 (uniform[0,4] expectation)."""
    rng = random.Random(2026)  # noqa: S311 — test-only seeded RNG
    samples: list[float] = []

    async def fn() -> str:
        raise RuntimeError("nope")

    sleeper = _SleepRecorder()
    # 101 attempts -> 100 sleep samples. base==max ensures delay
    # always saturates to 4 before jitter; jitter is U(0,4) -> mean ~2.
    with pytest.raises(RuntimeError):
        await with_backoff(
            fn,
            max_attempts=101,
            base_delay_seconds=4.0,
            max_delay_seconds=4.0,
            jitter=True,
            sleep=sleeper,
            rng=rng,
        )
    samples = sleeper.calls
    assert len(samples) == 100
    mean = statistics.mean(samples)
    # Expected mean = 2.0 (uniform[0,4]); allow generous slack for
    # 100-sample variance.
    assert 1.5 < mean < 2.5


# ---------------------------------------------------------------------------
# retryable predicate
# ---------------------------------------------------------------------------


async def test_retryable_false_short_circuits() -> None:
    """When the predicate returns False, the original exception
    re-raises immediately and the rest of the budget is discarded."""
    sleeper = _SleepRecorder()
    attempts = 0

    class _NonRetryableError(Exception):
        pass

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        raise _NonRetryableError("nope")

    def is_retryable(exc: BaseException) -> bool:
        return not isinstance(exc, _NonRetryableError)

    with pytest.raises(_NonRetryableError):
        await with_backoff(
            fn,
            max_attempts=5,
            jitter=False,
            retryable=is_retryable,
            sleep=sleeper,
        )
    # Predicate said no -> exactly one attempt.
    assert attempts == 1
    assert sleeper.calls == []


async def test_retryable_true_consumes_budget() -> None:
    """A predicate that returns True is equivalent to no predicate."""
    sleeper = _SleepRecorder()
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await with_backoff(
            fn,
            max_attempts=3,
            jitter=False,
            retryable=lambda _exc: True,
            sleep=sleeper,
        )
    assert attempts == 3


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancelled_error_inside_fn_propagates_immediately() -> None:
    """``asyncio.CancelledError`` from ``fn`` propagates without
    consuming the retry budget."""
    sleeper = _SleepRecorder()
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_backoff(fn, max_attempts=5, jitter=False, sleep=sleeper)
    assert attempts == 1
    assert sleeper.calls == []


async def test_cancelled_error_inside_sleep_propagates() -> None:
    """A cancel during the backoff sleep aborts the retry loop. The
    inner ``try``/``except CancelledError: raise`` wraps the sleep."""

    async def cancel_sleep(_sec: float) -> None:
        raise asyncio.CancelledError

    async def fn() -> str:
        raise RuntimeError("nope")

    with pytest.raises(asyncio.CancelledError):
        await with_backoff(
            fn,
            max_attempts=3,
            jitter=False,
            sleep=cancel_sleep,
        )


async def test_real_sleep_is_cancellable_via_wait_for() -> None:
    """The default sleep is ``asyncio.sleep`` — that's cancellable
    cooperatively. Wrap a long-running retry in ``asyncio.wait_for``
    with a tight timeout and confirm CancelledError surfaces."""

    async def fn() -> str:
        raise RuntimeError("nope")

    # Use the real ``asyncio.sleep`` so the cancel actually fires
    # during the inner wait.
    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError, TimeoutError)):
        await asyncio.wait_for(
            with_backoff(
                fn,
                max_attempts=10,
                base_delay_seconds=10.0,
                max_delay_seconds=10.0,
                jitter=False,
            ),
            timeout=0.05,
        )


# ---------------------------------------------------------------------------
# Coroutine reuse pin
# ---------------------------------------------------------------------------


async def test_with_backoff_takes_factory_not_coroutine() -> None:
    """``with_backoff`` expects a callable that returns a fresh
    awaitable per attempt. Passing a single already-instantiated
    coroutine instance fails on the second attempt because Python
    refuses to re-await a consumed coroutine.

    Pin so anyone tempted to write ``with_backoff(fn(), ...)`` (with
    parens) gets a clear failure mode.
    """
    sleeper = _SleepRecorder()

    async def boom() -> str:
        raise RuntimeError("nope")

    coro_instance = boom()  # CALLED once; can't be re-awaited.

    # The helper expects a Callable[[], Awaitable[T]], so we wrap the
    # already-awaitable coroutine in a one-shot function. The bug
    # being pinned here is that the SECOND retry raises because
    # ``coro_instance`` was already awaited.
    async def reused_factory() -> str:
        return await coro_instance

    with pytest.raises((RuntimeError, asyncio.InvalidStateError)):
        await with_backoff(
            reused_factory,
            max_attempts=3,
            jitter=False,
            sleep=sleeper,
        )


# ---------------------------------------------------------------------------
# RetryPolicy dataclass shape
# ---------------------------------------------------------------------------


def test_retry_policy_defaults_match_documented_values() -> None:
    """Defaults are part of the contract: max_attempts=3, base=1.0,
    max=30.0, jitter=True."""
    p = RetryPolicy()
    assert p.max_attempts == 3
    assert p.base_delay_seconds == 1.0
    assert p.max_delay_seconds == 30.0
    assert p.jitter is True


def test_retry_policy_is_frozen() -> None:
    """``RetryPolicy`` is frozen so callers can pass it around without
    worrying about a downstream mutation."""
    p = RetryPolicy()
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError varies
        p.max_attempts = 99  # type: ignore[misc]
