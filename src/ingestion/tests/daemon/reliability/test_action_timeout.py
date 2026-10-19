"""100% line + branch coverage for ``daemon.api.action_timeout``.

Locks the per-action timeout helper:

* a fast coroutine returns its value within budget;
* a slow coroutine is cancelled cleanly and raises
  :class:`TransientFailureError` carrying the reason payload;
* a non-positive ``timeout_seconds`` disables the cap (operator
  opt-out, documented in the helper's docstring);
* the underlying coroutine is cancelled (cleanup callbacks fire) when
  the timeout expires — important so the service-layer connection
  pool reclaims the slot;
* the raised :class:`TransientFailureError` chains the original
  :class:`asyncio.TimeoutError` via ``__cause__`` so the audit log can
  pivot back to the timing event.
"""

from __future__ import annotations

import asyncio

import pytest
from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.services.exceptions import TransientFailureError

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_fast_coroutine_completes_normally() -> None:
    """A coroutine that returns within the budget yields its value."""

    async def fast() -> str:
        return "done"

    result = await with_action_timeout(0.5, fast())
    assert result == "done"


async def test_default_action_timeout_30s_admits_quick_fns() -> None:
    """The helper takes the timeout as a positional arg; production
    callers pass ``ctx.config.api.action_timeout_seconds`` (default 30
    in slice 10). A trivial coroutine completes far below 30s."""

    async def quick() -> int:
        await asyncio.sleep(0)
        return 42

    result = await with_action_timeout(30, quick())
    assert result == 42


# ---------------------------------------------------------------------------
# Timeout firing
# ---------------------------------------------------------------------------


async def test_slow_coroutine_raises_transient_failure() -> None:
    """A coroutine that exceeds the budget raises
    :class:`TransientFailureError` with ``operation_timeout`` reason."""

    async def slow() -> None:
        await asyncio.sleep(0.5)

    with pytest.raises(TransientFailureError) as excinfo:
        await with_action_timeout(0.05, slow())
    err = excinfo.value
    assert isinstance(err.detail, dict)
    assert err.detail["reason"] == "operation_timeout"
    assert err.detail["timeout_seconds"] == 0.05


async def test_timeout_chains_underlying_timeouterror_as_cause() -> None:
    """The raised :class:`TransientFailureError` chains the underlying
    :class:`TimeoutError` via ``__cause__`` so logs can pivot back to
    the timing event without swallowing context."""

    async def slow() -> None:
        await asyncio.sleep(0.5)

    with pytest.raises(TransientFailureError) as excinfo:
        await with_action_timeout(0.01, slow())
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_timeout_cancels_underlying_coroutine() -> None:
    """When the timeout fires the wrapped coroutine is cancelled —
    ``finally`` blocks run so the service-layer can release held
    connections."""
    cleanup_ran = False

    async def slow() -> None:
        nonlocal cleanup_ran
        try:
            await asyncio.sleep(1.0)
        finally:
            cleanup_ran = True

    with pytest.raises(TransientFailureError):
        await with_action_timeout(0.02, slow())
    assert cleanup_ran is True


# ---------------------------------------------------------------------------
# Disabled timeout
# ---------------------------------------------------------------------------


async def test_zero_timeout_disables_cap() -> None:
    """``timeout_seconds=0`` is documented as "no timeout" — the
    coroutine runs without a wrapping ``wait_for``."""

    async def quick() -> str:
        return "ok"

    result = await with_action_timeout(0, quick())
    assert result == "ok"


async def test_negative_timeout_disables_cap() -> None:
    """``timeout_seconds < 0`` shares the same opt-out path."""

    async def quick() -> str:
        return "ok"

    result = await with_action_timeout(-1, quick())
    assert result == "ok"


async def test_disabled_timeout_does_not_cancel_long_coroutines() -> None:
    """When the cap is disabled, a coroutine that would have timed out
    runs to completion."""

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    result = await with_action_timeout(0, slow())
    assert result == "ok"


# ---------------------------------------------------------------------------
# Exception passthrough
# ---------------------------------------------------------------------------


async def test_underlying_exception_propagates_unchanged() -> None:
    """An exception raised by the wrapped coroutine surfaces as-is —
    the helper only intercepts ``asyncio.TimeoutError``."""

    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await with_action_timeout(1.0, boom())


async def test_cancellederror_in_wrapped_coroutine_propagates() -> None:
    """``asyncio.CancelledError`` raised from inside the wrapped
    coroutine is NOT translated to TransientFailureError. The helper
    only catches the timeout; cancellation by the caller surfaces
    cleanly so worker-pool emergency-stop semantics still hold."""

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_action_timeout(1.0, cancelled())
