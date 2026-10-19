"""Polymorphic credential-refresh coordinator.

Where :mod:`refresh` holds the Teams-specific orchestration (driver
pool, capture-then-merge, the silent-OIDC chain), this module is the
*platform-agnostic* glue that lets the bulk-refresh loop call any
:class:`RefreshStrategy` registered on a :class:`PlatformSpec`.

A single :func:`refresh_credential` call:

1. Acquires the per-user lock so a force-refresh action can't double
   up with the bulk cycle. Locks are caller-supplied so the runtime
   can share one dict across the API and the loop.
2. Loads the current credential payload from the token store.
   Returns ``NO_TOKENS`` when the row is absent or empty — the
   strategy never sees a missing payload.
3. Runs ``strategy.refresh`` under :func:`wrap_unexpected` so an
   unexpected exception inside the strategy collapses to
   :data:`RefreshOutcome.ERROR` instead of propagating.
4. Persists the outcome:

   * :data:`RefreshOutcome.SUCCESS` with a new payload → write through
     :meth:`TokenStore.update_credentials`.
   * :data:`RefreshOutcome.EXPIRED` → flip ``is_expired`` via
     :meth:`TokenStore.mark_expired`. The store's CAS guarantees
     idempotency; calling :meth:`mark_expired` on an already-expired
     row is a no-op (no audit double-write, no notifier double-fire).
   * :data:`RefreshOutcome.NO_TOKENS` / :data:`RefreshOutcome.ERROR` →
     no persistence.

The function returns a :class:`CredentialRefreshOutcome` (defined in
this module) carrying the email, platform, outcome enum, and a
truncated error string. The bulk loop aggregates these into
:class:`BulkRefreshStats`.

This module deliberately holds no Teams-specific knowledge — the
Teams flow goes through :class:`TeamsRefreshStrategy` like any other
platform. The legacy :func:`refresh_one` in :mod:`refresh` remains as
a backwards-compat path for the maintenance-service force-refresh
action, which has its own audit-writer integration to maintain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingestion.daemon.auth.strategy import RefreshOutcome, RefreshResult, wrap_unexpected
from ingestion.daemon.logging import bind_sync_context, get_logger
from ingestion.daemon.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
)

if TYPE_CHECKING:
    import asyncio

    from ingestion.daemon.auth.strategy import RefreshStrategy
    from ingestion.ports import TokenStore


# Module-level breaker so every refresh path through the coordinator
# shares one state map keyed on ``refresh:<platform>``. Lazy: first
# call creates the per-key state. The breaker is process-local — a
# restart resets every key to CLOSED — and intentionally not config-
# overlay-driven. Operators tune sensitivity via env-overlay later.
#
# TESTING HAZARD: this singleton retains state across tests in the
# same process. A test that drives the breaker to OPEN leaves it OPEN
# for every later test that calls ``refresh_credential`` until the
# state is explicitly reset. Use
# :func:`ingestion.daemon.reliability.circuit_breaker.reset_breaker_state_for_tests`
# between tests, or construct a fresh ``CircuitBreaker`` and inject it
# past the module-level default. See the circuit_breaker module
# docstring for the full pattern.
_REFRESH_BREAKER = CircuitBreaker()


def get_refresh_breaker() -> CircuitBreaker:
    """Expose the module-level breaker for tests and metrics readers.

    Tests that trip this breaker MUST reset its state via
    :func:`ingestion.daemon.reliability.circuit_breaker.reset_breaker_state_for_tests`
    or the OPEN state leaks into later tests in the same process.
    """
    return _REFRESH_BREAKER


class _RefreshError(Exception):
    """Internal sentinel signalling the breaker to count a strategy
    ERROR result as a failure. Caught immediately around the breaker
    call so no other code sees this type."""


@dataclass(frozen=True, slots=True)
class CredentialRefreshOutcome:
    """Per-credential outcome the bulk loop aggregates.

    Carries email + platform so the :class:`BulkRefreshStats`
    counters can pivot on platform without re-querying the candidate
    list. ``error`` is the truncated message from the strategy
    result (or our own wrap), suitable for log_failure.
    """

    user_email: str
    platform: str
    outcome: RefreshOutcome
    error: str | None = None


async def refresh_credential(
    *,
    user_email: str,
    platform: str,
    token_store: TokenStore,
    strategy: RefreshStrategy,
    user_lock: asyncio.Lock | None = None,
) -> CredentialRefreshOutcome:
    """Run one strategy-driven refresh for ``(user_email, platform)``.

    ``user_lock`` is the per-user serialization lock. The bulk loop
    passes one lock per email so two concurrent refreshes for the
    same user (e.g. force-refresh action overlapping with the cycle)
    can't write the same credential row at once. Without a lock the
    function still functions but the caller trusts the pool to dedupe.

    Never raises: every error path produces an outcome enum. The
    strategy's own exceptions are collapsed via :func:`wrap_unexpected`;
    a token-store I/O exception is caught here and reported as
    :data:`RefreshOutcome.ERROR` (the row write failed, not the
    refresh itself, but the caller's only useful action is "retry
    next cycle").
    """
    log = get_logger(__name__)
    with bind_sync_context(user_email=user_email, platform=platform):
        if user_lock is not None:
            async with user_lock:
                return await _refresh_credential_inner(
                    user_email=user_email,
                    platform=platform,
                    token_store=token_store,
                    strategy=strategy,
                    log=log,
                )
        return await _refresh_credential_inner(
            user_email=user_email,
            platform=platform,
            token_store=token_store,
            strategy=strategy,
            log=log,
        )


async def _refresh_credential_inner(
    *,
    user_email: str,
    platform: str,
    token_store: TokenStore,
    strategy: RefreshStrategy,
    log: object,
) -> CredentialRefreshOutcome:
    try:
        creds = await token_store.get_credentials(user_email, platform)
    except Exception as exc:  # noqa: BLE001 — defensive boundary
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.ERROR,
            error=f"token_store.get_credentials: {type(exc).__name__}: {exc}"[:200],
        )
    if creds is None:
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.NO_COOKIES,
            error="no credential row",
        )

    # Per-platform breaker on the refresh path. When a platform's auth
    # endpoint goes down a CLOSED breaker tolerates ``failure_threshold``
    # consecutive errors before tripping; once OPEN, callers see an
    # immediate CircuitOpenError instead of N more attempts at the
    # broken upstream. Translated to RefreshOutcome.ERROR so the bulk
    # cycle's stats reflect the rejection without leaking the breaker
    # type into upstream code.
    #
    # ``wrap_unexpected`` already swallows strategy exceptions and
    # converts them to ``RefreshOutcome.ERROR``. We want the breaker
    # to count *both* a raised exception (defensive — shouldn't happen
    # post-wrap_unexpected, but the helper is wrap-not-raise) and a
    # returned ERROR outcome as a failure, so we re-raise inside the
    # breaker callable when the result is ERROR. Other outcomes
    # (SUCCESS, EXPIRED, NO_TOKENS, NO_COOKIES) count as successful
    # observations of upstream — none of those indicate the upstream
    # itself is down.
    breaker = _REFRESH_BREAKER
    breaker_key = f"refresh:{platform}"

    async def _do_refresh() -> RefreshResult:
        inner = await wrap_unexpected(strategy, user_email, current_payload=creds)
        if inner.outcome is RefreshOutcome.ERROR:
            # Counts as a breaker failure but we want to preserve the
            # original outcome shape upstream — caught below.
            raise _RefreshError(inner.error_message or "refresh errored")
        return inner

    try:
        result = await breaker.call(breaker_key, _do_refresh)
    except CircuitOpenError as exc:
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.ERROR,
            error=f"circuit_open: {exc}"[:200],
        )
    except _RefreshError as exc:
        # Preserve the underlying ERROR outcome the strategy produced.
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.ERROR,
            error=str(exc)[:200],
        )

    if result.outcome is RefreshOutcome.SUCCESS and result.new_payload is not None:
        try:
            await token_store.update_credentials(user_email, platform, result.new_payload)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            return CredentialRefreshOutcome(
                user_email=user_email,
                platform=platform,
                outcome=RefreshOutcome.ERROR,
                error=f"token_store.update_credentials: {type(exc).__name__}: {exc}"[:200],
            )
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.SUCCESS,
            error=None,
        )

    if result.outcome is RefreshOutcome.EXPIRED:
        # Mark expired via the token store's atomic CAS. The store's
        # implementation is idempotent: calling mark_expired on an
        # already-expired row is a no-op (no audit double-write, no
        # notifier double-fire on the false-to-true transition).
        try:
            await token_store.mark_expired(user_email, platform)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            # Even if marking fails, the strategy still observed
            # EXPIRED — surface that to the caller; the next cycle
            # will retry the mark.
            return CredentialRefreshOutcome(
                user_email=user_email,
                platform=platform,
                outcome=RefreshOutcome.EXPIRED,
                error=f"mark_expired: {type(exc).__name__}: {exc}"[:200],
            )
        return CredentialRefreshOutcome(
            user_email=user_email,
            platform=platform,
            outcome=RefreshOutcome.EXPIRED,
            error=result.error_message,
        )

    # NO_TOKENS / NO_COOKIES / ERROR: no write-back. The error
    # message (if any) is preserved so the bulk loop's sync_history
    # row carries the strategy's own diagnostic.
    _ = log  # silence unused warning; logs are emitted inside strategies
    return CredentialRefreshOutcome(
        user_email=user_email,
        platform=platform,
        outcome=result.outcome,
        error=result.error_message,
    )
