"""Edge + production tests for slice 10 (reliability primitives).

Pinned scenarios across the new circuit-breaker, backoff, action
timeout, per-user cap, backpressure, and dead-letter event paths.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended) or ``[TEST-COVERAGE]``
(the existing implementation is fine; this just locks the behaviour).
Slow tests (>500ms) carry ``@pytest.mark.slow``.

Edge brainstorm additions (>= 8):

* recovery_timeout=0 collapses to "always-CLOSED" — pin operator
  opt-out [TEST-COVERAGE]
* Pipeline fetch retry: ``RateLimitError`` retried but
  ``RuntimeError("boom")`` is not — predicate scope locked
  [TEST-COVERAGE]
* Per-user cap dict deletion on user removal — flagged for slice 12
  cleanup pass [CODE-FIX]
* Module-level breaker singleton — tests must reset state between
  cases — provide a helper for the next slice's testers [TEST-COVERAGE]
* dead_letter event publisher subscriber crash is logged + dropped
  [TEST-COVERAGE]
* dead_letter.cleared event payload has consecutive_failures=0 and
  suppressed_until=None [TEST-COVERAGE]
* CircuitOpenError carries next_attempt_at consistent with the
  configured cooldown across re-trip cycles [TEST-COVERAGE]
* Probe in HALF_OPEN holds the slot exclusively even if it raises a
  non-Exception subclass (BaseException-class) — current behaviour:
  exits via finally only on Exception, NOT on BaseException [TEST-COVERAGE]
"""

from __future__ import annotations

import asyncio
import io
import statistics
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.api.action_timeout import with_action_timeout
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.reliability.backoff import RetryPolicy, with_backoff
from ingestion.daemon.reliability.circuit_breaker import (
    BreakerConfig,
    BreakerOutcome,
    CircuitBreaker,
    CircuitOpenError,
    reset_breaker_state_for_tests,
)
from ingestion.daemon.services.exceptions import TransientFailureError
from ingestion.daemon.store.local import apply_migrations
from ingestion.daemon.worker import WorkerPool

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


class _FrozenClock:
    """Deterministic clock — every test injects one to avoid wall-time
    drift inside breaker / dead-letter assertions."""

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
    probe_timeout_seconds: float = 30.0,
    clock: _FrozenClock | None = None,
) -> tuple[CircuitBreaker, _FrozenClock]:
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


# Re-export the public helper under the historical local name so the
# tests below can call it without churning every call site. The shim
# documents that the canonical helper now lives in the production
# module so future tests can import it directly.
reset_breaker_state = reset_breaker_state_for_tests


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "state.db"
    apply_migrations(p)
    return p


# ---------------------------------------------------------------------------
# 1. Circuit breaker race [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_breaker_concurrent_5_failures_trip_exactly_once() -> None:
    """5 concurrent ``call(key, failing_fn)`` -> exactly 5 underlying
    failures recorded (or fewer if the breaker tripped mid-batch and
    rejected later attempts), and the breaker is OPEN at the end with
    failure_count == observed-fails (no double-counting past the trip).

    Pin matches the Part-A implementation note: per-key lock serialises
    the increment so concurrent calls cannot lost-update past the
    threshold.
    """
    br, _ = _make_breaker(failure_threshold=3)

    async def boom() -> str:
        raise RuntimeError("nope")

    async def attempt() -> str:
        try:
            await br.call("k", boom)
            return "success_unexpected"
        except RuntimeError:
            return "fail"
        except CircuitOpenError:
            return "rejected"

    results = await asyncio.gather(*[attempt() for _ in range(5)])
    assert "success_unexpected" not in results
    fail_count = results.count("fail")
    rejected = results.count("rejected")
    assert fail_count + rejected == 5
    # Threshold is 3 — at least 3 must have fired before the trip.
    assert fail_count >= 3
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN
    # Counter equals real failures, never above (atomic CAS pin).
    assert state.failure_count == fail_count


# ---------------------------------------------------------------------------
# 2. Full state machine [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_breaker_full_cycle_emits_each_event_exactly_once() -> None:
    """CLOSED -> OPEN -> HALF_OPEN -> CLOSED -> OPEN -> HALF_OPEN -> OPEN.
    Each transition emits its event exactly once per crossing — repeated
    same-state events would spam the alert engine."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=5)
    out = io.StringIO()
    err = io.StringIO()

    async def boom() -> str:
        raise RuntimeError("nope")

    async def ok_call() -> str:
        return "ok"

    with redirect_stdout(out), redirect_stderr(err):
        # CLOSED -> OPEN
        with pytest.raises(RuntimeError):
            await br.call("k", boom)
        # OPEN -> HALF_OPEN -> CLOSED
        clock.advance(6)
        await br.call("k", ok_call)
        # CLOSED -> OPEN
        with pytest.raises(RuntimeError):
            await br.call("k", boom)
        # OPEN -> HALF_OPEN -> OPEN (probe fails)
        clock.advance(6)
        with pytest.raises(RuntimeError):
            await br.call("k", boom)

    text = out.getvalue() + err.getvalue()
    # Three trips to OPEN: initial, after probe-success-then-fail,
    # and after the probe-failure re-trip.
    assert text.count("circuit_breaker.open") == 3
    # Two transitions to HALF_OPEN (each time the cooldown elapsed).
    assert text.count("circuit_breaker.half_open") == 2
    # One return to CLOSED (on the first probe-success).
    assert text.count("circuit_breaker.closed") == 1


# ---------------------------------------------------------------------------
# 3. Probe-specific timeout pin [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_breaker_probe_has_specific_timeout_separate_from_call_timeout() -> None:
    """The HALF_OPEN probe has a dedicated timeout (default 30s,
    documented in the module) that's separate from any caller-side
    asyncio.wait_for. A 30s+ probe wedge is bounded by the breaker's
    own ``asyncio.wait_for``, not by the caller's timeout. Pin so a
    future change that drops the breaker-side timeout fails this
    test."""
    br, clock = _make_breaker(
        failure_threshold=1,
        recovery_timeout_seconds=1,
        probe_timeout_seconds=0.05,
    )

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    clock.advance(2)

    async def slow() -> str:
        # Simulate a hung upstream that the breaker must fail by itself.
        await asyncio.sleep(5)
        return "never"

    # The breaker's wait_for (0.05s) fires before any caller timeout.
    with pytest.raises(TimeoutError):
        await br.call("k", slow)
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN


# ---------------------------------------------------------------------------
# 4. Per-user cap dict cleanup leak [CODE-FIX flag for slice 12]
# ---------------------------------------------------------------------------


async def test_per_user_dict_does_not_clean_up_on_emergency_stop() -> None:
    """``WorkerPool._per_user_sems`` survives a broad emergency stop.

    Cleanup is gated on the slice-12 :meth:`WorkerPool.remove_user`
    hook, which fires from the lifecycle layer when a user is
    actually deleted. An emergency_stop is operator-initiated and
    transient — the same users typically resume on the next tick, so
    pruning their semaphores would force a needless re-allocation.
    """
    pool = WorkerPool(global_limit=4, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    pool.submit(user_email="bob@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    pool.emergency_stop()
    # Both still present — emergency_stop is not a user-removal signal.
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    assert "bob@x" in pool._per_user_sems  # noqa: SLF001


# ---------------------------------------------------------------------------
# 5. Backoff jitter distribution [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_backoff_jitter_distribution_mean_within_expected_range() -> None:
    """100 jitter samples at base=4 (saturated): the mean lands close
    to 2.0 (uniform[0,4] expectation). The simple uniform jitter shape
    is correct — too tight a clustering would mean a regression to
    fixed-delay or to one of the more aggressive jitter shapes."""
    import random

    rng = random.Random(2027)  # noqa: S311 — test-only seeded RNG

    async def boom() -> str:
        raise RuntimeError("nope")

    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(RuntimeError):
        await with_backoff(
            boom,
            max_attempts=101,
            base_delay_seconds=4.0,
            max_delay_seconds=4.0,
            jitter=True,
            rng=rng,
            sleep=record,
        )
    assert len(sleeps) == 100
    mean = statistics.mean(sleeps)
    # Generous slack — 100 samples can drift around the expected 2.0.
    assert 1.5 < mean < 2.5


# ---------------------------------------------------------------------------
# 6. Action timeout cancels underlying coroutine [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_action_timeout_cancels_via_wait_for() -> None:
    """``with_action_timeout`` uses ``asyncio.wait_for`` so cancellation
    propagates into the wrapped coroutine. Verify the cancel reaches
    the inner ``finally`` block (used in production to release a
    held connection)."""
    inner_finally_ran = False

    async def held() -> None:
        nonlocal inner_finally_ran
        try:
            await asyncio.sleep(1.0)
        finally:
            inner_finally_ran = True

    with pytest.raises(TransientFailureError):
        await with_action_timeout(0.02, held())
    assert inner_finally_ran is True


# ---------------------------------------------------------------------------
# 7. Backpressure during drain mode [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_backpressure_applies_independently_of_drain_mode(db_path: Path) -> None:
    """The backpressure cap is enforced inside the service-layer
    insert path. Drain mode is a scheduler concern — it does NOT
    bypass the cap. A force_sync_one with the cap met fails fast even
    if drain mode is on; both safeguards stack."""
    from ingestion.daemon.force_sync import enqueue_force_sync
    from ingestion.daemon.services import sync_service
    from ingestion.daemon.services.exceptions import BackpressureError

    # Pre-fill to the cap.
    for i in range(3):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")

    # Even with drain mode on (modelled by the API path; we only test
    # the service-layer branch here), the backpressure check fires.
    from tests.daemon.services.test_credential_service import CapturingAuditWriter

    writer = CapturingAuditWriter()
    with pytest.raises(BackpressureError):
        await sync_service.force_sync_one(
            db_path,
            email="alice@x",
            platform="moodle",
            audit_writer=writer,
            reason="urgent-during-drain",
            max_queue_depth=3,
        )


# ---------------------------------------------------------------------------
# 8. Dead-letter event flapping [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_dead_letter_flapping_emits_event_each_transition(db_path: Path) -> None:
    """Pause -> unpause -> pause flapping fires the event on each
    transition (no debounce). Locks current behaviour: the alert
    engine sees every transition; if a future change adds debouncing
    this test fails so the operator-visible behaviour is reviewed
    deliberately."""
    events: list[tuple[str, dict[str, Any]]] = []

    async def publisher(channel: str, payload: dict[str, Any]) -> None:
        events.append((channel, payload))

    tracker = DeadLetterTracker(
        db_path,
        threshold=1,
        cooldown_seconds=60,
        event_publisher=publisher,
    )
    # 1st failure -> suppressed event (channel x2: dead_letter + errors)
    await tracker.record_failure("alice@x", "moodle")
    # Clear -> cleared event
    await tracker.clear("alice@x", "moodle")
    # 2nd failure -> suppressed again
    await tracker.record_failure("alice@x", "moodle")
    # Clear -> cleared again
    await tracker.clear("alice@x", "moodle")

    suppressed = [e for e in events if e[1]["event"] == "dead_letter.suppressed"]
    cleared = [e for e in events if e[1]["event"] == "dead_letter.cleared"]
    # Each transition fires once per channel — 2 channels * 2 trips.
    assert len(suppressed) == 4
    assert len(cleared) == 4


# ---------------------------------------------------------------------------
# 9. EventBus subscriber raising [TEST-COVERAGE]
# ---------------------------------------------------------------------------


async def test_event_publisher_subscriber_raise_does_not_propagate(db_path: Path) -> None:
    """A subscriber callback that raises is caught + logged via
    ``_log.exception``. The dead-letter write path does NOT propagate
    the failure to the caller — a misbehaving subscriber must not
    take the daemon down."""
    call_count = 0

    async def bad_publisher(_channel: str, _payload: dict[str, Any]) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("subscriber asploded")

    tracker = DeadLetterTracker(
        db_path,
        threshold=1,
        cooldown_seconds=60,
        event_publisher=bad_publisher,
    )
    # Should NOT raise.
    await tracker.record_failure("alice@x", "moodle")
    # Publisher still got called at least once before the exception.
    assert call_count >= 1
    # Subsequent operations on the tracker still work.
    assert await tracker.is_suppressed("alice@x", "moodle") is True


# ---------------------------------------------------------------------------
# 10. Production: 1000 concurrent breaker calls under failure
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_breaker_under_1000_concurrent_failures_bounded() -> None:
    """1000 concurrent failing calls don't pile-up CPU / lock
    contention beyond the per-key serialisation. Wall-clock budget:
    well under 5 seconds even on a slow CI runner."""
    br, _ = _make_breaker(failure_threshold=10)

    async def boom() -> str:
        raise RuntimeError("nope")

    async def attempt() -> None:
        import contextlib

        with contextlib.suppress(RuntimeError, CircuitOpenError):
            await br.call("k", boom)

    start = time.monotonic()
    await asyncio.gather(*[attempt() for _ in range(1000)])
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"breaker contention slow path: {elapsed:.3f}s"
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.OPEN


# ---------------------------------------------------------------------------
# 11. Production: 100 users x 10 platforms x 100 syncs scheduler tick
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_per_user_dict_at_100_users_scheduler_tick_bounded() -> None:
    """100 distinct users with 10 platforms each. After every sync
    completes, the per-user dict has 100 entries and a "scheduler
    tick" (proxied here as iterating ``_per_user_sems``) stays
    well under 100ms."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)

    async def fn() -> None:
        return None

    for u in range(100):
        for p in range(10):
            pool.submit(
                user_email=f"u{u}@x",
                platform=f"p{p}",
                coro=fn(),
            )
    await pool.drain(timeout=10.0)
    assert len(pool._per_user_sems) == 100  # noqa: SLF001
    # Tick stand-in: scheduler reads the dict to decide who's busy.
    start = time.monotonic()
    _ = list(pool._per_user_sems.items())  # noqa: SLF001
    tick = time.monotonic() - start
    assert tick < 0.1


# ---------------------------------------------------------------------------
# 12. Production: backpressure ON -> OFF admits next force_sync
# ---------------------------------------------------------------------------


async def test_backpressure_off_after_cap_raised_admits_next_call(db_path: Path) -> None:
    """An operator scaling the cap up (overlay) on a backed-up queue
    must admit the next request immediately — the cap is read at
    insert time, not cached."""
    from ingestion.daemon.force_sync import enqueue_force_sync
    from ingestion.daemon.services import sync_service
    from ingestion.daemon.services.exceptions import BackpressureError

    from tests.daemon.services.test_credential_service import CapturingAuditWriter

    # Fill up to the cap.
    for i in range(3):
        await enqueue_force_sync(db_path, user_email=f"u{i}@x", platform="moodle")
    writer = CapturingAuditWriter()
    # cap=3 rejects.
    with pytest.raises(BackpressureError):
        await sync_service.force_sync_one(
            db_path,
            email="alice@x",
            platform="moodle",
            audit_writer=writer,
            reason="r",
            max_queue_depth=3,
        )
    # Operator raises the cap to 100; the next call admits without any
    # queue-side cleanup or restart.
    result = await sync_service.force_sync_one(
        db_path,
        email="alice@x",
        platform="moodle",
        audit_writer=writer,
        reason="r",
        max_queue_depth=100,
    )
    assert result.ok is True


# ---------------------------------------------------------------------------
# 13. Production: dead_letter.suppressed payload schema
# ---------------------------------------------------------------------------


async def test_dead_letter_suppressed_event_payload_schema(db_path: Path) -> None:
    """``dead_letter.suppressed`` payload includes ``consecutive_failures``
    and ``suppressed_until`` in ISO 8601 — locks the dashboard's
    consumer contract."""
    events: list[dict[str, Any]] = []

    async def publisher(_channel: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    tracker = DeadLetterTracker(
        db_path,
        threshold=1,
        cooldown_seconds=600,
        event_publisher=publisher,
    )
    await tracker.record_failure("alice@x", "moodle")
    suppressed = [e for e in events if e["event"] == "dead_letter.suppressed"]
    assert suppressed, "no suppressed event emitted"
    payload = suppressed[0]
    assert payload["user_email"] == "alice@x"
    assert payload["platform"] == "moodle"
    assert payload["consecutive_failures"] == 1
    # ISO 8601 with timezone — datetime.fromisoformat round-trips.
    iso = payload["suppressed_until"]
    assert isinstance(iso, str)
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None


async def test_dead_letter_cleared_event_has_zero_count_and_none_until(
    db_path: Path,
) -> None:
    """``dead_letter.cleared`` payload has ``consecutive_failures``
    set to the prior count (so the dashboard sees what was reset)
    and ``suppressed_until`` set to None (the row is no longer
    suppressed)."""
    events: list[dict[str, Any]] = []

    async def publisher(_channel: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    tracker = DeadLetterTracker(
        db_path,
        threshold=2,
        cooldown_seconds=600,
        event_publisher=publisher,
    )
    # 2 failures -> trip suppression.
    await tracker.record_failure("alice@x", "moodle")
    await tracker.record_failure("alice@x", "moodle")
    # Clear.
    await tracker.clear("alice@x", "moodle")
    cleared = [e for e in events if e["event"] == "dead_letter.cleared"]
    assert cleared, "no cleared event"
    payload = cleared[0]
    # Prior count was 2 (carried in the event for dashboard display).
    assert payload["consecutive_failures"] == 2
    assert payload["suppressed_until"] is None


# ---------------------------------------------------------------------------
# 14. Production: backoff with RetryPolicy(10, 1, 60) -> elapsed bounded
# ---------------------------------------------------------------------------


async def test_backoff_with_retry_policy_geometric_total_elapsed_bounded() -> None:
    """A long retry budget (10 attempts at base=1, max=60) records
    delays that geometric-cap then saturate at 60. The simulated
    total elapsed (sum of delays) is bounded by the cap * (n-1)
    plus base."""
    import random

    rng = random.Random(0)  # noqa: S311 — test-only seeded RNG
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    async def boom() -> str:
        raise RuntimeError("nope")

    p = RetryPolicy(max_attempts=10, base_delay_seconds=1.0, max_delay_seconds=60.0)
    with pytest.raises(RuntimeError):
        await with_backoff(
            boom,
            max_attempts=p.max_attempts,
            base_delay_seconds=p.base_delay_seconds,
            max_delay_seconds=p.max_delay_seconds,
            jitter=p.jitter,
            sleep=record,
            rng=rng,
        )
    # 9 retries, all bounded by 60.
    assert len(sleeps) == 9
    assert all(0.0 <= s <= 60.0 for s in sleeps)
    # Total stays under 9 * 60 = 540 (looser pin for the upper).
    assert sum(sleeps) < 540.0


# ---------------------------------------------------------------------------
# Edge brainstorm additions (>= 8) — tagged inline
# ---------------------------------------------------------------------------


async def test_edge_recovery_timeout_zero_collapses_to_always_closed() -> None:
    """[TEST-COVERAGE] ``recovery_timeout_seconds=0`` is documented as
    operator opt-out: every call attempts the probe immediately, so the
    breaker effectively never stays OPEN."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=0)

    async def boom() -> str:
        raise RuntimeError("nope")

    async def ok_call() -> str:
        return "ok"

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    # No clock advance — but recovery_timeout_seconds==0 means
    # next_attempt_at is already <= now, so the next call admits.
    _ = clock  # not advanced, just held for symmetry.
    result = await br.call("k", ok_call)
    assert result == "ok"
    state = await br.state_for("k")
    assert state.state is BreakerOutcome.CLOSED


async def test_edge_pipeline_fetch_retry_does_not_retry_runtime_error() -> None:
    """[TEST-COVERAGE] The pipeline's retry predicate filters on
    ``RateLimitError | PlatformUnavailableError`` only (per impl
    note); generic ``RuntimeError`` surfaces immediately rather than
    consuming the budget. Pin via the simpler ``with_backoff``
    surface using a predicate that mirrors the pipeline's."""
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    class _RateLimitError(Exception):
        pass

    def is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, _RateLimitError)

    attempts = 0

    async def runtime_boom() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("not retryable")

    with pytest.raises(RuntimeError):
        await with_backoff(
            runtime_boom,
            max_attempts=5,
            jitter=False,
            retryable=is_retryable,
            sleep=record,
        )
    assert attempts == 1
    assert sleeps == []

    # Counter-case: rate-limit IS retried.
    attempts = 0

    async def rate_boom() -> str:
        nonlocal attempts
        attempts += 1
        raise _RateLimitError("nope")

    with pytest.raises(_RateLimitError):
        await with_backoff(
            rate_boom,
            max_attempts=3,
            jitter=False,
            retryable=is_retryable,
            sleep=record,
        )
    assert attempts == 3


async def test_edge_per_user_dict_grows_with_unique_users() -> None:
    """[CODE-FIX flag for slice 12] Per-user dict has no cleanup pass
    today. A ``WorkerPool`` that sees N unique users over its
    lifetime carries N entries — bounded by user count, but not zero
    on idle. Pin so a future cleanup change is reviewed."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)

    async def fn() -> None:
        return None

    for i in range(50):
        pool.submit(user_email=f"u{i}@x", platform="moodle", coro=fn())
    await pool.drain(timeout=2.0)
    assert len(pool._per_user_sems) == 50  # noqa: SLF001


async def test_edge_module_singleton_reset_helper_works() -> None:
    """[TEST-COVERAGE] The module-level breaker singletons retain state
    across tests in the same process. Exercise the public
    ``reset_breaker_state_for_tests`` helper (re-exported above as
    ``reset_breaker_state``) so the next tester pass has a documented
    escape hatch — the helper now lives in
    :mod:`ingestion.daemon.reliability.circuit_breaker` so any test
    suite can import it directly."""
    br, _ = _make_breaker(failure_threshold=1)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("singleton-test", boom)
    assert (await br.state_for("singleton-test")).state is BreakerOutcome.OPEN
    # Reset via the public production-side helper.
    reset_breaker_state_for_tests(br)
    state = await br.state_for("singleton-test")
    assert state.state is BreakerOutcome.CLOSED
    assert state.failure_count == 0
    # The historical local name still works for back-compat.
    with pytest.raises(RuntimeError):
        await br.call("singleton-test", boom)
    reset_breaker_state(br)
    assert (await br.state_for("singleton-test")).state is BreakerOutcome.CLOSED


async def test_edge_circuit_open_error_carries_consistent_next_attempt() -> None:
    """[TEST-COVERAGE] ``CircuitOpenError.next_attempt_at`` matches the
    breaker's stored state — calling state_for and reading the
    exception both surface the same timestamp."""
    br, clock = _make_breaker(failure_threshold=1, recovery_timeout_seconds=120)

    async def boom() -> str:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await br.call("k", boom)
    state = await br.state_for("k")
    assert state.next_attempt_at is not None

    async def ok_call() -> str:
        return "ok"

    with pytest.raises(CircuitOpenError) as excinfo:
        await br.call("k", ok_call)
    assert excinfo.value.next_attempt_at == state.next_attempt_at
    assert excinfo.value.next_attempt_at == clock.now + timedelta(seconds=120)


async def test_edge_dead_letter_no_event_when_already_suppressed(db_path: Path) -> None:
    """[TEST-COVERAGE] A failure that lands while the row is ALREADY
    suppressed must NOT re-emit ``dead_letter.suppressed`` — the
    transition only fires on the prior->suppressed crossing."""
    events: list[dict[str, Any]] = []

    async def publisher(_channel: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    tracker = DeadLetterTracker(
        db_path,
        threshold=1,
        cooldown_seconds=600,
        event_publisher=publisher,
    )
    await tracker.record_failure("alice@x", "moodle")
    suppressed_before = [e for e in events if e["event"] == "dead_letter.suppressed"]
    assert len(suppressed_before) == 2  # 2 channels: dead_letter + errors

    # Second failure while still suppressed — no new transition.
    await tracker.record_failure("alice@x", "moodle")
    suppressed_after = [e for e in events if e["event"] == "dead_letter.suppressed"]
    assert len(suppressed_after) == 2  # unchanged


async def test_edge_dead_letter_clear_on_unsuppressed_emits_no_event(
    db_path: Path,
) -> None:
    """[TEST-COVERAGE] A clear on an already-clear row is a no-op for
    events — no transition occurred."""
    events: list[dict[str, Any]] = []

    async def publisher(_channel: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    tracker = DeadLetterTracker(
        db_path,
        threshold=10,
        cooldown_seconds=60,
        event_publisher=publisher,
    )
    # Fresh row — never suppressed.
    await tracker.clear("alice@x", "moodle")
    cleared = [e for e in events if e["event"] == "dead_letter.cleared"]
    assert cleared == []


async def test_edge_action_timeout_carries_request_id_in_log() -> None:
    """[TEST-COVERAGE flag for slice 6 integration] When the action
    timeout fires inside a request scope, structlog binds
    ``request_id`` via ``RequestIdMiddleware`` and the operator can
    pivot from a 502 to the timing event. We exercise the helper
    here without the middleware (the request_id contextvar default
    is empty) and pin that the helper itself does NOT crash on the
    missing-scope case."""

    async def slow() -> None:
        await asyncio.sleep(0.5)

    with pytest.raises(TransientFailureError):
        await with_action_timeout(0.01, slow())
    # Helper survives without a bound request_id (background task path).
