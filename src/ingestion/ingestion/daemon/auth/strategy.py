"""Polymorphic credential-refresh contract shared across platforms.

Slice 8 introduces a single :class:`RefreshStrategy` Protocol so the
:func:`run_bulk_refresh_cycle` and :class:`TeamsRefreshLoop` paths stop
hard-coding ``platform == "teams"``. Each platform registers a strategy
on its :class:`PlatformSpec` (slice 3 registry); the bulk loop iterates
strategies polymorphically and skips platforms that don't expose one.

Two flavours of refresh are anticipated:

* **Rotate-on-call.** The strategy holds OAuth-style credentials and
  exchanges a refresh_token for a fresh access_token (Teams today via
  :class:`HttpxTokenRefresher`). The strategy returns the rotated
  payload; the caller persists it.
* **Probe-not-rotate.** The strategy doesn't mint anything new but
  validates the current credential by calling a lightweight read-only
  API (Moodle ``site_info``, Telegram ``getMe``). On 401/403 the
  outcome is :data:`RefreshOutcome.EXPIRED` so the caller can flip
  ``is_expired`` on the credential row, mirroring the Teams flow.

This module deliberately re-exports :class:`RefreshOutcome` from
:mod:`ingestion.daemon.auth.refresh` so the legacy Teams path keeps
its single source of truth on the enum even after the protocol
abstraction lands. New platforms import :class:`RefreshOutcome` from
this module by convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Single source of truth for the outcome enum. Teams already exports it
# from ``refresh.py`` and several call sites import via that path; keep
# both names alive so a one-shot migration isn't required across slices.
from ingestion.daemon.auth.refresh import RefreshOutcome

__all__ = (
    "AuthenticationProbeError",
    "RefreshError",
    "RefreshOutcome",
    "RefreshResult",
    "RefreshStrategy",
    "RefreshUnsupportedError",
    "wrap_unexpected",
)


class RefreshError(Exception):
    """Base for refresh-strategy failures.

    Strategies SHOULD return a :class:`RefreshResult` with
    :attr:`RefreshOutcome.ERROR` rather than raising — the caller (bulk
    loop, force-refresh action) doesn't have a robust per-strategy
    error-routing path. This class exists so strategies that want to
    signal a typed failure to a direct test harness still can; the
    bulk loop wraps everything via :func:`wrap_unexpected` regardless.
    """


class AuthenticationProbeError(RefreshError):
    """The probe round-trip itself failed before a 401/403 came back.

    Distinct from :class:`RefreshOutcome.EXPIRED` which means "probe
    succeeded and the platform said the token is dead". This is the
    "we couldn't even ask the platform" case (DNS, network blip, TLS).
    """


class RefreshUnsupportedError(RefreshError):
    """A caller invoked :meth:`RefreshStrategy.refresh` on a strategy
    whose :meth:`is_refresh_supported` returns ``False``.

    Defensive: bulk loop pre-filters on supports-flag, but a hand-wired
    test that skips the gate gets a clear error rather than an
    exception from inside the strategy guts.
    """


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of one :meth:`RefreshStrategy.refresh` call.

    ``new_payload`` is ``None`` when the strategy is probe-not-rotate
    (Moodle, Telegram) or when the call did not produce a writable
    update (e.g. ``EXPIRED``). The caller persists ``new_payload``
    only on :data:`RefreshOutcome.SUCCESS` with a non-None payload —
    callers that want to flip ``is_expired`` consult
    ``outcome is RefreshOutcome.EXPIRED`` separately.

    ``latency_ms`` is wall-clock milliseconds for the attempt. Captured
    by the strategy itself so the outer loop's distribution measures
    the real cost (including platform side, not just queue waits).
    """

    outcome: RefreshOutcome
    new_payload: dict[str, Any] | None = None
    error_message: str | None = None
    latency_ms: float = 0.0


@runtime_checkable
class RefreshStrategy(Protocol):
    """Per-platform credential-refresh surface.

    Implementations live in
    :mod:`ingestion.daemon.auth.<platform>_strategy` and register on
    :class:`ingestion.daemon.platforms.PlatformSpec.refresh_strategy`.

    The contract is intentionally narrow: the bulk loop holds the
    credential payload and persistence; the strategy only knows how
    to "refresh / probe the credential and report back".

    All methods are ``async`` so a strategy can spawn an httpx
    request, queue itself behind a semaphore, etc. without forcing
    the bulk loop into ``asyncio.to_thread`` callsites.
    """

    async def refresh(
        self,
        email: str,
        *,
        current_payload: dict[str, Any],
    ) -> RefreshResult:
        """Refresh or probe the credential for ``email``.

        ``current_payload`` is the full decrypted credential blob
        from the token store (e.g. ``{"token": "...", "user_id": 42}``
        for Moodle). May be empty (``{}``) when the row has just been
        provisioned — strategies must handle that as
        :data:`RefreshOutcome.NO_TOKENS` rather than crashing on a
        missing key.
        """
        ...

    async def is_refresh_supported(self) -> bool:
        """Return ``True`` when this platform supports refresh.

        Strategies that exist as scaffolding only (a future platform
        whose refresh contract isn't pinned yet) return ``False`` and
        the bulk loop skips them with a structured warn-once log line.
        Default for an implemented platform is ``True``.
        """
        ...


async def wrap_unexpected(
    strategy: RefreshStrategy,
    email: str,
    *,
    current_payload: dict[str, Any],
) -> RefreshResult:
    """Run ``strategy.refresh`` and convert any unexpected exception
    into a :data:`RefreshOutcome.ERROR` result.

    Strategies are *encouraged* to return ``RefreshResult`` directly,
    but a bug deep inside (httpx connection error mistakenly leaking
    out, KeyError on a missing payload field) shouldn't take down the
    bulk loop's worker. This helper centralises the wrap so every
    call site (bulk_refresh, force_refresh) gets the same guarantee.

    The exception's text is truncated at 200 chars in the result's
    ``error_message`` so a noisy backtrace doesn't blow up the
    structured-log payload size. Callers that need the original
    exception for telemetry should add their own ``except`` *outside*
    this helper.
    """
    try:
        return await strategy.refresh(email, current_payload=current_payload)
    except Exception as exc:  # noqa: BLE001 — defensive bulk-loop boundary
        return RefreshResult(
            outcome=RefreshOutcome.ERROR,
            new_payload=None,
            error_message=f"{type(exc).__name__}: {exc}"[:200],
        )
