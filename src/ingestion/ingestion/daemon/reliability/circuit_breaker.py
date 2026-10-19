"""Per-key 3-state circuit breaker for fallible upstream calls.

Wraps a callable with a CLOSED -> OPEN -> HALF_OPEN -> CLOSED state
machine. When ``failure_threshold`` consecutive calls for a given key
fail the breaker trips OPEN; subsequent calls raise
:class:`CircuitOpenError` immediately rather than hammering the
upstream. After ``recovery_timeout_seconds`` the breaker transitions
to HALF_OPEN and admits up to ``half_open_max_calls`` probes; a probe
success closes the breaker and resets counts, a probe failure re-opens
it with a reset timer.

State is held per-key in a process-local dict guarded by per-key
``asyncio.Lock``. This prevents the race where N concurrent callers
each see the breaker as CLOSED, each fail, and each report "open"
while having incremented the counter past threshold simultaneously.
A cross-restart persistent backing (sqlite) is OUT OF SCOPE for this
slice — restart resets every breaker to CLOSED and the dead-letter
tracker plus the bulk-refresh cycle's own retry budget cover the
reliability gap on first-tick-after-restart.

The breaker is keyed by a free-form string. Callers compose keys like
``f"refresh:{platform}"`` or ``f"fetch:{platform}"`` so the refresh
path and the fetch path each get an independent breaker per platform.
The dict grows unbounded with new keys, but in practice the keyspace
is bounded by ``num_actions * num_platforms`` (a handful) so cleanup
is unnecessary.

Probes in HALF_OPEN are bounded to a 30s timeout so a long-running
call doesn't pin the single probe slot indefinitely. Cancellation of
a probe (``asyncio.CancelledError``) is treated as "no observation":
the breaker state is unchanged, neither success nor failure is
recorded. This matches the "do not count operator-initiated stops as
failures" semantics the worker pool already uses.

Module-level singleton testing hazard
-------------------------------------

Two callers hold breakers as MODULE-LEVEL singletons that retain state
across tests in the same process:

* :func:`ingestion.daemon.auth.coordinator.get_refresh_breaker` →
  ``coordinator._REFRESH_BREAKER`` (key prefix ``refresh:<platform>``)
* :func:`ingestion.pipeline.get_fetch_breaker` →
  ``pipeline._FETCH_BREAKER`` (key prefix ``fetch:<platform>``)

A test that trips one of these singletons leaves the breaker OPEN for
the duration of the process unless it explicitly resets state. Use
:func:`reset_breaker_state_for_tests` between tests that touch either
singleton, OR construct a fresh ``CircuitBreaker`` and inject it past
the module-level default.

This is enough rope to hang a test suite — see the conftest patterns
in ``tests/daemon/auth`` and ``tests/edges/s10_reliability.py`` for
the canonical reset hooks.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Cap probe duration in HALF_OPEN. A wedged upstream that holds the
# probe slot for minutes would lock every other caller out of the
# breaker — we prefer to fail the probe and re-open with a fresh
# cooldown.
HALF_OPEN_PROBE_TIMEOUT_SECONDS: float = 30.0


class BreakerOutcome(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the breaker is OPEN.

    Carries the key + the time the breaker is expected to transition
    to HALF_OPEN so callers can render an informative error or schedule
    a retry past the cooldown.
    """

    def __init__(self, key: str, *, next_attempt_at: datetime | None) -> None:
        super().__init__(f"circuit breaker open for {key!r}")
        self.key = key
        self.next_attempt_at = next_attempt_at


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """Configuration for a :class:`CircuitBreaker` instance.

    ``failure_threshold`` is the consecutive-failure count that trips
    the breaker. A successful call resets the counter; the threshold
    therefore measures uninterrupted streaks, not totals.

    ``recovery_timeout_seconds`` controls how long the breaker stays
    OPEN before admitting a probe. A value of 0 effectively disables
    the OPEN phase: every call attempts the probe, which means the
    breaker collapses to "always-CLOSED" behaviour. Document this as
    operator opt-out; do not enforce a minimum here.

    ``half_open_max_calls`` caps concurrent probes during HALF_OPEN.
    Default 1 — only one in-flight probe at a time; further callers
    see the breaker as still-OPEN until the probe lands.
    """

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 60.0
    half_open_max_calls: int = 1


@dataclass(frozen=True, slots=True)
class BreakerState:
    """Snapshot of one key's breaker state.

    Frozen so observers (tests, dashboards) cannot mutate the live
    state via the snapshot. The actual mutable state lives on a
    private ``_KeyState`` inside :class:`CircuitBreaker`.
    """

    state: BreakerOutcome
    failure_count: int = 0
    last_failure_at: datetime | None = None
    next_attempt_at: datetime | None = None


@dataclass(slots=True)
class _KeyState:
    """Mutable per-key state guarded by ``lock``.

    Held inside the breaker and never handed out to callers; the public
    snapshot type is :class:`BreakerState`. The lock serialises every
    transition so two concurrent failures for the same key cannot both
    increment the counter past the threshold and report different
    outcomes.
    """

    state: BreakerOutcome = BreakerOutcome.CLOSED
    failure_count: int = 0
    last_failure_at: datetime | None = None
    next_attempt_at: datetime | None = None
    half_open_in_flight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class CircuitBreaker:
    """Process-local breaker keyed by free-form strings.

    Construct one instance per breaker family (e.g. one for the auth
    refresh path, one for the pipeline fetch path) and reuse across
    keys. The internal dict lazily creates per-key state on first
    use.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        probe_timeout_seconds: float = HALF_OPEN_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config or BreakerConfig()
        self._now = now or _utc_now
        self._probe_timeout_seconds = probe_timeout_seconds
        self._states: dict[str, _KeyState] = {}
        self._states_lock = asyncio.Lock()
        self._log = get_logger(__name__)

    @property
    def config(self) -> BreakerConfig:
        return self._config

    async def state_for(self, key: str) -> BreakerState:
        """Return a snapshot of ``key``'s state. Creates if absent."""
        ks = await self._get_or_create(key)
        async with ks.lock:
            return BreakerState(
                state=ks.state,
                failure_count=ks.failure_count,
                last_failure_at=ks.last_failure_at,
                next_attempt_at=ks.next_attempt_at,
            )

    async def call[T](self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Invoke ``fn`` under the breaker keyed by ``key``.

        Raises :class:`CircuitOpenError` immediately if the breaker is
        OPEN and the cooldown has not elapsed. Otherwise runs ``fn``
        and records the outcome. Probes during HALF_OPEN are wrapped
        in a per-call timeout so a wedged upstream cannot pin the
        single-probe slot.
        """
        ks = await self._get_or_create(key)
        admit_as_probe = await self._admit(key, ks)
        try:
            if admit_as_probe:
                # Pin the probe budget so a hung upstream cannot wedge
                # HALF_OPEN forever. ``asyncio.wait_for`` cancels the
                # underlying coroutine on timeout, which the breaker
                # then treats as a failure.
                result = await asyncio.wait_for(fn(), timeout=self._probe_timeout_seconds)
            else:
                result = await fn()
        except asyncio.CancelledError:
            # Operator-initiated cancel (or shutdown) — do not change
            # state. A cancel is neither success nor failure; counting
            # it as a failure would over-trip the breaker on shutdown.
            if admit_as_probe:
                await self._release_probe_slot(ks)
            raise
        except Exception as exc:
            await self._record_failure(key, ks, was_probe=admit_as_probe)
            raise exc
        else:
            await self._record_success(key, ks, was_probe=admit_as_probe)
            return result

    async def _get_or_create(self, key: str) -> _KeyState:
        async with self._states_lock:
            ks = self._states.get(key)
            if ks is None:
                ks = _KeyState()
                self._states[key] = ks
            return ks

    async def _admit(self, key: str, ks: _KeyState) -> bool:
        """Return True if the call should be treated as a probe.

        Raises :class:`CircuitOpenError` when the breaker is OPEN and
        the cooldown has not yet elapsed, or when HALF_OPEN already has
        ``half_open_max_calls`` probes in flight.
        """
        async with ks.lock:
            now = self._now()
            if ks.state is BreakerOutcome.OPEN:
                if ks.next_attempt_at is not None and now >= ks.next_attempt_at:
                    # Cooldown elapsed: transition to HALF_OPEN and
                    # admit this caller as the first probe.
                    self._transition(ks, BreakerOutcome.HALF_OPEN, key=key)
                    ks.half_open_in_flight = 1
                    return True
                raise CircuitOpenError(key, next_attempt_at=ks.next_attempt_at)
            if ks.state is BreakerOutcome.HALF_OPEN:
                if ks.half_open_in_flight >= self._config.half_open_max_calls:
                    # Another caller already holds the probe slot;
                    # surface as if the breaker were still OPEN so
                    # downstream sees a consistent rejection.
                    raise CircuitOpenError(key, next_attempt_at=ks.next_attempt_at)
                ks.half_open_in_flight += 1
                return True
            # CLOSED: just admit the call.
            return False

    async def _record_success(self, key: str, ks: _KeyState, *, was_probe: bool) -> None:
        async with ks.lock:
            if was_probe:
                ks.half_open_in_flight = max(0, ks.half_open_in_flight - 1)
                # Probe success: close the breaker, reset counters.
                self._transition(ks, BreakerOutcome.CLOSED, key=key)
                ks.failure_count = 0
                ks.last_failure_at = None
                ks.next_attempt_at = None
                return
            # CLOSED success: drop the consecutive-failure counter.
            ks.failure_count = 0
            ks.last_failure_at = None

    async def _record_failure(self, key: str, ks: _KeyState, *, was_probe: bool) -> None:
        async with ks.lock:
            now = self._now()
            ks.last_failure_at = now
            if was_probe:
                ks.half_open_in_flight = max(0, ks.half_open_in_flight - 1)
                # Probe failed: re-open with a fresh cooldown.
                ks.next_attempt_at = now + timedelta(
                    seconds=self._config.recovery_timeout_seconds,
                )
                self._transition(ks, BreakerOutcome.OPEN, key=key)
                return
            # CLOSED failure: bump count and trip if past threshold.
            ks.failure_count += 1
            if ks.failure_count >= self._config.failure_threshold:
                ks.next_attempt_at = now + timedelta(
                    seconds=self._config.recovery_timeout_seconds,
                )
                self._transition(ks, BreakerOutcome.OPEN, key=key)

    async def _release_probe_slot(self, ks: _KeyState) -> None:
        """Roll back a probe-slot acquisition without changing state.

        Called when a probe is cancelled before producing an outcome.
        We deliberately do not transition state because cancellation
        is not an observation of upstream health.
        """
        async with ks.lock:
            ks.half_open_in_flight = max(0, ks.half_open_in_flight - 1)

    def reset_state_for_tests(self) -> None:
        """Drop every per-key entry so the next call sees CLOSED.

        Test-only escape hatch for the module-level singletons in
        :mod:`ingestion.daemon.auth.coordinator` (``_REFRESH_BREAKER``)
        and :mod:`ingestion.pipeline` (``_FETCH_BREAKER``). Those
        singletons retain state across tests in the same process; a
        test that drives a singleton OPEN leaves it OPEN for every
        later test until the state is explicitly reset.

        Not meant for production code — operational reset masks real
        upstream failures and defeats the breaker's whole point. The
        method is annotated public so test modules can call it
        without a private-symbol warning, mirroring the
        ``reset_snapshot_cache_for_tests`` pattern used elsewhere in
        the daemon.
        """
        self._states.clear()

    def _transition(self, ks: _KeyState, target: BreakerOutcome, *, key: str) -> None:
        """In-place state change with structured logging.

        Caller must hold ``ks.lock``. Logging is best-effort; a logger
        failure does not stop the transition. The method emits the
        canonical ``circuit_breaker.{state}`` events the alert engine
        subscribes to.
        """
        if ks.state is target:
            return
        ks.state = target
        event = f"circuit_breaker.{target.value}"
        # Defensive: never let a logging error mask a state transition.
        # The breaker has already mutated state above.
        with contextlib.suppress(Exception):
            self._log.warning(
                event,
                key=key,
                failure_count=ks.failure_count,
                next_attempt_at=ks.next_attempt_at.isoformat()
                if ks.next_attempt_at is not None
                else None,
            )


def reset_breaker_state_for_tests(breaker: CircuitBreaker) -> None:
    """Module-level alias for :meth:`CircuitBreaker.reset_state_for_tests`.

    Lets call sites read as
    ``reset_breaker_state_for_tests(get_refresh_breaker())`` rather
    than method-chaining off the singleton accessor. Both forms do
    the same thing.
    """
    breaker.reset_state_for_tests()


__all__ = [
    "HALF_OPEN_PROBE_TIMEOUT_SECONDS",
    "BreakerConfig",
    "BreakerOutcome",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "reset_breaker_state_for_tests",
]
