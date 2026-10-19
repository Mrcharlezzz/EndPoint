"""Background loop that runs credential-refresh cycles on an interval.

Slice 8: this loop is now *polymorphic* — instead of filtering
candidates to ``c.platform == "teams"`` it walks the platform
registry, picks every platform whose :class:`PlatformSpec` carries a
:attr:`refresh_strategy`, and submits all matching candidates to one
:func:`run_polymorphic_refresh_cycle`. The strategy abstraction
(slice 8) makes the per-platform behaviour uniform: Teams runs the
silent-OIDC chain (rotate-on-call), Moodle runs ``site_info``, and
Telegram runs ``getMe`` (probe-not-rotate).

Cadence rationale stays the same as the Teams-only original: the
loop runs on a coarser tick than the scheduler (default every
30 minutes) because a Teams Graph access_token is only valid ~60
minutes and we want every user refreshed within one token lifetime.
The Moodle/Telegram probes are cheap so the same cadence is fine.

Lives alongside the scheduler as a separate asyncio task. Exits on
the shared shutdown :class:`asyncio.Event`; the shutdown event is
threaded into the bulk cycle so an in-flight 30-minute distributed
set of refreshes can abandon pending offsets instead of blocking
SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingestion.daemon.auth.bulk_refresh import run_polymorphic_refresh_cycle
from ingestion.daemon.logging import get_logger
from ingestion.daemon.platforms import PLATFORMS, get_platform

if TYPE_CHECKING:
    import random

    from ingestion.daemon.auth.strategy import RefreshStrategy
    from ingestion.daemon.candidates import CandidateSource
    from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
    from ingestion.ports import TokenStore


@dataclass
class RefreshLoopDeps:
    token_store: TokenStore
    candidate_source: CandidateSource
    interval_seconds: int
    timeout_seconds: int = 30
    max_retries_per_user: int = 0
    concurrency: int = 8
    rng: random.Random | None = None
    # Optional. When provided, each refresh writes a ``sync_history`` row with
    # ``sync_kind='credential_refresh'`` so the UI's "Refresh activity" tab
    # can render alongside content syncs.
    sync_logger: SqliteSyncLogger | None = None
    # Process-wide per-user lock dict, shared with the on-demand refresh
    # action so the two refresh paths serialise on the same lock per user.
    # Defaults to None; the cycle then uses a local dict (tests want this).
    user_locks: dict[str, asyncio.Lock] | None = None


class TeamsRefreshLoop:
    """Polymorphic credential-refresh loop.

    Name kept for backwards compat with the runtime composition and
    test imports — slice 8 generalised the body but the class name
    change can wait for a follow-up rename. Despite the name this
    loop now drives every platform whose :class:`PlatformSpec` has a
    registered :attr:`refresh_strategy`.
    """

    def __init__(self, deps: RefreshLoopDeps) -> None:
        self._deps = deps
        self._log = get_logger(__name__)
        # Warn-once memory: a candidate whose platform has no
        # registered strategy gets one structured warning per process.
        # Without this an operator who adds a new platform without a
        # strategy never sees that the loop silently drops it.
        self._unsupported_platforms_warned: set[str] = set()

    async def run(self, shutdown: asyncio.Event) -> None:
        """Main loop. Cycles until ``shutdown`` is set."""
        while not shutdown.is_set():
            self._log.info("refresh_loop.cycle_started")
            await self._run_one_cycle(shutdown)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    shutdown.wait(),
                    timeout=self._deps.interval_seconds,
                )

    async def _run_one_cycle(self, shutdown: asyncio.Event) -> None:
        try:
            candidates = await self._list_refresh_candidates()
        except Exception:
            self._log.exception("refresh_loop.candidate_listing_failed")
            return
        if not candidates:
            self._log.info("refresh_loop.no_refresh_candidates")
            return
        try:
            await run_polymorphic_refresh_cycle(
                candidates=candidates,
                token_store=self._deps.token_store,
                strategy_for=_strategy_for,
                interval_seconds=self._deps.interval_seconds,
                concurrency=self._deps.concurrency,
                rng=self._deps.rng,
                shutdown=shutdown,
                sync_logger=self._deps.sync_logger,
                user_locks=self._deps.user_locks,
            )
        except Exception:
            self._log.exception("refresh_loop.cycle_errored")

    async def _list_refresh_candidates(self) -> list[tuple[str, str]]:
        """Return ``(email, platform)`` pairs eligible for refresh.

        Filters:
        * Platform has a registered :attr:`refresh_strategy` on its
          spec (skipped silently otherwise — runtime never registered
          one for that platform).
        * Candidate is not paused.
        * Candidate is not already expired (defensive: production
          ``CandidateSource`` filters expired rows but a misconfigured
          source shouldn't burn pool slots on dead credentials).

        Logs one warn-once per platform without a strategy so an
        operator notices the gap without grepping the registry.
        """
        candidates = await self._deps.candidate_source.list_candidates()
        out: list[tuple[str, str]] = []
        for c in candidates:
            if c.is_paused or getattr(c, "is_expired", False):
                continue
            try:
                spec = get_platform(c.platform)
            except KeyError:
                # Unknown platform — scheduler logs unknown platforms;
                # we don't double-log here.
                continue
            if spec.refresh_strategy is None:
                if c.platform not in self._unsupported_platforms_warned:
                    self._unsupported_platforms_warned.add(c.platform)
                    self._log.warning(
                        "refresh_loop.platform_skipped_no_strategy",
                        platform=c.platform,
                    )
                continue
            out.append((c.user_email, c.platform))
        return out


def _strategy_for(platform: str) -> RefreshStrategy | None:
    """Look up a platform's registered refresh strategy.

    Wrapped in a free function so the polymorphic cycle can be tested
    with a stub resolver instead of monkeypatching the registry.
    Returns ``None`` for unknown platforms or platforms without a
    registered strategy — the cycle treats both as "skip".
    """
    spec = PLATFORMS.get(platform)
    if spec is None:
        return None
    return spec.refresh_strategy
