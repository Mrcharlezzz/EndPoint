"""Daemon lifecycle: startup, background loops, graceful SIGTERM shutdown.

Runtime composition grows incrementally:
    - Phase 0: stub tick loop, SIGTERM handling, Fernet + config + SQLite.
    - Phase 3: Teams refresh loop runs alongside the tick loop and the
      Playwright pool. If ``ENDPOINT_DAEMON__TEAMS_REFRESH__ENABLED=false``
      is set, the refresh loop is skipped.
    - Phase 4: SqliteLogHandler attached to the root logger so every
      structlog record lands in the ``logs`` SQLite table; retention loop
      prunes old rows; alert engine ticks every 30s. Each is independently
      gated via config so CI and sandbox runs can disable them.
    - Phase 5+: the scheduler task will land here too.

Ports that hit Postgres are imported lazily inside the task factories because
``core.models`` requires ``core.bootstrap.init()`` first; ``_bootstrap``
runs that before any task is scheduled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import core.bootstrap

from ingestion.clients.pool import AsyncHTTPClientPool
from ingestion.daemon import lockfile
from ingestion.daemon.api.pubsub import EventBus
from ingestion.daemon.config import DaemonConfig, load_config
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.logging import configure_logging, get_logger
from ingestion.daemon.store.conn_pool import AsyncConnectionPool
from ingestion.daemon.store.local import LocalStore, open_store

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.daemon.alerts.engine import AlertNotifier
    from ingestion.daemon.store.logs import SqliteLogHandler
    from ingestion.daemon.worker import WorkerPool
    from ingestion.ports import Adapter


@dataclass(frozen=True)
class Runtime:
    config: DaemonConfig
    store: LocalStore
    cipher: CredentialCipher
    shutdown: asyncio.Event
    # One EventBus for the whole daemon so the log handler, scheduler,
    # and API share the same pub/sub surface. The bus is created at
    # _bootstrap time but its event loop is attached in run() once the
    # asyncio loop is available.
    events: EventBus = field(default_factory=EventBus)
    # Worker pool is created inside _start_scheduler but the final shutdown
    # path needs a handle to force-stop any task that outlasts the scheduler's
    # drain timeout. Stored in a list so the dataclass can stay frozen and
    # the scheduler factory mutates the list content instead of rebinding.
    worker_pools: list[WorkerPool] = field(default_factory=lambda: [])
    # Lockfile guarding exclusive access to the SQLite state. ``None`` when
    # the path is in-memory (each instance owns a private DB).
    lock_path: Path | None = None
    # Shared HTTP client pool keyed on (host, base_url). Adapters keep their
    # per-call construction in this slice; the pool is registered here so a
    # later carve-out can opt them into ``acquire`` / ``release`` without
    # touching adapter wiring. Drained in :func:`run` shutdown.
    http_pool: AsyncHTTPClientPool = field(default_factory=AsyncHTTPClientPool)
    # Bounded async pool of aiosqlite connections to the daemon SQLite file.
    # Set in :func:`_bootstrap` once the path is known; used by hot write
    # paths (dead_letter, audit, force_sync) to skip the per-call open
    # cost. Stored in a list so the dataclass can stay frozen.
    sqlite_pool: list[AsyncConnectionPool] = field(default_factory=lambda: [])
    # Scheduler instances registered after spawn. Held in a list (mutable
    # through a frozen dataclass) so the SIGHUP handler can re-arm the
    # scheduler's scope cleanup without owning a separate registry.
    schedulers: list[object] = field(default_factory=lambda: [])


def _bootstrap(sqlite_path: Path | None = None) -> Runtime:
    """Do all startup work that can fail synchronously.

    Kept separate from :func:`run` so tests can assemble a runtime without
    owning the asyncio loop.

    Logging is configured FIRST so any subsequent bootstrap failure lands in
    structured JSON on stdout instead of the stdlib default. The level is
    re-applied after config loads in case the overlay or env raises it.
    """
    log_level_env = os.environ.get("ENDPOINT_DAEMON__LOG_LEVEL", "INFO")
    configure_logging(log_level_env)
    core.bootstrap.init()
    # Fail loud at startup if a PlatformSpec references a config field
    # that no longer exists on PlatformToggles / PlatformPauseConfig.
    # Without this the registry would silently return False on every
    # tick and the platform would never sync.
    from ingestion.daemon.platforms import validate_registry  # noqa: PLC0415

    validate_registry()
    cipher = CredentialCipher.from_env()
    base = DaemonConfig() if sqlite_path is None else DaemonConfig(sqlite_path=sqlite_path)
    # Lock BEFORE the store opens so a stale lockfile from a crashed
    # predecessor is the very first thing the new process touches. Anything
    # else (apply_migrations, WAL checkpoint) would race a sibling daemon.
    lock_path = lockfile.acquire(base.sqlite_path)
    try:
        store = open_store(base.sqlite_path)
    except BaseException:
        lockfile.release(lock_path)
        raise
    try:
        # Thread the base config into load_config so the sqlite_path we just
        # opened persists after the overlay is applied.
        config = load_config(store, base=base)
        if config.log_level != log_level_env:
            configure_logging(config.log_level)
        return Runtime(
            config=config,
            store=store,
            cipher=cipher,
            shutdown=asyncio.Event(),
            lock_path=lock_path,
        )
    except BaseException:
        # Any failure between opening the store and returning the Runtime
        # means the caller never gets a handle to close it. Release here.
        store.close()
        lockfile.release(lock_path)
        raise


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> None:
    log = get_logger(__name__)

    def _signal(sig: signal.Signals) -> None:
        log.info("daemon.signal_received", signal=sig.name)
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows and some restricted environments don't support loop signal
        # handlers. Fall back to Python's default handling there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal, sig)


def _on_sighup(runtime: Runtime) -> None:
    """SIGHUP handler: re-read overlay, log diff, re-arm scope cleanup.

    Most of the daemon's runtime state already polls the overlay every
    tick (the scheduler reloads on each cycle, the content sink calls
    ``load_config`` on every write). SIGHUP forces a fresh read here
    so:

    * the overlay's "now" snapshot lands in :attr:`Runtime.config`
      where the Sentry init / static-config consumers can see it;
    * the scheduler's one-shot scope cleanup re-arms so an operator
      who narrowed a platform's FetchScope set via the overlay gets
      a cleanup pass without restarting the daemon;
    * a structured ``runtime.config_reloaded`` log line surfaces what
      changed so operators can audit the SIGHUP.

    Some changes (``sqlite_path``, the Fernet key, the log level set
    at process start) STILL require a restart — the open store / loop
    handle / formatter were bound at boot and are not re-created
    here. We log a warning when such a change is detected so the
    operator knows the SIGHUP only partially landed.

    Shutdown safety: if shutdown is already set, this is a no-op.
    Re-reading state during shutdown would race the lifecycle order
    that closes the store + lockfile.
    """
    log = get_logger(__name__)
    if runtime.shutdown.is_set():
        log.info("runtime.sighup_ignored_during_shutdown")
        return
    try:
        old_dump = runtime.config.model_dump()
        new_config = load_config(runtime.store, base=runtime.config)
        new_dump = new_config.model_dump()
    except Exception:
        # Bad overlay shouldn't take the daemon down — keep running with
        # the prior config and log the failure.
        log.exception("runtime.config_reload_failed")
        return
    diff = _diff_config(old_dump, new_dump)
    # Re-arm the scheduler's scope cleanup BEFORE the diff branch so a
    # SIGHUP without any overlay changes still triggers a fresh cleanup
    # pass — operators send SIGHUP precisely to ask for a re-evaluation,
    # so we should honor that intent even when nothing else changed.
    for scheduler in runtime.schedulers:
        reset = getattr(scheduler, "reset_scope_cleanup", None)
        if reset is not None:
            try:
                reset()
            except Exception:
                log.exception("runtime.scheduler_reset_errored")
    if not diff:
        log.info("runtime.config_reloaded", changes=0)
        return
    # Boot-time fields that won't take effect without a restart.
    boot_only = {key for key in diff if key in {"sqlite_path", "log_level", "api.host", "api.port"}}
    if boot_only:
        log.warning(
            "runtime.config_reload_partial",
            boot_only=sorted(boot_only),
            message="some changes require a daemon restart to take effect",
        )
    # Mutate the frozen Runtime by going through object.__setattr__ —
    # the dataclass is frozen for accidental mutation safety, but the
    # SIGHUP path is the one place we deliberately swap the snapshot.
    object.__setattr__(runtime, "config", new_config)
    log.info(
        "runtime.config_reloaded",
        changes=len(diff),
        keys=sorted(diff.keys()),
    )


def _diff_config(
    old: dict[str, object], new: dict[str, object]
) -> dict[str, tuple[object, object]]:
    """Return a flat dotted-key diff between two config dumps.

    Nested dicts are flattened to dotted paths so the caller can list
    exactly the keys that changed. Equal values are skipped.
    """
    out: dict[str, tuple[object, object]] = {}
    _flat_diff(old, new, "", out)
    return out


def _flat_diff(
    old: object,
    new: object,
    prefix: str,
    out: dict[str, tuple[object, object]],
) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        # Cast through Any so pyright doesn't degrade the keys / values to
        # "Unknown" when the inputs are bare ``dict[str, object]``. The
        # _flat_diff signature accepts ``object`` so the recursion is
        # type-checked even with the cast.
        from typing import Any, cast  # noqa: PLC0415

        old_dict = cast("dict[str, Any]", old)
        new_dict = cast("dict[str, Any]", new)
        keys: set[str] = set(old_dict.keys()) | set(new_dict.keys())
        for key in keys:
            child_prefix = f"{prefix}.{key}" if prefix else key
            _flat_diff(old_dict.get(key), new_dict.get(key), child_prefix, out)
        return
    if old != new:
        out[prefix] = (old, new)


def _install_sighup_handler(loop: asyncio.AbstractEventLoop, runtime: Runtime) -> None:
    """Register the SIGHUP callback on ``loop`` if the platform supports it.

    POSIX-only (Windows raises NotImplementedError on
    ``loop.add_signal_handler`` for SIGHUP). The handler runs on the
    asyncio loop, so blocking calls inside ``_on_sighup`` are
    discouraged — the helper sticks to in-memory dict diffs and
    structured logging.
    """
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(sighup, _on_sighup, runtime)


async def _tick_loop(runtime: Runtime) -> None:
    log = get_logger(__name__)
    log.info(
        "daemon.started",
        sqlite_path=str(runtime.config.sqlite_path),
        log_level=runtime.config.log_level,
    )
    # Shadow mode silently suppresses content writes, so booting with it
    # set is the kind of thing that gets left on by accident after an
    # aborted test. Emit a WARNING at startup so the first thing in the
    # log file tells the operator their content isn't landing.
    if runtime.config.ops.shadow_mode:
        log.warning(
            "daemon.shadow_mode_active",
            message=("daemon booted with ops.shadow_mode=true — content writes are suppressed"),
        )
    interval = runtime.config.scheduler.tick_seconds
    while not runtime.shutdown.is_set():
        try:
            await asyncio.wait_for(runtime.shutdown.wait(), timeout=interval)
        except TimeoutError:
            log.debug("daemon.tick")
    log.info("daemon.shutting_down")


async def _start_refresh_loop(
    runtime: Runtime,
) -> tuple[asyncio.Task[None] | None, dict[str, asyncio.Lock] | None]:
    """Compose and spawn the Teams refresh loop, or return ``(None, None)`` if disabled.

    Returns ``(task, user_locks)``. The locks dict is shared with the
    API server so the bulk cycle and the on-demand refresh action
    serialise per-user.

    The refresh path uses :class:`HttpxTokenRefresher` — pure HTTPS
    against ``login.microsoftonline.com``, no browser. Disabled
    altogether when ``teams_refresh.enabled`` is False.
    """
    log = get_logger(__name__)
    refresh_cfg = runtime.config.teams_refresh
    if not refresh_cfg.enabled:
        log.info("refresh_loop.disabled_by_config")
        return None, None

    # Lazy imports: these modules reach into core.models which must only be
    # touched after core.bootstrap.init() ran in _bootstrap.
    from ingestion.config import IngestionConfig
    from ingestion.daemon.auth.moodle_strategy import MoodleRefreshStrategy
    from ingestion.daemon.auth.notifier import LogOnlyNotifier
    from ingestion.daemon.auth.refresh_loop import RefreshLoopDeps, TeamsRefreshLoop
    from ingestion.daemon.auth.teams_strategy import TeamsRefreshStrategy
    from ingestion.daemon.auth.telegram_strategy import TelegramRefreshStrategy
    from ingestion.daemon.platforms import register_refresh_strategy
    from ingestion.daemon.ports.candidate_source import PostgresCandidateSource
    from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
    from ingestion.daemon.ports.token_store import PostgresTokenStore

    # Slice 8: register every platform's refresh strategy on its
    # PlatformSpec before the polymorphic loop walks the registry.
    # Each register call is idempotent (no-op on identical instance).
    ingestion_config = IngestionConfig()
    register_refresh_strategy(
        "teams",
        TeamsRefreshStrategy(timeout_seconds=refresh_cfg.timeout_seconds),
    )
    register_refresh_strategy(
        "moodle",
        MoodleRefreshStrategy(ingestion_config.moodle),
    )
    register_refresh_strategy(
        "telegram",
        TelegramRefreshStrategy(ingestion_config.telegram),
    )

    user_locks: dict[str, asyncio.Lock] = {}
    token_store = PostgresTokenStore(runtime.cipher, notifier=LogOnlyNotifier())
    candidate_source = PostgresCandidateSource()
    loop_instance = TeamsRefreshLoop(
        RefreshLoopDeps(
            token_store=token_store,
            candidate_source=candidate_source,
            interval_seconds=refresh_cfg.interval_seconds,
            timeout_seconds=refresh_cfg.timeout_seconds,
            max_retries_per_user=refresh_cfg.max_retries_per_user,
            concurrency=refresh_cfg.concurrency,
            # Piggyback refresh runs onto sync_history so the V6 Credentials
            # page's "Refresh activity" tab shares the V2 Syncs UI.
            sync_logger=SqliteSyncLogger(runtime.config.sqlite_path),
            user_locks=user_locks,
        )
    )
    log.info(
        "refresh_loop.starting",
        interval_seconds=refresh_cfg.interval_seconds,
    )
    task = asyncio.create_task(loop_instance.run(runtime.shutdown), name="teams-refresh-loop")
    return task, user_locks


def _maybe_install_log_handler(runtime: Runtime) -> SqliteLogHandler | None:
    """Attach the SqliteLogHandler unless the operator opted out.

    Returned handle is opaque to the caller; runtime only needs to close it
    on shutdown to release the SQLite connection. The handler publishes
    each persisted row to the shared EventBus on the "logs" channel so
    /ws/logs subscribers see a live stream.
    """
    obs = runtime.config.observability
    if not obs.log_to_sqlite:
        return None
    from ingestion.daemon.store.logs import install_sqlite_log_handler

    events = runtime.events

    def _fanout(row: dict[str, object]) -> None:
        # The log handler runs on its dedicated writer thread; use the
        # threadsafe variant so the event bus schedules on the loop.
        events.publish_threadsafe("logs", row)

    return install_sqlite_log_handler(runtime.config.sqlite_path, on_write=_fanout)


async def _start_scheduler(runtime: Runtime) -> asyncio.Task[None] | None:
    """Compose the scheduler and spawn it. Returns ``None`` when disabled.

    The full composition happens here so lazy imports stay contained:
    Django-touching modules must only load after :func:`core.bootstrap.init`
    ran during ``_bootstrap``.
    """
    log = get_logger(__name__)
    scheduler_cfg = runtime.config.scheduler
    if not scheduler_cfg.enabled:
        log.info("scheduler.disabled_by_config")
        return None

    from ingestion.config import IngestionConfig
    from ingestion.daemon.auth.notifier import LogOnlyNotifier
    from ingestion.daemon.dead_letter import DeadLetterTracker
    from ingestion.daemon.ports.candidate_source import PostgresCandidateSource
    from ingestion.daemon.ports.content_sink import PostgresContentSink
    from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
    from ingestion.daemon.ports.sync_state import SqliteSyncStateStore
    from ingestion.daemon.ports.token_store import PostgresTokenStore
    from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
    from ingestion.daemon.worker import WorkerPool

    # IngestionConfig reads ``ENDPOINT_<platform>__*`` env vars with
    # pydantic-settings. Live operator policy is applied by wrapping the
    # adapter after construction, not by mutating platform config.
    base_ingestion_config = IngestionConfig()

    def _adapter_factory(platform: str) -> Adapter:
        # Delegates to the central registry. Re-raised UnknownPlatformError
        # surfaces as a ValueError-shaped failure for callers that already
        # caught the literal-dict KeyError pattern (UnknownPlatformError
        # inherits KeyError so the existing scheduler-side fallback path
        # in _platform_enabled still works).
        from ingestion.daemon.ingestion_controls import build_live_adapter  # noqa: PLC0415
        from ingestion.daemon.platforms import (  # noqa: PLC0415 — local import keeps runtime module light
            UnknownPlatformError,
            make_adapter,
        )

        try:
            adapter = make_adapter(platform, base_ingestion_config)
            daemon_config = load_config(runtime.store)
            return build_live_adapter(adapter, daemon_config)
        except UnknownPlatformError as exc:
            raise ValueError(f"no adapter registered for platform {platform!r}") from exc

    # Pass the shared sqlite pool when one is set up (run() warms it before
    # spawning the scheduler). Falls back to per-call aiosqlite.connect when
    # the pool is absent — keeps callers that build a Scheduler without
    # going through run() (e.g. tests) working unchanged.
    dl_pool = runtime.sqlite_pool[0] if runtime.sqlite_pool else None
    dead_letter = DeadLetterTracker(
        runtime.config.sqlite_path,
        threshold=scheduler_cfg.dead_letter_threshold,
        cooldown_seconds=scheduler_cfg.dead_letter_cooldown_seconds,
        pool=dl_pool,
        # Slice 10: the EventBus is the alert engine's subscription
        # surface. Direct ``publish`` works here because the
        # dead-letter writes happen on the event loop already; no
        # threadsafe wrapper needed.
        event_publisher=runtime.events.publish,
    )

    async def on_finish(user: str, platform: str, exc: BaseException | None) -> None:
        # Cancellation is never reported via on_finish (worker.py short-
        # circuits the callback on CancelledError), but guard anyway in
        # case a future caller passes one through: an operator-initiated
        # emergency stop must not poison the dead-letter counter.
        if isinstance(exc, asyncio.CancelledError):
            return
        if exc is None:
            await dead_letter.record_success(user, platform)
            runtime.events.publish_threadsafe(
                "syncs",
                {"event": "sync.finished", "user_email": user, "platform": platform},
            )
        else:
            await dead_letter.record_failure(user, platform)
            failure_event = {
                "event": "sync.failed",
                "user_email": user,
                "platform": platform,
                "error": str(exc)[:500],
            }
            # Publish to both channels so clients can subscribe to either
            # the general sync stream or the error-only stream without
            # having to mirror events clientside.
            runtime.events.publish_threadsafe("syncs", failure_event)
            runtime.events.publish_threadsafe("errors", failure_event)

    pool = WorkerPool(
        global_limit=scheduler_cfg.max_concurrent_syncs,
        per_platform_limits=dict(scheduler_cfg.per_platform_max_concurrent),
        per_user_limit=scheduler_cfg.max_syncs_per_user,
        on_finish=on_finish,
    )
    runtime.worker_pools.append(pool)
    # Bind the publish method so the scheduler can fan-out events
    # without reaching into runtime.events itself. publish_threadsafe is
    # safe to call from any loop/thread.
    events = runtime.events
    deps = SchedulerDeps(
        config_store=runtime.store,
        candidate_source=PostgresCandidateSource(),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=PostgresTokenStore(runtime.cipher, notifier=LogOnlyNotifier()),
        content_sink=PostgresContentSink(
            # Poll the live overlay on every write so an operator flip from
            # the V10 ops pane takes effect without a daemon restart. Re-
            # reading the config is cheap: load_config hits the in-memory
            # overlay read path, not disk per-call.
            is_shadow_mode=lambda: load_config(runtime.store).ops.shadow_mode,
        ),
        sync_state=SqliteSyncStateStore(runtime.config.sqlite_path),
        sync_logger=SqliteSyncLogger(runtime.config.sqlite_path),
        adapter_factory=_adapter_factory,
        sqlite_path=runtime.config.sqlite_path,
        event_sink=events.publish_threadsafe,
    )
    scheduler = Scheduler(deps)
    runtime.schedulers.append(scheduler)
    log.info(
        "scheduler.starting",
        max_concurrent=scheduler_cfg.max_concurrent_syncs,
        tick_seconds=scheduler_cfg.tick_seconds,
    )
    return asyncio.create_task(scheduler.run(runtime.shutdown), name="scheduler")


async def _start_api_server(
    runtime: Runtime,
    *,
    user_refresh_locks: dict[str, asyncio.Lock] | None = None,
) -> asyncio.Task[None] | None:
    """Run uvicorn serving the FastAPI control-panel app.

    Shutdown is cooperative: the shared shutdown event flips
    ``server.should_exit`` so uvicorn leaves its accept loop and drains
    in-flight requests. Force-exit kicks in if drain hangs past 10s.

    ``user_refresh_locks`` is the per-user lock dict shared with the
    refresh loop, so the on-demand force-refresh action and the bulk
    cycle serialise on the same locks.
    """
    log = get_logger(__name__)
    api_cfg = runtime.config.api
    if not api_cfg.enabled:
        log.info("api.disabled_by_config")
        return None

    import uvicorn

    from ingestion.daemon.api import create_app

    app = create_app(
        config=runtime.config,
        store=runtime.store,
        cipher=runtime.cipher,
        events=runtime.events,
        user_refresh_locks=user_refresh_locks,
    )
    server_config = uvicorn.Config(
        app,
        host=api_cfg.host,
        port=api_cfg.port,
        log_config=None,
        lifespan="on",
    )
    server = uvicorn.Server(server_config)

    async def _driver() -> None:
        serve_task = asyncio.create_task(server.serve(), name="api-uvicorn")
        try:
            await runtime.shutdown.wait()
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=10.0)
            except TimeoutError:
                server.force_exit = True
                with contextlib.suppress(Exception):
                    await serve_task

    log.info("api.starting", host=api_cfg.host, port=api_cfg.port)
    return asyncio.create_task(_driver(), name="api-server")


async def _start_lifecycle_loops(runtime: Runtime) -> list[asyncio.Task[None]]:
    """Spawn the slice-12 lifecycle prune loop.

    Returns an empty list when every TTL is set to ``0`` (the kill
    switch path). The single loop function handles all three tables
    internally; we don't split into one task per table because the
    SQLite write lock would still serialise them.
    """
    log = get_logger(__name__)
    cfg = runtime.config.lifecycle
    if cfg.interval_seconds <= 0:
        log.info("lifecycle_prune_loop.disabled_by_config")
        return []
    if (
        cfg.sync_history_retention_days <= 0
        and cfg.dead_letter_retention_days <= 0
        and cfg.audit_log_retention_days <= 0
    ):
        log.info("lifecycle_prune_loop.no_tables_enabled")
        return []
    from ingestion.daemon.lifecycle.loops import run_lifecycle_prune_loop

    return [
        asyncio.create_task(
            run_lifecycle_prune_loop(
                runtime.shutdown,
                db_path=runtime.config.sqlite_path,
                sync_history_retention_days=cfg.sync_history_retention_days,
                dead_letter_retention_days=cfg.dead_letter_retention_days,
                audit_log_retention_days=cfg.audit_log_retention_days,
                interval_seconds=cfg.interval_seconds,
            ),
            name="lifecycle-prune-loop",
        )
    ]


def _build_alert_notifier(runtime: Runtime) -> AlertNotifier | None:
    """Pick the :class:`AlertNotifier` for this runtime.

    Returns ``None`` (engine falls back to its built-in null notifier)
    when ``alerts.webhook_url`` is empty. Returns a configured
    :class:`WebhookAlertNotifier` otherwise. Construction failures (bad
    URL, invalid format) log + fall back to ``None`` so a typo in the
    overlay doesn't take down the whole alert loop.
    """
    log = get_logger(__name__)
    cfg = runtime.config.alerts
    if not cfg.webhook_url:
        return None
    from ingestion.daemon.alerts.notifiers import WebhookAlertNotifier

    try:
        return WebhookAlertNotifier(
            url=cfg.webhook_url,
            token=cfg.webhook_token,
            notifier_format=cfg.notifier_format,
            db_path=runtime.config.sqlite_path,
        )
    except ValueError:
        log.exception(
            "alerts.notifier_misconfigured",
            notifier_format=cfg.notifier_format,
        )
        return None


async def _start_observability_loops(runtime: Runtime) -> list[asyncio.Task[None]]:
    obs = runtime.config.observability
    alerts_cfg = runtime.config.alerts
    tasks: list[asyncio.Task[None]] = []
    from ingestion.daemon.observability_loops import (
        run_alert_engine_loop,
        run_idempotency_prune_loop,
        run_metrics_publisher_loop,
        run_retention_loop,
    )

    log = get_logger(__name__)

    if obs.retention_days > 0:
        tasks.append(
            asyncio.create_task(
                run_retention_loop(
                    runtime.shutdown,
                    db_path=runtime.config.sqlite_path,
                    retention_days=obs.retention_days,
                    interval_seconds=obs.retention_interval_seconds,
                ),
                name="retention-loop",
            )
        )
    if obs.alert_tick_seconds > 0:
        # Slice 13: seed default rules + pick the right notifier. Both
        # are gated by config so disabling the alert engine entirely
        # (interval=0) skips both. seed_default_rules is idempotent so
        # restart loops on a misconfigured system don't duplicate rules.
        if alerts_cfg.seed_default_rules:
            from ingestion.daemon.alerts.seed import seed_default_rules

            try:
                inserted = await seed_default_rules(runtime.config.sqlite_path)
                if inserted:
                    log.info("alerts.default_rules_seeded", names=inserted)
            except Exception:
                log.exception("alerts.default_rules_seed_errored")
        notifier = _build_alert_notifier(runtime)
        tasks.append(
            asyncio.create_task(
                run_alert_engine_loop(
                    runtime.shutdown,
                    db_path=runtime.config.sqlite_path,
                    interval_seconds=obs.alert_tick_seconds,
                    event_sink=runtime.events.publish_threadsafe,
                    notifier=notifier,
                ),
                name="alert-engine-loop",
            )
        )
    # Slice 13: hourly online-safe SQLite backup. Gated on
    # ``observability.backup_enabled``. Same shutdown event as the
    # other loops so SIGTERM cancels the backup mid-run if needed.
    if obs.backup_enabled:
        from ingestion.daemon.store.backup import resolve_backup_dir, run_backup_loop

        backup_dir = resolve_backup_dir(
            sqlite_path=runtime.config.sqlite_path,
            override=obs.backup_dir,
        )
        tasks.append(
            asyncio.create_task(
                run_backup_loop(
                    runtime.shutdown,
                    sqlite_path=runtime.config.sqlite_path,
                    backup_dir=backup_dir,
                    interval_seconds=obs.backup_interval_seconds,
                    retention_count=obs.backup_retention_count,
                    event_sink=runtime.events.publish_threadsafe,
                ),
                name="backup-loop",
            )
        )
    # Metrics publisher piggybacks on the first worker pool the scheduler
    # creates. The scheduler is spawned after observability, so the pool
    # handle is fetched lazily — if scheduling is disabled there's no pool
    # and we skip metrics entirely.
    if runtime.worker_pools:
        tasks.append(
            asyncio.create_task(
                run_metrics_publisher_loop(
                    runtime.shutdown,
                    pool=runtime.worker_pools[0],
                    db_path=runtime.config.sqlite_path,
                    event_sink=runtime.events.publish_threadsafe,
                ),
                name="metrics-publisher-loop",
            )
        )
    # Idempotency cache prune. Runs hourly so the idempotency_log
    # table doesn't grow unbounded. Only spawned when the daemon's
    # idempotency cache is actually enabled — ttl_seconds=0 means
    # the middleware bypasses the cache so there's nothing to prune.
    if runtime.config.api.idempotency_ttl_seconds > 0:
        tasks.append(
            asyncio.create_task(
                run_idempotency_prune_loop(
                    runtime.shutdown,
                    db_path=runtime.config.sqlite_path,
                ),
                name="idempotency-prune-loop",
            )
        )
    return tasks


async def run(sqlite_path: Path | None = None) -> None:
    """Start the daemon and block until SIGTERM/SIGINT."""
    runtime = _bootstrap(sqlite_path=sqlite_path)
    loop = asyncio.get_running_loop()
    runtime.events.attach_loop(loop)
    # Spin up the aiosqlite connection pool now that we own the loop. Warming
    # the first 2 connections eagerly so the very first hot-path acquire
    # doesn't pay the open cost. Stored in runtime.sqlite_pool so the
    # shutdown path can drain it.
    sqlite_pool = AsyncConnectionPool(runtime.config.sqlite_path)
    await sqlite_pool.warm()
    runtime.sqlite_pool.append(sqlite_pool)
    # Flip any sync_history row left in 'running' by a prior process that
    # died before finishing (SIGKILL, OOM, power loss, `compose down -t 0`).
    # Runs before workers start so nothing is actually in-flight yet.
    from ingestion.daemon.ports.sync_logger import SqliteSyncLogger

    reconciled = await SqliteSyncLogger(runtime.config.sqlite_path).reconcile_orphaned_running()
    if reconciled:
        get_logger(__name__).info(
            "sync_history.orphans_reconciled",
            count=reconciled,
        )
    _install_signal_handlers(loop, runtime.shutdown)
    _install_sighup_handler(loop, runtime)
    from ingestion.daemon.sentry import init_sentry

    if init_sentry(runtime.config.sentry):
        get_logger(__name__).info(
            "sentry.initialised",
            environment=runtime.config.sentry.environment,
        )
    log_handler = _maybe_install_log_handler(runtime)
    refresh_task, user_refresh_locks = await _start_refresh_loop(runtime)
    # Scheduler first so ``runtime.worker_pools`` is populated by the time
    # the metrics publisher loop looks for a pool handle.
    scheduler_task = await _start_scheduler(runtime)
    observability_tasks = await _start_observability_loops(runtime)
    # Slice 12 lifecycle prune loop. Spawned alongside observability so
    # the same shutdown event drains it during SIGTERM. Kept separate
    # from observability_loops in source so a Slice 13 alert subscriber
    # can listen for ``lifecycle.user_deleted`` events without the
    # cleanup module pulling alerts in.
    lifecycle_tasks = await _start_lifecycle_loops(runtime)
    api_task = await _start_api_server(
        runtime,
        user_refresh_locks=user_refresh_locks,
    )
    try:
        await _tick_loop(runtime)
    finally:
        # Stop the background tasks before closing the stores so a log
        # call racing with shutdown doesn't hit a closed handler.
        background = [*observability_tasks, *lifecycle_tasks]
        if refresh_task is not None:
            background.append(refresh_task)
        if scheduler_task is not None:
            background.append(scheduler_task)
        if api_task is not None:
            background.append(api_task)
        for task in background:
            if not task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, timeout=15.0)
        # Force-stop any worker that outlasted the scheduler's drain — a
        # wedged adapter would otherwise leak asyncio tasks as the loop
        # closes, and the SqliteLogHandler could GC-close mid-write.
        for pool in runtime.worker_pools:
            with contextlib.suppress(Exception):
                await pool.close()
        # Drain pooled HTTP clients before the loop closes so keep-alive
        # connections shut down cleanly. Idempotent: safe even when nothing
        # was ever acquired.
        with contextlib.suppress(Exception):
            await runtime.http_pool.close_all()
        # Drain pooled aiosqlite connections so the loop can close cleanly.
        # Order matters: log_handler closes its own thread-owned connection
        # next, and the LocalStore close after handles the synchronous one.
        for pool in runtime.sqlite_pool:
            with contextlib.suppress(Exception):
                await pool.close_all()
        if log_handler is not None:
            with contextlib.suppress(Exception):
                log_handler.close()
        runtime.store.close()
        with contextlib.suppress(Exception):
            lockfile.release(runtime.lock_path)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
