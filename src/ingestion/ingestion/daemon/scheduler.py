"""The Scheduler: one tick per second, submit due (user, platform) syncs.

Orchestration only. All side-effect-carrying work (ORM, SQLite, HTTP) is
delegated to injected dependencies so the main loop can be exercised with
in-memory fakes.

Filters, in order, applied to every candidate on every tick:

    1. Platform kill switch (``config.platforms.<name>_enabled``). Off → skip.
    2. Platform pause (``config.platforms.paused.<name>``). On → skip. Distinct
       from the kill switch: pause is a transient operator toggle via the V10
       ops pane; kill-switch is deployment-level configuration.
    3. Drain mode (``config.scheduler.drain_mode``). On → skip submission for
       every candidate; in-flight tasks run to completion.
    4. User paused (``PlatformCredential.is_paused`` on the candidate).
    5. Dead-letter suppression (consecutive failures > threshold).
    6. In-flight dedupe (a task for this (user, platform) is already running).
    7. Due-check: ``now - last_success_at >= interval``.

Reading config fresh on every tick means toggling a kill switch via the
overlay takes effect within one second.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.daemon.config import DaemonConfig, load_config
from ingestion.daemon.force_sync import consume_force_sync, list_force_sync_pending
from ingestion.daemon.logging import bind_sync_context, get_logger
from ingestion.daemon.platforms import (
    UnknownPlatformError,
    get_platform,
    iter_platforms,
)
from ingestion.daemon.schedule_queries import (
    last_success_at_bulk,
    stored_cursor_scopes,
    sync_state_by_id,
    user_interval_overrides_bulk,
)
from ingestion.pipeline import run_sync

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ingestion.daemon.candidates import Candidate, CandidateSource
    from ingestion.daemon.dead_letter import DeadLetterTracker
    from ingestion.daemon.store.local import LocalStore
    from ingestion.daemon.worker import WorkerPool
    from ingestion.ports import Adapter, ContentSink, SyncLogger, SyncStateStore, TokenStore
    from ingestion.schemas import FetchScope


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class SchedulerDeps:
    """All collaborators the scheduler needs, injected at construction.

    Frozen: tests that need to advance the clock must build a new
    ``Scheduler`` with ``dataclasses.replace(deps, now=...)`` rather than
    mutating an existing instance.
    """

    config_store: LocalStore
    candidate_source: CandidateSource
    worker_pool: WorkerPool
    dead_letter: DeadLetterTracker
    token_store: TokenStore
    content_sink: ContentSink
    sync_state: SyncStateStore
    sync_logger: SyncLogger
    adapter_factory: Callable[[str], Adapter]
    sqlite_path: Path
    scope_for_platform: Callable[[str], frozenset[FetchScope] | None] | None = None
    # Default clock is tz-aware UTC. ``datetime.now`` (naive) would only work
    # by accident after `astimezone(UTC)` re-interprets it as local.
    now: Callable[[], datetime] = _utc_now
    # Optional fan-out sink for WebSocket subscribers. Two args: channel
    # name and a JSON-serialisable payload. Runtime passes a
    # ``bus.publish_threadsafe`` wrapper; tests can omit to keep the
    # scheduler decoupled from the API surface.
    event_sink: Callable[[str, dict[str, object]], None] | None = None


class Scheduler:
    """Tick-driven scheduler. One per daemon process."""

    def __init__(self, deps: SchedulerDeps) -> None:
        self._deps = deps
        self._log = get_logger(__name__)
        # Warn-once memory lives on the instance so a second scheduler in the
        # same process (e.g. across test runs) gets its own fresh warnings.
        self._unknown_platforms_seen: set[str] = set()
        # Warn-once memory for the pause skip log. The scheduler checks
        # ``platforms.paused.<name>`` on every tick; emitting one INFO per
        # candidate per tick under a pause would flood the logs. We log
        # each (user, platform) once when its pause transitions to "skip"
        # and clear the entry when the pause lifts so a re-pause re-logs.
        self._paused_skipped_logged: set[tuple[str, str]] = set()
        # (user_email, platform, scope) tuples we've already warned about.
        # Slice 12 wires the cleanup hook from the lifecycle module — when
        # scope bucketing is enabled, vanished cursors are deleted on the
        # first tick rather than only logged. Until the cleanup pass runs
        # we still emit one warning per (user, platform, scope) so
        # operators see the drift in the logs. Cleared by
        # :meth:`_run_scope_cleanup_once` after the prune lands.
        self._scope_vanish_warned: set[tuple[str, str, str]] = set()
        # Has the per-platform cursor cleanup pass already run? Walks
        # every registered platform once on the first tick where
        # ``scope_for_platform`` is wired. Re-runs only when an explicit
        # signal flips this back to False (currently SIGHUP — see
        # :func:`runtime._on_sighup`).
        self._scope_cleanup_done: bool = False

    # Hard cap on how long scheduler.run will wait for in-flight tasks to
    # drain before giving up and letting the runtime force-stop the pool.
    # The Phase 0 SIGTERM exit criterion is 5 seconds; this must stay well
    # under the runtime's own shutdown budget.
    DRAIN_TIMEOUT_SECONDS: float = 30.0

    async def run(self, shutdown: asyncio.Event) -> None:
        """Main loop: tick until ``shutdown`` is set, then drain in-flight tasks."""
        config = load_config(self._deps.config_store)
        self._log.info(
            "scheduler.started",
            tick_seconds=config.scheduler.tick_seconds,
            max_concurrent=config.scheduler.max_concurrent_syncs,
        )
        while not shutdown.is_set():
            try:
                await self.tick_once(config=config)
            except Exception:
                self._log.exception("scheduler.tick_errored")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=config.scheduler.tick_seconds)
            # Reload config every tick so overlay toggles (drain, kill switches)
            # take effect next tick.
            config = load_config(self._deps.config_store)
        self._log.info("scheduler.draining", timeout_seconds=self.DRAIN_TIMEOUT_SECONDS)
        still_pending = await self._deps.worker_pool.drain(timeout=self.DRAIN_TIMEOUT_SECONDS)
        if still_pending:
            # A hung adapter (network blackhole, stuck playwright) can
            # prevent drain from completing. Give up cleanly instead of
            # holding SIGTERM open forever.
            self._log.warning("scheduler.drain_timeout", still_pending=len(still_pending))
        self._log.info("scheduler.drained")

    async def tick_once(self, *, config: DaemonConfig | None = None) -> int:
        """Submit every due, eligible candidate. Returns count submitted.

        ``config`` can be passed by the outer loop so a single tick uses
        one coherent snapshot; callers that don't care (tests) get a
        fresh load.

        Emergency stop (``config.scheduler.emergency_stop_requested``)
        short-circuits submission and cancels every in-flight task. The
        overlay flag is cleared once the pool has been stopped so the next
        tick resumes normal operation — operators toggle the flag via
        ``/api/actions/scheduler/emergency_stop``.

        Force-sync requests are consumed first: any row in the
        ``force_sync_requests`` table matches a candidate even if the
        regular due-check says it's not eligible. Rows are deleted as the
        scheduler submits them so a restart doesn't duplicate work.
        """
        if config is None:
            config = load_config(self._deps.config_store)
        # Prune warn-once memory for platforms that just un-paused, so the
        # next pause event logs again. Runs every tick; the set is small.
        self._clear_paused_skip_memory(config)
        if config.scheduler.emergency_stop_requested:
            cancelled = self._deps.worker_pool.emergency_stop()
            self._log.warning("scheduler.emergency_stop", cancelled=cancelled)
            # Clear the flag so a later tick can submit again once the
            # operator flips the switch back off.
            self._deps.config_store.delete_overlay("scheduler.emergency_stop_requested")
            return 0
        if config.scheduler.drain_mode or config.scheduler.global_paused:
            return 0

        forced = set(await list_force_sync_pending(self._deps.sqlite_path))
        candidates = await self._deps.candidate_source.list_candidates()
        # Pre-fetch every (user, platform) last-success timestamp and
        # interval override in a single SELECT each, scoped to this tick.
        # The alternative (one query per candidate inside _eligible) issues
        # 2*N connections per tick, which pinned CPU at ~100 candidates.
        # The snapshots are tick-local: nothing persists them past the
        # method return so a long-lived stale cache can't form.
        last_success: dict[tuple[str, str], datetime] = await last_success_at_bulk(
            self._deps.sqlite_path,
        )
        interval_overrides: dict[tuple[str, str], int] = await user_interval_overrides_bulk(
            self._deps.sqlite_path,
        )
        if self._deps.scope_for_platform is not None:
            # Scope drift detection only makes sense when scope_for_platform
            # is actually wired. In runtime composition today it's None, so
            # this branch is a no-op.
            await self._warn_on_vanished_scopes()
            # Slice 12: clean up orphaned cursors once per scheduler-life
            # (or once per SIGHUP if the operator re-arms it). The
            # _warn_on_vanished_scopes step still runs first so the
            # operator sees one WARN before the prune lands.
            if not self._scope_cleanup_done:
                deleted = await self._run_scope_cleanup_once()
                if deleted:
                    self._log.info("scheduler.scope_cleanup_pass", deleted=deleted)
        submitted = 0
        now = self._deps.now().astimezone(UTC)
        for candidate in candidates:
            key = (candidate.user_email, candidate.platform)
            is_forced = key in forced
            if is_forced:
                # Even a forced sync respects the pool's busy check —
                # firing two runs for the same (user, platform) at once
                # would race on cursor writes. If busy, leave the request
                # row in place so the next tick retries.
                if self._deps.worker_pool.is_busy(*key):
                    continue
                if not self._platform_enabled(config, candidate.platform):
                    # Silently drop: a forced sync on a disabled platform
                    # would surface as a hanging request. Log so the
                    # operator can see why the force didn't land.
                    self._log.warning(
                        "scheduler.force_sync_dropped_platform_disabled",
                        user_email=candidate.user_email,
                        platform=candidate.platform,
                    )
                    await consume_force_sync(self._deps.sqlite_path, *key)
                    continue
                if self._platform_paused(config, candidate.platform):
                    # A paused platform is transient — unlike the kill
                    # switch, leave the force_sync row in place so the
                    # request fires as soon as the operator un-pauses.
                    self._log_paused_skip(candidate.user_email, candidate.platform)
                    continue
            elif not await self._eligible(candidate, config, now, last_success, interval_overrides):
                continue
            task = self._deps.worker_pool.submit(
                user_email=candidate.user_email,
                platform=candidate.platform,
                coro=self._run_for(candidate.user_email, candidate.platform),
            )
            if task is not None:
                submitted += 1
                self._emit(
                    "syncs",
                    {
                        "event": "sync.started",
                        "user_email": candidate.user_email,
                        "platform": candidate.platform,
                        "forced": is_forced,
                        "timestamp": now.isoformat(),
                    },
                )
                if is_forced:
                    await consume_force_sync(self._deps.sqlite_path, *key)
        return submitted

    def _emit(self, channel: str, event: dict[str, object]) -> None:
        """Fan-out helper. No-op when ``event_sink`` is None (tests)."""
        sink = self._deps.event_sink
        if sink is None:
            return
        try:
            sink(channel, event)
        except Exception:
            self._log.exception("scheduler.event_sink_errored", channel=channel)

    async def _warn_on_vanished_scopes(self) -> None:
        """Log one warning per stored scope that's no longer in the current
        ``scope_for_platform`` resolution.

        Slice 12 adds the explicit cleanup pass via
        :meth:`_run_scope_cleanup_once` — this method continues to log
        the WARN so operators see the drift even when cleanup hasn't
        run yet (e.g. cleanup pass disabled because resolver returned
        None for a particular platform).
        """
        resolver = self._deps.scope_for_platform
        if resolver is None:
            return
        stored = await stored_cursor_scopes(self._deps.sqlite_path)
        for (user_email, platform), scopes in stored.items():
            resolved = resolver(platform)
            # ``None`` means "adapter decides" — we can't tell what's
            # active from the scheduler's side, so skip the check.
            if resolved is None:
                continue
            current = {str(s) for s in resolved}
            # ``'default'`` is the pipeline's bucket when scope is None;
            # a cursor written there is not "orphaned" by a shrinking
            # scope set. Keep it until scope bucketing wires end-to-end.
            current.add("default")
            for stale in scopes - current:
                key = (user_email, platform, stale)
                if key in self._scope_vanish_warned:
                    continue
                self._scope_vanish_warned.add(key)
                self._log.warning(
                    "scheduler.scope_vanished",
                    user_email=user_email,
                    platform=platform,
                    scope=stale,
                    active_scopes=sorted(current),
                )

    async def _run_scope_cleanup_once(self) -> int:
        """One-shot cleanup of orphaned ``sync_cursors`` rows per platform.

        Runs lazily on the first tick where ``scope_for_platform`` is
        wired and ``_scope_cleanup_done`` is False. Subsequent ticks
        skip the pass to avoid issuing the same DELETE every second.

        Returns the total rows deleted across every platform. Each
        per-platform call emits ``lifecycle.cursor_pruned`` via the
        scheduler's event sink so dashboards see real-time pruning.
        """
        resolver = self._deps.scope_for_platform
        if resolver is None or self._scope_cleanup_done:
            return 0
        # Lazy import: lifecycle module is light, but circular protection
        # is cheap and consistent with the other lazy imports here.
        from ingestion.daemon.lifecycle.cleanup import (  # noqa: PLC0415
            cleanup_stale_cursors,
        )

        deleted_total = 0
        for spec in iter_platforms():
            resolved = resolver(spec.name)
            if resolved is None:
                # Resolver opts out for this platform — adapter decides
                # the active scopes. Skip rather than risk deleting a
                # cursor we can't characterise.
                continue
            current = {str(s) for s in resolved}
            # The pipeline writes "default" when scope is None. Keep it
            # implicit so the cleanup doesn't drop cursors written
            # before per-scope bucketing wires end-to-end.
            current.add("default")
            try:
                deleted = await cleanup_stale_cursors(
                    self._deps.sqlite_path,
                    platform=spec.name,
                    current_scopes=frozenset(current),
                    event_sink=self._deps.event_sink,
                )
            except Exception:
                self._log.exception(
                    "scheduler.scope_cleanup_errored",
                    platform=spec.name,
                )
                continue
            deleted_total += deleted
        self._scope_cleanup_done = True
        # Reset the warn-once memory so a future un-clean phase (e.g.
        # SIGHUP triggers another pass) re-warns for any new drift.
        self._scope_vanish_warned.clear()
        return deleted_total

    def reset_scope_cleanup(self) -> None:
        """Re-arm the one-shot scope cleanup so the next tick runs it.

        Called from :func:`runtime._on_sighup` so an operator who has
        narrowed a platform's scope set via the overlay can force the
        cleanup pass without restarting the daemon.
        """
        self._scope_cleanup_done = False

    async def _eligible(
        self,
        candidate: Candidate,
        config: DaemonConfig,
        now: datetime,
        last_success: dict[tuple[str, str], datetime],
        interval_overrides: dict[tuple[str, str], int],
    ) -> bool:
        if not self._platform_enabled(config, candidate.platform):
            return False
        if self._platform_paused(config, candidate.platform):
            self._log_paused_skip(candidate.user_email, candidate.platform)
            return False
        if candidate.is_paused:
            return False
        key = (candidate.user_email, candidate.platform)
        if self._deps.worker_pool.is_busy(*key):
            return False
        if await self._deps.dead_letter.is_suppressed(*key):
            return False
        interval = self._resolve_interval(candidate.platform, config, interval_overrides.get(key))
        last = last_success.get(key)
        if last is None:
            return True
        age = (now - last).total_seconds()
        return age >= interval

    def _resolve_interval(
        self,
        platform: str,
        config: DaemonConfig,
        user_override: int | None,
    ) -> int:
        if user_override is not None:
            return user_override
        platform_override = config.scheduler.per_platform_interval_seconds.get(platform)
        if platform_override is not None:
            return platform_override
        return config.scheduler.default_interval_seconds

    async def _run_for(self, user_email: str, platform: str) -> None:
        """Assemble one :func:`run_sync` call.

        The pipeline catches its own exceptions and routes them through
        ``sync_logger.log_failure``, so we can't distinguish hard failure
        from partial success by looking at the returned ``SyncStats`` alone.
        Instead we query the just-written ``sync_history`` row by its
        ``sync_id``: if its ``state`` is ``'failed'`` we raise so the
        worker pool's ``on_finish`` callback hands the exception to the
        dead-letter tracker. Keying on ``sync_id`` rather than "most
        recent by ``started_at``" avoids a race where a concurrent retry
        or clock jump could return a stale row's outcome. Partial success
        (``state='complete'`` with per-scope errors on the ``FetchResult``)
        does NOT dead-letter a user.
        """
        with bind_sync_context(user_email=user_email, platform=platform):
            credentials = await self._deps.token_store.get_credentials(user_email, platform)
            if credentials is None:
                self._log.warning("scheduler.missing_credentials")
                raise RuntimeError(f"no credentials for {user_email} on {platform}")

            adapter = self._deps.adapter_factory(platform)
            scope = None
            if self._deps.scope_for_platform is not None:
                scope = self._deps.scope_for_platform(platform)

            _, sync_id = await run_sync(
                adapter=adapter,
                credentials=credentials,
                content_sink=self._deps.content_sink,
                sync_state=self._deps.sync_state,
                sync_logger=self._deps.sync_logger,
                user_id=user_email,
                scope=scope,
                token_store=self._deps.token_store,
            )
            state = await sync_state_by_id(self._deps.sqlite_path, sync_id)
            if state == "failed":
                raise RuntimeError(
                    f"run_sync failed for {user_email}/{platform} (sync_id={sync_id})"
                )

    def _platform_enabled(self, config: DaemonConfig, platform: str) -> bool:
        # Registry-backed: PlatformSpec.enabled_config_attr names the field
        # on PlatformToggles. Unknown platforms default to disabled (safer
        # than guessing) and emit one warning per process so a new platform
        # added to Postgres before a code deploy doesn't silently never sync.
        try:
            spec = get_platform(platform)
        except UnknownPlatformError:
            if platform not in self._unknown_platforms_seen:
                self._unknown_platforms_seen.add(platform)
                self._log.warning("scheduler.unknown_platform", platform=platform)
            return False
        return bool(getattr(config.platforms, spec.enabled_config_attr))

    def _platform_paused(self, config: DaemonConfig, platform: str) -> bool:
        """Return True when the V10 ops pause is set for ``platform``.

        Distinct from ``_platform_enabled`` (kill switch) — a paused platform
        is transient: credentials remain valid, forced syncs stay enqueued,
        and the dashboard can flip the flag back off without a redeploy.
        Unknown platforms are never paused (the kill-switch path already
        rejects them).
        """
        try:
            spec = get_platform(platform)
        except UnknownPlatformError:
            return False
        return bool(getattr(config.platforms.paused, spec.paused_config_attr))

    def _log_paused_skip(self, user_email: str, platform: str) -> None:
        """INFO log on the first tick a (user, platform) is skipped under
        an active pause. The warn-once entry is cleared when the pause
        lifts (via ``_clear_paused_skip_memory``) so a later re-pause
        emits a fresh log line."""
        key = (user_email, platform)
        if key in self._paused_skipped_logged:
            return
        self._paused_skipped_logged.add(key)
        self._log.info(
            "sync_paused",
            user_email=user_email,
            platform=platform,
            reason="config.overlay",
        )

    def _clear_paused_skip_memory(self, config: DaemonConfig) -> None:
        """Drop warn-once entries for platforms that are no longer paused.

        Called once per tick. Without this, un-pausing then re-pausing
        a platform would silently suppress the re-pause log. Cheap: the
        set is bounded by the number of (user, platform) pairs.
        """
        if not self._paused_skipped_logged:
            return
        # Iterate the registry instead of a hardcoded tuple so adding a
        # fourth platform's pause clean-up requires no edit here.
        still_paused = {
            spec.name for spec in iter_platforms() if self._platform_paused(config, spec.name)
        }
        self._paused_skipped_logged = {
            (user, platform)
            for (user, platform) in self._paused_skipped_logged
            if platform in still_paused
        }
