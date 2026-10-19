"""/api/settings surface for the dashboard's Settings page (handoff §7.11).

Three read endpoints:

    * ``GET /api/settings/storage`` — SQLite file size, row counts per
      major table, oldest audit/log timestamps. Lets the operator see
      whether the daemon's local store is healthy and whether the
      retention job is keeping up.
    * ``GET /api/settings/about`` — daemon version, git commit if
      available, python/platform details, flag summary.
    * ``GET /api/settings/retention`` — effective retention window and
      the last time each prune ran (best-effort).

Mutations for retention / log level are done via ``/api/config/overlay/*``
so this router is read-only.
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiosqlite
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ApiContext, ContextDep

if TYPE_CHECKING:
    from ingestion.ports import Adapter

router = APIRouter(prefix="/settings", tags=["settings"])


DAEMON_VERSION = "0.1.0"


# Build commit is resolved once per process. ``None`` once a failed lookup has
# been observed so we don't shell out repeatedly for the same answer. The
# tuple's first slot is "did we resolve yet?" so a legitimate ``None`` result
# (no env var, git not available) is cached too.
_BUILD_COMMIT_SENTINEL: tuple[bool, str | None] = (False, None)
_build_commit_cache: tuple[bool, str | None] = _BUILD_COMMIT_SENTINEL


def _resolve_build_commit() -> str | None:
    """Return the deployed commit SHA, preferring ``ENDPOINT_BUILD_COMMIT``
    (set by the image build) over a live ``git rev-parse HEAD``.

    Cached for the life of the process — the commit can't change without a
    daemon restart, and shelling out per ``/about`` request would be wasteful.
    """
    global _build_commit_cache
    resolved, value = _build_commit_cache
    if resolved:
        return value

    env_commit = os.environ.get("ENDPOINT_BUILD_COMMIT")
    if env_commit:
        _build_commit_cache = (True, env_commit.strip())
        return _build_commit_cache[1]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        _build_commit_cache = (True, None)
        return None
    if result.returncode != 0:
        _build_commit_cache = (True, None)
        return None
    sha = result.stdout.strip()
    _build_commit_cache = (True, sha or None)
    return _build_commit_cache[1]


def _postgres_host_from_env() -> str:
    """Extract the host portion of ``ENDPOINT_DAEMON__DB__URL``.

    Returns an empty string when the env var is unset or unparseable — the
    dashboard renders "not configured" in that case. The password portion of
    the URL is never surfaced; ``urlparse`` exposes it separately but we
    only read ``.hostname``.
    """
    url = os.environ.get("ENDPOINT_DAEMON__DB__URL", "")
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return parsed.hostname or ""


class StorageOldest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs: str | None
    audit_log: str | None
    alert_firings: str | None


class StorageStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: str
    file_size_bytes: int
    row_counts: dict[str, int]
    oldest: StorageOldest


class AboutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daemon_version: str
    python_version: str
    platform: str
    api_cookie_name: str
    # Browser-facing GlitchTip/Sentry URL. Empty string when the operator
    # hasn't configured ``ENDPOINT_DAEMON__SENTRY__PUBLIC_URL`` — the
    # dashboard hides the "Open in GlitchTip" button in that case.
    glitchtip_url: str
    # Organization slug in the GlitchTip instance. Dashboard joins with
    # ``glitchtip_url`` to land the operator on ``/{org}/issues/`` rather
    # than the org-selector front page.
    glitchtip_org_slug: str
    # Sentry environment label (dev / staging / prod). Shown next to the
    # version in the dashboard topbar.
    environment: str
    # ISO timestamp of when the daemon process started. Dashboard renders
    # live uptime by diffing against wall-clock on a 1s ticker.
    started_at: str
    # Deployed commit SHA. ``None`` when the image build didn't stamp
    # ``ENDPOINT_BUILD_COMMIT`` and the daemon isn't running inside a git
    # checkout — the About panel shows "unknown build" in that case.
    build_commit: str | None
    # Host portion of ``ENDPOINT_DAEMON__DB__URL``. Empty string when the
    # env var is unset. Password is never surfaced.
    postgres_host: str
    # Scheduler concurrency cap. Matches ``scheduler.max_concurrent_syncs``
    # in the running config so the UI displays what's actually in effect
    # after overlay is applied.
    worker_pool_size: int


class RetentionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_retention_days: int
    log_retention_interval_seconds: int
    log_to_sqlite: bool
    alert_tick_seconds: int


class AdapterHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    enabled: bool
    # ``None`` when we couldn't probe (platform disabled, or no non-expired
    # credential to use). ``True``/``False`` only when we actually hit the
    # adapter's ``test_connection``.
    healthy: bool | None
    probed_at: str
    error: str | None


class AdaptersHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[AdapterHealth]
    total_enabled: int
    total_healthy: int


_TABLES_OF_INTEREST = (
    "logs",
    "audit_log",
    "sync_history",
    "sync_profiles",
    "alert_rules",
    "alert_firings",
    "dead_letter_state",
    "user_schedule_overrides",
    "config_overlay",
)


@router.get("/storage", response_model=StorageStats)
async def storage_stats(ctx: ContextDep) -> StorageStats:
    """File size + per-table row counts + oldest rows in the hot tables."""
    db_path = ctx.config.sqlite_path
    file_size = db_path.stat().st_size if db_path.exists() else 0

    row_counts: dict[str, int] = {}
    async with aiosqlite.connect(db_path) as conn:
        for table in _TABLES_OF_INTEREST:
            row = await (await conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            row_counts[table] = int(row[0]) if row is not None else 0

        oldest_log = await (await conn.execute("SELECT MIN(timestamp) FROM logs")).fetchone()
        oldest_audit = await (await conn.execute("SELECT MIN(timestamp) FROM audit_log")).fetchone()
        oldest_firing = await (
            await conn.execute("SELECT MIN(fired_at) FROM alert_firings")
        ).fetchone()
    return StorageStats(
        db_path=str(db_path),
        file_size_bytes=file_size,
        row_counts=row_counts,
        oldest=StorageOldest(
            logs=oldest_log[0] if oldest_log is not None else None,
            audit_log=oldest_audit[0] if oldest_audit is not None else None,
            alert_firings=oldest_firing[0] if oldest_firing is not None else None,
        ),
    )


@router.get("/about", response_model=AboutResponse)
async def about(ctx: ContextDep) -> AboutResponse:
    """Static info about the running daemon."""
    return AboutResponse(
        daemon_version=DAEMON_VERSION,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        api_cookie_name="endpoint_admin_session",
        glitchtip_url=ctx.config.sentry.public_url,
        glitchtip_org_slug=ctx.config.sentry.org_slug,
        environment=ctx.config.sentry.environment,
        started_at=ctx.started_at.isoformat(),
        build_commit=_resolve_build_commit(),
        postgres_host=_postgres_host_from_env(),
        worker_pool_size=ctx.config.scheduler.max_concurrent_syncs,
    )


@router.get("/retention", response_model=RetentionResponse)
async def retention(ctx: ContextDep) -> RetentionResponse:
    """Effective retention settings as the daemon is running them."""
    obs = ctx.config.observability
    return RetentionResponse(
        log_retention_days=obs.retention_days,
        log_retention_interval_seconds=obs.retention_interval_seconds,
        log_to_sqlite=obs.log_to_sqlite,
        alert_tick_seconds=obs.alert_tick_seconds,
    )


# --- Adapter health probe ---------------------------------------------------
#
# The Overview/StatusStrip renders "N/M adapters healthy" driven by this
# endpoint. Each probe picks the first usable PlatformCredential, decrypts
# the payload, constructs the matching adapter, and calls
# ``test_connection()`` with a short timeout. The result is cached in
# process memory for ``_HEALTH_TTL`` seconds so the UI's periodic refresh
# doesn't hammer the upstream APIs.

_HEALTH_TTL_SECONDS = 60.0
_HEALTH_PROBE_TIMEOUT_SECONDS = 10.0
_health_cache: dict[str, tuple[float, AdapterHealth]] = {}
_health_locks: dict[str, asyncio.Lock] = {}


def _make_adapter(platform_name: str) -> Adapter:
    """Build a stateless adapter instance keyed on platform.

    Thin wrapper around :func:`ingestion.daemon.platforms.make_adapter`.
    Re-shapes :class:`UnknownPlatformError` into the historical
    ``ValueError`` the existing tests assert against, so the public
    contract stays unchanged.
    """
    from ingestion.config import IngestionConfig
    from ingestion.daemon.platforms import UnknownPlatformError, make_adapter

    cfg = IngestionConfig()
    try:
        return make_adapter(platform_name, cfg)
    except UnknownPlatformError as exc:
        raise ValueError(f"no adapter registered for platform {platform_name!r}") from exc


async def _probe_one(platform_name: str, enabled: bool, ctx: ApiContext) -> AdapterHealth:
    """Run a single probe. Never raises — every failure mode becomes a
    structured result row so the dashboard can render it."""
    probed_at = datetime.now(tz=UTC).isoformat()
    if not enabled:
        return AdapterHealth(
            platform=platform_name,
            enabled=False,
            healthy=None,
            probed_at=probed_at,
            error=None,
        )

    from core.models import PlatformCredential

    row = await PlatformCredential.objects.filter(
        platform=platform_name, is_expired=False, is_paused=False
    ).afirst()
    if row is None:
        return AdapterHealth(
            platform=platform_name,
            enabled=True,
            healthy=None,
            probed_at=probed_at,
            error="no non-expired credential configured",
        )

    try:
        creds = ctx.cipher.decrypt_json(bytes(row.encrypted_payload))
    except Exception as exc:  # noqa: BLE001 — any decrypt failure is a probe failure
        return AdapterHealth(
            platform=platform_name,
            enabled=True,
            healthy=False,
            probed_at=probed_at,
            error=f"decrypt failed: {type(exc).__name__}",
        )

    try:
        adapter = _make_adapter(platform_name)
    except Exception as exc:  # noqa: BLE001
        return AdapterHealth(
            platform=platform_name,
            enabled=True,
            healthy=False,
            probed_at=probed_at,
            error=f"adapter init failed: {type(exc).__name__}: {exc!s:.200}",
        )

    try:
        ok = await asyncio.wait_for(
            adapter.test_connection(creds),
            timeout=_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return AdapterHealth(
            platform=platform_name,
            enabled=True,
            healthy=False,
            probed_at=probed_at,
            error=f"probe timed out after {_HEALTH_PROBE_TIMEOUT_SECONDS:.0f}s",
        )
    except Exception as exc:  # noqa: BLE001
        return AdapterHealth(
            platform=platform_name,
            enabled=True,
            healthy=False,
            probed_at=probed_at,
            error=f"{type(exc).__name__}: {exc!s:.200}",
        )

    return AdapterHealth(
        platform=platform_name,
        enabled=True,
        healthy=bool(ok),
        probed_at=probed_at,
        error=None,
    )


async def _probe_cached(platform_name: str, enabled: bool, ctx: ApiContext) -> AdapterHealth:
    now = time.monotonic()
    cached = _health_cache.get(platform_name)
    if cached is not None and cached[0] > now:
        return cached[1]
    lock = _health_locks.setdefault(platform_name, asyncio.Lock())
    async with lock:
        # Re-check after acquiring the lock in case another request filled it.
        cached = _health_cache.get(platform_name)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        result = await _probe_one(platform_name, enabled, ctx)
        _health_cache[platform_name] = (time.monotonic() + _HEALTH_TTL_SECONDS, result)
        return result


@router.get("/adapters/health", response_model=AdaptersHealthResponse)
async def adapters_health(ctx: ContextDep) -> AdaptersHealthResponse:
    """Per-platform liveness probes. Cached in memory for
    ``_HEALTH_TTL_SECONDS`` so the Overview page can poll cheaply."""
    from ingestion.daemon.platforms import iter_platforms

    toggles = ctx.config.platforms
    # Iterate the registry instead of a hardcoded triple so adding a
    # fourth platform requires no change here.
    inputs = [
        (spec.name, bool(getattr(toggles, spec.enabled_config_attr))) for spec in iter_platforms()
    ]
    # Run all probes concurrently. The lock inside _probe_cached
    # serialises repeat probes for the same platform across concurrent
    # dashboard requests.
    results = await asyncio.gather(*(_probe_cached(name, enabled, ctx) for name, enabled in inputs))
    total_enabled = sum(1 for r in results if r.enabled)
    total_healthy = sum(1 for r in results if r.healthy is True)
    return AdaptersHealthResponse(
        platforms=results,
        total_enabled=total_enabled,
        total_healthy=total_healthy,
    )
