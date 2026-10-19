"""Layered daemon config: env vars overlaid by rows in the local SQLite.

Precedence, lowest to highest:
    1. hard-coded defaults on :class:`DaemonConfig`
    2. env vars with prefix ``ENDPOINT_DAEMON__`` (pydantic-settings)
    3. rows in the ``config_overlay`` SQLite table, keyed by the dotted setting path

The overlay exists so operators can flip kill switches, pause users, or adjust
schedules from the control panel without restarting the process. Env vars are
for boot-time infrastructure (DB host, fernet key, admin password).

Keys in the overlay use the same dotted path shape as pydantic model fields,
e.g. ``scheduler.global_paused`` or ``platforms.teams_enabled``. Typos or
values that fail pydantic validation are logged (see :func:`_apply_overlay`)
and skipped so a stale overlay row can't crash startup.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from ingestion.daemon.store.local import LocalStore

_log = structlog.get_logger(__name__)

DEFAULT_SQLITE_PATH = Path("/var/lib/ingestor/state.db")


class SchedulerConfig(BaseModel):
    # Hard disable: ``False`` skips the scheduler task entirely at runtime
    # startup. Useful for sandbox runs and the scaffolding subprocess test
    # where we don't want Postgres or adapter HTTP calls firing.
    enabled: bool = True
    global_paused: bool = False
    drain_mode: bool = False
    # Set by /api/actions/scheduler/emergency_stop. The scheduler checks
    # this on each tick, cancels every in-flight task, then clears the
    # flag. Never persists to overlay between daemon restarts because the
    # scheduler clears it on consumption.
    emergency_stop_requested: bool = False
    tick_seconds: float = 1.0
    max_concurrent_syncs: int = 8
    # Per-user cap: at most N concurrent syncs across all platforms for a
    # single user. A user with 10k items on one platform can no longer
    # monopolise the global pool slots and starve the rest. Default 2 lets
    # a Teams + Moodle pair run side-by-side; a value of 0 is the kill
    # switch ("admit nothing for any user", documented edge).
    max_syncs_per_user: int = 2
    # How often (seconds) a (user, platform) is eligible to sync again. Can be
    # overridden per-user via the ``user_schedule_overrides`` SQLite table.
    default_interval_seconds: int = 600
    # Dead-letter policy: after N consecutive failed syncs the scheduler stops
    # scheduling that (user, platform) for ``dead_letter_cooldown_seconds``.
    dead_letter_threshold: int = 3
    dead_letter_cooldown_seconds: int = 3600
    # Per-platform overrides. Keys are platform strings ("teams", "moodle",
    # "telegram"). Missing keys fall back to the globals above.
    per_platform_interval_seconds: dict[str, int] = Field(default_factory=dict)
    per_platform_max_concurrent: dict[str, int] = Field(default_factory=dict)


class PlatformPauseConfig(BaseModel):
    """Per-platform pause switches. Set via the overlay to temporarily stop
    syncs for a single platform without disabling it outright.

    Distinct from :class:`PlatformToggles`'s ``*_enabled`` flags: ``enabled``
    is treated as permanent configuration (disables the adapter end-to-end,
    drops forced syncs); ``paused`` is treated as a transient operator pause
    so the scheduler skips the platform but leaves credentials and routing
    intact. The dashboard's V10 ops pane toggles this.
    """

    teams: bool = False
    moodle: bool = False
    telegram: bool = False


class PlatformToggles(BaseModel):
    teams_enabled: bool = True
    moodle_enabled: bool = True
    telegram_enabled: bool = True
    # Transient operator pause. Honored by the scheduler on every tick; flip
    # via ``PUT /api/config/overlay/platforms.paused.<platform>``. See
    # :class:`PlatformPauseConfig` for the semantics vs ``*_enabled``.
    paused: PlatformPauseConfig = Field(default_factory=PlatformPauseConfig)


class IngestionControlsConfig(BaseModel):
    """Live daemon policy applied uniformly after adapter normalization."""

    recency_days: int | None = Field(default=None, ge=1)


class TeamsRefreshConfig(BaseModel):
    """Bulk-refresh cycle for Teams tokens using the headless Playwright pool."""

    # Hard disable the refresh loop at runtime. Matches the scheduler and
    # api kill-switches; avoids overloading pool_size as a kill switch.
    enabled: bool = True
    # 30 minutes matches the default access-token lifetime, so a completed
    # cycle gets every user within one expiry window.
    interval_seconds: int = 1800
    # Concurrent headless contexts. Too high and Microsoft throttles; too low
    # and the cycle runs over its window.
    pool_size: int = 4
    # Jitter applied to each context acquisition so concurrent refreshes
    # don't all fire into Microsoft at the same millisecond.
    pool_jitter_seconds: float = 1.0
    # Per-refresh wait for tokens to appear in intercepted requests.
    timeout_seconds: int = 30
    # Cap retries per user per cycle so a stuck user doesn't burn a slot.
    max_retries_per_user: int = 1
    # Cap how many refresh_one calls run concurrently. Default: pool_size * 2.
    concurrency: int = 8


class ApiConfig(BaseModel):
    # Hard disable the API server at runtime. The app factory still builds
    # the app object for tests; this only gates ``runtime.run`` spawning
    # uvicorn.
    enabled: bool = True
    host: str = "0.0.0.0"  # noqa: S104 (container-local; auth via signed cookie)
    port: int = 8765
    session_ttl_hours: int = 24
    # CORS: empty list means same-origin only. Dashboard served from the
    # same container shares an origin; set explicitly during dev. A bare
    # ``"*"`` is rejected when credentials are allowed (browsers refuse it
    # anyway, but we fail loudly at startup).
    cors_origins: list[str] = Field(default_factory=list)
    # If True the app will refuse to start without both
    # ``ENDPOINT_ADMIN_PASSWORD`` and ``ENDPOINT_SESSION_SECRET`` set.
    require_auth_env: bool = True
    # Flip this when a TLS-terminating proxy sits in front of the daemon.
    # When True the session cookie is issued with ``Secure=True``, which
    # stops the browser from sending it over plain HTTP. Default is False
    # so local dev (vite -> uvicorn on :8765) works.
    https_terminated_upstream: bool = False
    # Hard cap on HTTP request body size (bytes). 1 MiB easily covers the
    # largest legitimate overlay/payload; anything bigger is either a bug or
    # an attacker streaming junk at the daemon.
    max_request_bytes: int = 1_048_576
    # Per-source-IP token-bucket limit on ``POST /api/auth/login``.
    # 10/minute is enough for an admin who mistyped their password a few
    # times while leaving no room for meaningful bruteforce.
    login_rate_limit_per_minute: int = 10
    # TTL for cached Idempotency-Key responses. Slice 5 part B. 24h
    # matches AWS / Stripe / Twilio conventions and is long enough that
    # a retry from a queued worker won't expire between failure and
    # retry. Operators who need a tighter window can lower this; a
    # value of 0 disables the cache entirely (handler runs every time).
    idempotency_ttl_seconds: int = 24 * 60 * 60
    # Hard cap on every destructive-action handler. Without it a
    # single hung dependency (postgres, sqlite VACUUM stuck on a
    # blocked writer) holds an HTTP request open until the upstream
    # times out — operators see a "click and wait" UI that never
    # resolves. 30s is generous for our slowest in-process action
    # (compact_storage on a multi-GB sqlite); anything that needs
    # longer should run as a background job.
    action_timeout_seconds: int = 30
    # Backpressure: refuse new force_sync_requests inserts past this
    # depth. Without it, an operator script that loops ``force_sync_one``
    # over a million users can flood the SQLite queue and block every
    # legitimate API call on the same write transaction. 10k is well
    # past any legitimate operator burst (~ a per-user incident
    # response across an entire org) but not so high that a runaway
    # script can pin the daemon for minutes before the cap fires.
    max_force_sync_queue_depth: int = 10_000


class ObservabilityConfig(BaseModel):
    """Log persistence + alert-engine knobs. Phase 4."""

    # Persist every structlog record into the ``logs`` SQLite table.
    log_to_sqlite: bool = True
    retention_days: int = 30
    retention_interval_seconds: int = 3600
    alert_tick_seconds: int = 30
    # Slice 13: hourly online-safe SQLite backup of ``state.db``. Default
    # on; flip to False on a host with no spare disk for the rotation.
    # Default 24 hourly snapshots = 24h of point-in-time recovery.
    backup_enabled: bool = True
    backup_interval_seconds: int = 3600
    backup_retention_count: int = 24
    # Empty path means "alongside the live DB". Operators with a separate
    # backups volume override via env (e.g.
    # ``ENDPOINT_DAEMON__OBSERVABILITY__BACKUP_DIR=/var/backups``).
    backup_dir: Path | None = None


class AlertsConfig(BaseModel):
    """Slice 13: external alert delivery + default-rule seeding.

    ``webhook_url`` toggles the :class:`WebhookAlertNotifier`. Empty (the
    default) keeps the engine on the :class:`NullAlertNotifier` so existing
    tests and dev-mode runs don't try to reach a non-existent endpoint.

    ``notifier_format`` picks between a generic JSON payload (``"webhook"``,
    default) and a Slack-compatible ``{"text": "..."}`` shape (``"slack"``).
    ``webhook_token`` is sent as ``Authorization: Bearer <token>`` when
    non-empty; the secret never appears in any log line.
    """

    webhook_url: str = ""
    webhook_token: str = ""
    notifier_format: str = "webhook"
    # When True (default) the daemon seeds a small set of opinionated
    # alert rules on first boot if the table is empty / missing them.
    # Idempotent — operators who pre-seeded their own copies are not
    # overwritten because the seed insert is keyed on rule name.
    seed_default_rules: bool = True


class LifecycleConfig(BaseModel):
    """TTL prune windows for daemon-owned tables (slice 12).

    Each ``*_retention_days`` field is the maximum age of rows kept in
    the corresponding table before the lifecycle prune loop deletes
    them. Set any field to ``0`` to disable that prune entirely (the
    loop driver treats ``0`` as "skip this pass" rather than "delete
    everything"). The interval governs how often the loop wakes up;
    1 hour matches the existing log-retention loop.

    Defaults:

    * sync_history: 30 days (matches operator-incident review window)
    * dead_letter: 90 days (long enough to spot recurring issues)
    * audit_log: 365 days (compliance default)
    """

    sync_history_retention_days: int = 30
    dead_letter_retention_days: int = 90
    audit_log_retention_days: int = 365
    interval_seconds: int = 3600


class OpsConfig(BaseModel):
    """Operator safety switches. Persistent via the overlay.

    Shadow mode suppresses content writes through :class:`PostgresContentSink`
    so a full fetch cycle runs end-to-end (adapter, normalization, cursor
    updates via the sync-state path) without producing visible content in
    the shared ``ingested_items`` table. It's intended for testing schema
    changes or credential rotations without downstream consumers seeing
    half-processed data.

    Intentionally in scope: content writes (``upsert``/``upsert_batch``) and
    the content-triggered sync_history completion entry. Intentionally OUT
    of scope: audit_log, logs, dead_letter, and failed_sync writers — those
    are observability surfaces and must keep working so the shadow run is
    still inspectable from the dashboard.
    """

    shadow_mode: bool = False


class SentryConfig(BaseModel):
    """Optional Sentry/GlitchTip error-tracker integration.

    When ``dsn`` is non-empty the daemon initialises ``sentry_sdk`` at
    startup. Unhandled exceptions, ``logger.exception/error`` calls, and
    FastAPI request failures flow to the configured GlitchTip/Sentry
    instance automatically. Leave ``dsn`` empty to disable — the daemon
    still boots, just without external error forwarding.

    ``public_url`` is the browser-facing base URL of the GlitchTip UI
    (e.g. ``http://localhost:8000``). The dashboard reads it via
    /api/settings/about and renders a "Open in GlitchTip" link on the
    Errors page. It's separate from ``dsn`` because the DSN is an
    ingest endpoint and the public URL is where a human clicks.
    """

    dsn: str = ""
    environment: str = "development"
    traces_sample_rate: float = 0.0
    public_url: str = ""
    # Organization slug inside the GlitchTip instance. The dashboard's
    # "Open in GlitchTip" deep links need it to construct issue-search
    # URLs (``{public_url}/{org_slug}/issues/?query=...``). Matches the
    # slug produced by ``scripts/glitchtip_bootstrap.py``.
    org_slug: str = ""


class DaemonConfig(BaseSettings):
    """Daemon-level settings.

    Env precedence is handled by pydantic-settings. The overlay is applied by
    :func:`load_config` after initial construction.
    """

    sqlite_path: Path = DEFAULT_SQLITE_PATH
    log_level: str = "INFO"
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    platforms: PlatformToggles = Field(default_factory=PlatformToggles)
    ingestion_controls: IngestionControlsConfig = Field(default_factory=IngestionControlsConfig)
    teams_refresh: TeamsRefreshConfig = Field(default_factory=TeamsRefreshConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    sentry: SentryConfig = Field(default_factory=SentryConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)

    model_config = SettingsConfigDict(
        env_prefix="ENDPOINT_DAEMON__",
        env_nested_delimiter="__",
    )


def _set_nested(data: dict[str, Any], dotted_key: str, value: Any) -> bool:
    """Apply ``dotted_key = value`` in-place on ``data``.

    Returns True when the path exists on the model, False when it's unknown.
    """
    parts = dotted_key.split(".")
    cursor: dict[str, Any] | None = data
    for part in parts[:-1]:
        if cursor is None or part not in cursor:
            return False
        next_level: Any = cursor[part]
        cursor = cast("dict[str, Any]", next_level) if isinstance(next_level, dict) else None
    if cursor is None or parts[-1] not in cursor:
        return False
    cursor[parts[-1]] = value
    return True


def _apply_overlay(config: DaemonConfig, overlay: dict[str, Any]) -> DaemonConfig:
    """Return a copy of ``config`` with overlay values applied.

    Each overlay key is applied one at a time against a tentative dict; if
    the result fails pydantic validation, that single key is logged and
    dropped so the daemon still boots with the rest of the overlay and the
    env defaults in place.
    """
    base = config.model_dump()
    valid = base
    for dotted_key, value in overlay.items():
        candidate = deepcopy(valid)
        if not _set_nested(candidate, dotted_key, value):
            _log.warning("daemon.overlay.unknown_key", key=dotted_key)
            continue
        try:
            DaemonConfig.model_validate(candidate)
        except ValidationError as exc:
            _log.warning(
                "daemon.overlay.invalid_value",
                key=dotted_key,
                errors=exc.errors(include_url=False, include_input=False),
            )
            continue
        valid = candidate
    return DaemonConfig.model_validate(valid)


def load_config(
    store: LocalStore | None = None,
    *,
    base: DaemonConfig | None = None,
) -> DaemonConfig:
    """Build a :class:`DaemonConfig` from env + overlay.

    Pass ``store=None`` to skip the overlay (useful for tests that only want
    env precedence). Pass an already-constructed ``base`` to carry forward
    values that wouldn't round-trip through a fresh ``DaemonConfig()`` call
    (e.g. a non-default ``sqlite_path`` supplied to ``runtime._bootstrap``).
    """
    start = base if base is not None else DaemonConfig()
    if store is None:
        return start
    overlay = store.all_overlay()
    if not overlay:
        return start
    return _apply_overlay(start, overlay)
