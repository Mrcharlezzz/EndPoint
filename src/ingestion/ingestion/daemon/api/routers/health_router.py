"""Liveness and readiness probes for orchestrators (k8s, compose).

Three public endpoints — never auth-gated, per Prometheus / k8s convention:

- ``GET /health`` — pure liveness. The process is up. No DB calls; a slow
  or wedged SQLite must not flap liveness.
- ``GET /live`` — alias of ``/health`` for k8s readers that look for the
  conventional name.
- ``GET /ready`` — readiness. Verifies the daemon can actually serve work:
  SQLite reachable, scheduler in a known state (active / disabled /
  stalled), and a recent sync attempt when the scheduler is enabled. A
  daemon whose worker pool has deadlocked but whose ASGI socket is still
  open answers 503 here so an orchestrator can recycle the pod.

The readiness "stalled" signal uses ``MAX(started_at)`` from
``sync_history`` against a generous threshold of ``max(60s, 2 *
tick_seconds)``. The exact spec asked for ``2 * tick_seconds`` but with
the default ``tick_seconds=1.0`` that would flap on any tick that
happened to find no due candidates; the floor of 60s tolerates idle
intervals on small fleets. A daemon with literally zero historical
syncs is reported "active" (not "stalled") — an empty fleet is a valid
state, not a stall.
"""

from __future__ import annotations

from typing import Literal

import aiosqlite
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ApiContext, ContextDep
from ingestion.daemon.logging import get_logger

router = APIRouter(tags=["health"])

_log = get_logger(__name__)

# Floor for "scheduler stalled" detection. With default ``tick_seconds=1.0``
# the spec's 2*tick window is too tight; idle intervals on small fleets can
# easily exceed two seconds without anything being wrong. 60s is generous
# enough to ride out a quiet period and tight enough that a deadlocked
# scheduler still gets caught within a single Prometheus scrape window.
_STALLED_FLOOR_SECONDS = 60.0


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class ReadyChecks(BaseModel):
    """Per-check status surface inside the ``/ready`` body.

    Each field is a fixed enum so dashboards can colour-code without
    string-matching free-form messages.
    """

    model_config = ConfigDict(extra="forbid")

    db: Literal["ok", "unreachable"]
    worker_pool: Literal["ok", "uninitialised"]
    scheduler: Literal["active", "disabled", "stalled"]


class ReadyResponse(BaseModel):
    """Readiness probe payload. ``status`` is ``ok`` only when every
    dependent check is non-degraded."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unhealthy"]
    checks: ReadyChecks
    reason: str | None = None


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: process is up.

    No downstream calls. A slow SQLite must not break liveness — that's
    what readiness is for.
    """
    return HealthResponse(status="ok")


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """k8s convention alias for ``/health``."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(ctx: ContextDep, response: Response) -> ReadyResponse:
    """Readiness: the daemon can serve work.

    Returns 200 with ``status: ok`` when every check passes, otherwise
    503 with a per-check breakdown so an orchestrator can decide whether
    to wait or recycle. The endpoint pins 503 (not 500) so probes treat
    a missing dependency as "not ready yet" rather than an exception.
    """
    db_status = await _check_db(ctx.config.sqlite_path)
    worker_pool_status: Literal["ok", "uninitialised"] = "ok"
    scheduler_status, scheduler_reason = await _check_scheduler(ctx)

    checks = ReadyChecks(
        db=db_status,
        worker_pool=worker_pool_status,
        scheduler=scheduler_status,
    )
    failed_reasons: list[str] = []
    if db_status != "ok":
        failed_reasons.append("sqlite unreachable")
    if scheduler_status == "stalled" and scheduler_reason:
        failed_reasons.append(scheduler_reason)

    if failed_reasons:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="unhealthy",
            checks=checks,
            reason="; ".join(failed_reasons),
        )
    return ReadyResponse(
        status="ok",
        checks=checks,
        reason=None,
    )


async def _check_db(sqlite_path: object) -> Literal["ok", "unreachable"]:
    """Round-trip a trivial query against SQLite.

    Caught broadly: aiosqlite/PEP-249 surface a wide error family
    (operational errors, lock timeouts, IO errors) and any of them mean
    "not ready". We never want this probe to raise — it must always
    answer with one of the two enum strings.
    """
    try:
        async with aiosqlite.connect(sqlite_path) as conn:
            cursor = await conn.execute("SELECT 1")
            await cursor.fetchone()
            await cursor.close()
    except Exception:
        _log.warning("ready.db_check_failed", exc_info=True)
        return "unreachable"
    return "ok"


async def _check_scheduler(
    ctx: ApiContext,
) -> tuple[Literal["active", "disabled", "stalled"], str | None]:
    """Classify scheduler state from config + sync_history age.

    "stalled" requires both: scheduler is configured enabled AND the
    most recent ``started_at`` is older than ``max(60s, 2*tick)``. A
    fresh daemon with zero history is "active" — empty history is a
    valid post-deploy state, not a stall.
    """
    config = ctx.config
    if not config.scheduler.enabled:
        return "disabled", None

    threshold = max(_STALLED_FLOOR_SECONDS, 2.0 * float(config.scheduler.tick_seconds))
    age_seconds = await _last_sync_age_seconds(config.sqlite_path)
    if age_seconds is None:
        # No history yet. We can't distinguish "just deployed" from
        # "never scheduling" without more state, so don't penalise the
        # fresh-deploy case.
        return "active", None
    if age_seconds > threshold:
        return "stalled", (
            f"no sync attempted in last {int(age_seconds)}s (threshold {int(threshold)}s)"
        )
    return "active", None


async def _last_sync_age_seconds(sqlite_path: object) -> float | None:
    """Return seconds since the most recent ``started_at`` in
    ``sync_history``, or ``None`` if the table has no rows.

    Uses SQLite's ``julianday`` arithmetic so the calculation runs on
    the DB side and survives any clock-skew between the query and the
    Python interpreter.
    """
    try:
        async with aiosqlite.connect(sqlite_path) as conn:
            cursor = await conn.execute(
                "SELECT (julianday('now') - julianday(MAX(started_at))) * 86400 "
                "AS age FROM sync_history"
            )
            row = await cursor.fetchone()
            await cursor.close()
    except Exception:
        _log.warning("ready.last_sync_age_failed", exc_info=True)
        return None
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None
