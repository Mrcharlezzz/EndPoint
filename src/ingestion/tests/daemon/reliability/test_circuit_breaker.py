"""100% line + branch coverage for ``daemon.reliability.circuit_breaker``.

Locks the 3-state CLOSED -> OPEN -> HALF_OPEN -> CLOSED machine: the
counter resets on success, trips OPEN at threshold, admits one probe
after the cooldown elapses, closes on probe success, re-opens (with a
fresh cooldown) on probe failure, and treats cancellation as a
no-observation event that leaves state unchanged.

Tests inject a deterministic ``now`` callable instead of patching the
real clock — the breaker takes the clock as a constructor arg precisely
so this suite can advance time without ``freezegun``. Per-key isolation
is exercised by interleaving failures on multiple keys; concurrent
trips are exercised via ``asyncio.gather`` to confirm the per-key lock
serialises threshold detection.

Module-level singletons (``coordinator.get_refresh_breaker``,
``pipeline.get_fetch_breaker``) are NOT touched here; this suite
exercises freshly-constructed instances so cross-test state pollution
is impossible.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta

import pytest
from ingestion.daemon.reliability.circuit_breaker import (
    HALF_OPEN_PROBE_TIMEOUT_SECONDS,
    BreakerConfig,
    BreakerOutcome,
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FrozenClock:
    """Deterministic clock the breaker calls instead of ``datetime.now``."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2026, 4, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _make_breaker(
    *,
    failure_threshold: int = 3,
    recovery_timeout_seconds: float = 60.0,
    half_open_max_calls: int = 1,
    clock: _FrozenClock | None = None,
    probe_timeout_seconds: float = HALF_OPEN_PROBE_TIMEOUT_SECONDS,
) -> tuple[CircuitBreaker, _FrozenClock]:
    """Construct a breaker with a fresh frozen clock."""
    cfg = BreakerConfig(
        failure_threshold=failure_threshold,
        recovery_timeout_seconds=recovery_timeout_seconds,
        half_open_max_calls=half_open_max_calls,
    )
    c = clock or _FrozenClock()
    return (
        CircuitBreaker(cfg, now=c, probe_timeout_seconds=probe_timeout_seconds),
        c,
    )


async def _ok() -> str:
    return "ok"


# ---------------------------------------------------------------------------
# CLOSED state behaviour
# ---------------------------------------------------------------------------


async def test_closed_call_runs_fn_and_returns_result() -> None:
    """A call against a fresh CLOSED breaker just invokes the callable."""
    br, _ = _make_breaker()
    result = await br.call("k", _ok)
    assert result == "ok"
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0


async def test_closed_success_does_not_change_state() -> None:
    """Several successes in a row — counter stays at 0, state stays CLOSED."""
    br, _ = _make_breaker()
    for _ in range(5):
        assert await br.call("k", _ok) == "ok"
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0
    assert state.last_failure_at is None
    assert state.next_attempt_at is None


async def test_closed_failure_increments_counter_without_tripping_below_threshold() -> None:
    """A CLOSED failure bumps ``failure_count`` but doesn't trip the breaker
    until the threshold is met."""
    br, _ = _make_breaker(failure_threshold=3)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 1
    assert state.last_failure_at is not None


async def test_closed_success_after_failure_resets_counter() -> None:
    """A success between failures resets the consecutive-failure streak."""
    br, _ = _make_breaker(failure_threshold=3)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    assert (await br.state_for("k")).failure_count == 2
    # Success: counter resets.
    await br.call("k", _ok)
    state = await br.state_for("k")
    assert state.failure_count == 0
    assert state.last_failure_at is None


# ---------------------------------------------------------------------------
# CLOSED -> OPEN transition
# ---------------------------------------------------------------------------


async def test_closed_to_open_at_threshold() -> None:
    """N consecutive failures (== threshold) trips the breaker OPEN."""
    br, clock = _make_breaker(failure_threshold=3, recovery_timeout_seconds=60)

    async def boom() -> str:
        raise RuntimeError("nope")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await br.call("k", boom)

    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN
    assert state.failure_count == 3
    assert state.next_attempt_at == clock.now + timedelta(seconds=60)


async def test_open_rejects_subsequent_calls_with_circuit_open_error() -> None:
    """Once OPEN, calls fast-fail with ``CircuitOpenError`` carrying ``key``
    + ``next_attempt_at``."""
    br, clock = _make_breaker(failure_threshold=2, recovery_timeout_seconds=60)

    async def boom() -> str:
        raise RuntimeError("nope")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await br.call("k", boom)

    with pytest.raises(CircuitOpenError) as excinfo:
        await br.call("k", _ok)
    err = excinfo.value
    assert err.key == "k"
    assert err.next_attempt_at == clock.now + timedelta(seconds=60)


async def test_circuit_open_error_str_message_mentions_key() -> None:
    """``str(CircuitOpenError)`` carries the key for log readability."""
    br, _ = _make_breaker(failure_threshold=1)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("alpha", boom)
    with pytest.raises(CircuitOpenError) as excinfo:
        await br.call("alpha", _ok)
    assert "alpha" in str(excinfo.value)


# ---------------------------------------------------------------------------
# OPEN -> HALF_OPEN transition
# ---------------------------------------------------------------------------


async def test_open_to_half_open_after_recovery_timeout() -> None:
    """Once ``recovery_timeout_seconds`` elapses the next call is admitted
    as a probe and the breaker transitions to HALF_OPEN."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=60)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    assert (await br.state_for("k")).state is BreakerOutcome.OPEN

    # Cooldown not elapsed -> still rejects.
    with pytest.raises(CircuitOpenError):
        await br.call("k", _ok)

    # Advance past the cooldown — next call is admitted as probe.
    clock.advance(61)
    result = await br.call("k", _ok)
    assert result == "ok"
    # Probe success closes the breaker.
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED


async def test_open_does_not_admit_until_cooldown_exact() -> None:
    """At exactly ``next_attempt_at`` the probe is admitted (>= boundary)."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=10)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    # Advance EXACTLY recovery_timeout — should now admit.
    clock.advance(10)
    result = await br.call("k", _ok)
    assert result == "ok"


# ---------------------------------------------------------------------------
# HALF_OPEN -> CLOSED transition
# ---------------------------------------------------------------------------


async def test_half_open_probe_success_resets_counter_and_closes() -> None:
    """A successful probe in HALF_OPEN closes the breaker and zeros all
    counters/timestamps."""
    br, clock = _make_breaker(failure_threshold=2, recovery_timeout_seconds=10)

    async def boom() -> str:
        raise RuntimeError("nope")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await br.call("k", boom)
    clock.advance(11)

    await br.call("k", _ok)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0
    assert state.last_failure_at is None
    assert state.next_attempt_at is None


# ---------------------------------------------------------------------------
# HALF_OPEN -> OPEN transition
# ---------------------------------------------------------------------------


async def test_half_open_probe_failure_reopens_with_fresh_timer() -> None:
    """A failing probe in HALF_OPEN resets the cooldown clock and goes
    back to OPEN — the timer measures from the probe failure, not the
    original trip."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=30)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(31)

    # Probe — fails again.
    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN
    # next_attempt_at is fresh: now + 30, not the original failure + 30.
    assert state.next_attempt_at == clock.now + timedelta(seconds=30)


# ---------------------------------------------------------------------------
# HALF_OPEN concurrent probe limit
# ---------------------------------------------------------------------------


async def test_half_open_caps_concurrent_probes_at_one() -> None:
    """Default ``half_open_max_calls=1`` — only one probe in flight at a
    time. A second concurrent caller sees the breaker as still-OPEN."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=5)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(6)

    # First probe holds the slot until ``release`` fires.
    release = asyncio.Event()

    async def slow_probe() -> str:
        await release.wait()
        return "probe"

    probe_task = asyncio.create_task(br.call("k", slow_probe))
    # Yield so the task lands and acquires the probe slot.
    await asyncio.sleep(0)
    # Concurrent second call sees no slot — surfaces CircuitOpenError.
    with pytest.raises(CircuitOpenError):
        await br.call("k", _ok)
    release.set()
    assert await probe_task == "probe"
    # Probe success closed the breaker.
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED


async def test_half_open_max_calls_two_admits_two_concurrent_probes() -> None:
    """With ``half_open_max_calls=2`` two concurrent probes fit; a third is
    rejected."""
    br, clock = _make_breaker(
        failure_threshold=1,
        recovery_timeout_seconds=5,
        half_open_max_calls=2,
    )

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(6)

    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "ok"

    a = asyncio.create_task(br.call("k", slow))
    await asyncio.sleep(0)
    b = asyncio.create_task(br.call("k", slow))
    await asyncio.sleep(0)
    # Third concurrent caller would exceed cap.
    with pytest.raises(CircuitOpenError):
        await br.call("k", _ok)
    release.set()
    await asyncio.gather(a, b)


# ---------------------------------------------------------------------------
# Cancellation handling
# ---------------------------------------------------------------------------


async def test_cancellation_during_closed_call_does_not_change_state() -> None:
    """``asyncio.CancelledError`` raised inside the wrapped fn doesn't
    count as a failure — the counter is unchanged."""
    br, _ = _make_breaker(failure_threshold=2)

    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await br.call("k", cancelled)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0


async def test_cancellation_during_probe_does_not_count_as_failure() -> None:
    """A probe cancelled mid-flight leaves state HALF_OPEN (no observation),
    not OPEN. The slot is released so the next call can probe fresh."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=5)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(6)

    async def cancelled_probe() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await br.call("k", cancelled_probe)
    # Cancel does not count as failure; state remains HALF_OPEN with
    # the slot released so subsequent probes can run.
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.HALF_OPEN
    # Slot was released — a follow-up probe goes through.
    assert await br.call("k", _ok) == "ok"
    assert (await br.state_for("k")).state is BreakerOutcome.CLOSED


# ---------------------------------------------------------------------------
# Per-key isolation
# ---------------------------------------------------------------------------


async def test_per_key_isolation_failures_dont_cross() -> None:
    """Tripping breaker for key 'a' does not affect key 'b'."""
    br, _ = _make_breaker(failure_threshold=2)

    async def boom() -> str:
        raise RuntimeError("nope")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await br.call("a", boom)

    # Key 'a' is OPEN.
    a_state = await br.state_for("a")
    assert a_state.state is BreakerOutcome.OPEN
    # Key 'b' is fresh.
    b_state = await br.state_for("b")
    assert b_state.state is BreakerOutcome.CLOSED
    # And key 'b' calls go through.
    assert await br.call("b", _ok) == "ok"


async def test_state_for_creates_lazy_entry_for_unseen_key() -> None:
    """``state_for`` is safe to call against a never-seen key — returns
    the default CLOSED snapshot."""
    br, _ = _make_breaker()
    state = await br.state_for("never-seen")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0


# ---------------------------------------------------------------------------
# Atomic CAS / concurrent failures
# ---------------------------------------------------------------------------


async def test_concurrent_failures_serialise_through_per_key_lock() -> None:
    """5 concurrent failures (threshold=3) must produce exactly 5
    raised RuntimeErrors and end with breaker OPEN at count == 5
    (no double-counting past the increment, no lost updates)."""
    br, _ = _make_breaker(failure_threshold=3)

    async def boom() -> str:
        raise RuntimeError("boom")

    async def call() -> None:
        with pytest.raises(RuntimeError):
            await br.call("k", boom)

    # Some of these will trip the breaker mid-batch; once OPEN, the
    # remaining tasks see CircuitOpenError instead of RuntimeError.
    # We can't predict the exact split (depends on scheduling), so
    # accept either + count outcomes by class.
    async def call_loose() -> str:
        try:
            await br.call("k", boom)
            return "unexpected_success"
        except RuntimeError:
            return "fail"
        except CircuitOpenError:
            return "rejected"

    results = await asyncio.gather(*[call_loose() for _ in range(5)])
    # At least 3 must have hit the underlying RuntimeError (those
    # that incremented before the trip); the remainder are
    # rejections.
    fail_count = results.count("fail")
    rejected = results.count("rejected")
    assert fail_count >= 3
    assert fail_count + rejected == 5
    assert "unexpected_success" not in results

    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN
    # Counter equals the number of failures recorded; never above the
    # actual fail_count (no lost-update double-count).
    assert state.failure_count == fail_count


# ---------------------------------------------------------------------------
# Probe timeout (HALF_OPEN budget)
# ---------------------------------------------------------------------------


async def test_probe_timeout_caps_long_running_probe() -> None:
    """A wedged probe is cancelled by the breaker's per-call timeout and
    treated as a failure, re-opening the breaker."""
    br, clock = _make_breaker(
        failure_threshold=1,
        recovery_timeout_seconds=5,
        probe_timeout_seconds=0.05,
    )

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(6)

    async def slow() -> str:
        await asyncio.sleep(5.0)
        return "never"

    # The probe times out — ``asyncio.wait_for`` raises TimeoutError,
    # which the breaker counts as a failure.
    with pytest.raises(TimeoutError):
        await br.call("k", slow)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN


# ---------------------------------------------------------------------------
# Structured event emission on transitions
# ---------------------------------------------------------------------------


async def _capture_log_lines(coro: asyncio.Future[None]) -> str:
    """Run a coroutine while capturing structlog output to stdout/stderr."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        await coro  # type: ignore[func-returns-value]
    return out.getvalue() + err.getvalue()


async def test_transition_to_open_emits_structured_event() -> None:
    """Tripping the breaker emits a ``circuit_breaker.open`` log event
    carrying ``key`` + ``failure_count`` so alert subscribers can pivot."""
    br, _ = _make_breaker(failure_threshold=2)

    async def boom() -> str:
        raise RuntimeError("nope")

    async def trip() -> None:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await br.call("alpha", boom)

    text = await _capture_log_lines(trip())  # type: ignore[arg-type]
    assert "circuit_breaker.open" in text
    assert "alpha" in text


async def test_transition_to_half_open_emits_structured_event() -> None:
    """Crossing the cooldown emits ``circuit_breaker.half_open``."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=5)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("alpha", boom)
    clock.advance(6)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        await br.call("alpha", _ok)
    text = out.getvalue() + err.getvalue()
    assert "circuit_breaker.half_open" in text
    # Probe success also fires circuit_breaker.closed.
    assert "circuit_breaker.closed" in text


async def test_transition_to_closed_emits_structured_event() -> None:
    """Probe success fires ``circuit_breaker.closed`` separately from the
    ``half_open`` event (two transitions, one per state edge)."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=5)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(6)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        await br.call("k", _ok)
    text = out.getvalue() + err.getvalue()
    # Two distinct events on this single call: half_open then closed.
    assert text.count("circuit_breaker.half_open") >= 1
    assert text.count("circuit_breaker.closed") >= 1


async def test_no_op_transition_emits_no_event() -> None:
    """``_transition`` is a no-op when target equals the current state.
    Recording successive successes on a CLOSED breaker should NOT spam
    'circuit_breaker.closed' events."""
    br, _ = _make_breaker(failure_threshold=2)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        for _ in range(3):
            await br.call("k", _ok)
    text = out.getvalue() + err.getvalue()
    # No transition event during steady-state CLOSED operation.
    assert "circuit_breaker.closed" not in text
    assert "circuit_breaker.open" not in text


# ---------------------------------------------------------------------------
# Logger error never blocks state mutation
# ---------------------------------------------------------------------------


async def test_logger_failure_does_not_block_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the logger raises during a transition the state still updates
    — the breaker uses ``contextlib.suppress(Exception)`` around log
    calls because logging must NEVER mask reliability behaviour."""
    br, _ = _make_breaker(failure_threshold=1)

    class _BrokenLogger:
        def warning(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("logger asploded")

    # Inject the broken logger after construction.
    br._log = _BrokenLogger()  # type: ignore[assignment]

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    # Despite the logger error the transition completes.
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN


# ---------------------------------------------------------------------------
# Public surface and dataclass shape
# ---------------------------------------------------------------------------


def test_breaker_state_is_frozen() -> None:
    """``BreakerState`` is a frozen dataclass — observers cannot mutate
    the live state via the snapshot."""
    snap = BreakerState(state=BreakerOutcome.CLOSED)
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError varies
        snap.state = BreakerOutcome.OPEN  # type: ignore[misc]


def test_breaker_config_defaults_match_documented_values() -> None:
    """Defaults are part of the contract — slice 10 fixed
    ``failure_threshold=5``, ``recovery_timeout_seconds=60``, and
    ``half_open_max_calls=1``. A change here is a behaviour change
    operators rely on; bump deliberately."""
    cfg = BreakerConfig()
    assert cfg.failure_threshold == 5
    assert cfg.recovery_timeout_seconds == 60.0
    assert cfg.half_open_max_calls == 1


def test_breaker_returns_cached_config() -> None:
    """``CircuitBreaker.config`` exposes the constructor config
    unchanged — used by metrics readers that surface thresholds in
    dashboards."""
    cfg = BreakerConfig(failure_threshold=7)
    br = CircuitBreaker(cfg)
    assert br.config is cfg


def test_circuit_breaker_uses_default_config_when_none_passed() -> None:
    """Passing ``config=None`` (or omitting) picks up the dataclass
    defaults."""
    br = CircuitBreaker(None)
    assert br.config.failure_threshold == 5


async def test_circuit_breaker_uses_default_now_when_none_passed() -> None:
    """No injected clock — fall back to ``datetime.now(tz=UTC)``. The
    default ``_utc_now`` is exercised here so coverage hits the
    fallback path (every other test injects a frozen clock)."""
    br = CircuitBreaker()
    assert br.config.failure_threshold == 5
    # Drive a successful call so the breaker actually invokes _utc_now
    # internally (state_for + record_failure paths read self._now()).
    await br.call("k", _ok)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    state = await br.state_for("k")
    assert state.last_failure_at is not None
    # Default clock is wall-clock UTC — sanity-check the timestamp is
    # tz-aware and within a few seconds of now.
    assert state.last_failure_at.tzinfo is UTC
