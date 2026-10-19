"""GET /api/schedule/next — when each credential's next sync is due.

Powers the Overview countdown panel. Mirrors the scheduler's eligibility
logic (`_eligible` in ``scheduler.py``) so the API reports the same
reasons a sync might be blocked.

One intentional asymmetry vs the scheduler: an unregistered platform
returns ``True`` from :func:`_platform_enabled` here so the row stays
visible in the dashboard, while ``Scheduler._platform_enabled`` returns
``False`` and gates execution. Visibility (this endpoint) and gating
(the scheduler) deliberately diverge so an operator can see a row whose
platform was added to Postgres ahead of a code deploy.

Diagnostics endpoint (slice 7):

* ``GET /api/schedule/why-not-running/{email}/{platform}`` — single-pair
  variant of ``_blocked_by``. Operators use it when a particular user
  hasn't synced in a while and the schedule list is too noisy. Returns
  the same enum vocabulary plus an explicit ``"none"`` for the
  unblocked case, plus ``"running"`` and ``"not_due"`` for states that
  ``schedule/next`` represents implicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite
from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.config import DaemonConfig, load_config
from ingestion.daemon.platforms import UnknownPlatformError, get_platform
from ingestion.daemon.schedule_queries import (
    last_success_at_bulk,
    user_interval_overrides_bulk,
)
from ingestion.daemon.system_users import SYSTEM_USER_EMAILS

router = APIRouter(prefix="/schedule", tags=["schedule"])

BlockedBy = Literal[
    "emergency_stop",
    "drain_mode",
    "global_paused",
    "expired",
    "paused",
    "platform_disabled",
    "platform_paused",
    "suppressed",
]
IntervalSource = Literal["user_override", "platform_default", "default"]


class NextSyncItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_email: str
    platform: str
    last_success_at: str | None
    interval_seconds: int
    interval_source: IntervalSource
    next_due_at: str
    blocked_by: BlockedBy | None
    # True while a ``sync_history`` row for this pair is in state='running'.
    # Distinct from ``blocked_by``: a running pair is actively progressing,
    # just not yet "complete" (so it wouldn't count as last_success yet).
    running: bool


class ScheduleNextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NextSyncItem]


async def _suppressed_until_map(db_path: Path) -> dict[tuple[str, str], datetime]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT user_email, platform, suppressed_until "
            "FROM dead_letter_state WHERE suppressed_until IS NOT NULL",
        )
        rows = await cursor.fetchall()
    out: dict[tuple[str, str], datetime] = {}
    for email, plat, until in rows:
        if until is None:
            continue
        out[(str(email), str(plat))] = datetime.fromisoformat(str(until))
    return out


async def _running_pairs(db_path: Path) -> set[tuple[str, str]]:
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT user_email, platform FROM sync_history "
            "WHERE state = 'running' AND finished_at IS NULL",
        )
        rows = await cursor.fetchall()
    return {(str(row[0]), str(row[1])) for row in rows}


def _resolve_interval(
    platform: str,
    config: DaemonConfig,
    user_override: int | None,
) -> tuple[int, IntervalSource]:
    if user_override is not None:
        return user_override, "user_override"
    platform_override = config.scheduler.per_platform_interval_seconds.get(platform)
    if platform_override is not None:
        return platform_override, "platform_default"
    return config.scheduler.default_interval_seconds, "default"


def _platform_enabled(config: DaemonConfig, platform: str) -> bool:
    # Schedule API mirrors the scheduler's _eligible logic but treats
    # unknown platforms as ENABLED (legacy behaviour: the read endpoint
    # should not hide rows for a platform the operator can see in
    # Postgres but hasn't been added to the registry yet). The scheduler
    # path is the one that gates actual execution and returns False for
    # unknowns; here we only render visibility.
    try:
        spec = get_platform(platform)
    except UnknownPlatformError:
        return True
    return bool(getattr(config.platforms, spec.enabled_config_attr))


def _platform_paused(config: DaemonConfig, platform: str) -> bool:
    try:
        spec = get_platform(platform)
    except UnknownPlatformError:
        return False
    return bool(getattr(config.platforms.paused, spec.paused_config_attr))


def _blocked_by(
    *,
    cred_is_expired: bool,
    cred_is_paused: bool,
    platform: str,
    config: DaemonConfig,
    suppressed_until: datetime | None,
    now: datetime,
) -> BlockedBy | None:
    # Ordering mirrors scheduler._eligible: global kill-switches win over
    # per-credential state so the reason the operator sees is the top-of-
    # stack cause rather than a fact that wouldn't matter if they unpaused.
    if config.scheduler.emergency_stop_requested:
        return "emergency_stop"
    if config.scheduler.drain_mode:
        return "drain_mode"
    if config.scheduler.global_paused:
        return "global_paused"
    if not _platform_enabled(config, platform):
        return "platform_disabled"
    if _platform_paused(config, platform):
        return "platform_paused"
    # ``expired`` is checked BEFORE ``paused``: a paused-AND-expired
    # credential reports ``expired`` because that is the more actionable
    # signal (unpausing alone won't restore eligibility — the operator
    # needs to re-auth first).  The contract is pinned by
    # ``tests/edges/s7_endpoints.py::test_why_not_running_expired_wins_over_paused``.
    if cred_is_expired:
        return "expired"
    if cred_is_paused:
        return "paused"
    if suppressed_until is not None and suppressed_until > now:
        return "suppressed"
    return None


@router.get("/next", response_model=ScheduleNextResponse)
async def schedule_next(ctx: ContextDep) -> ScheduleNextResponse:
    """Return the next-due timestamp for every credential in the system.

    The scheduler consults ``last_success_at + interval`` on every tick;
    this endpoint exposes that computation as a snapshot so the dashboard
    can render a live countdown. ``next_due_at`` is ``last_success_at +
    interval_seconds`` (or ``now`` when a credential has never completed
    a sync — its next tick will pick it up).
    """
    from core.models import PlatformCredential

    config = load_config(ctx.store)
    now = datetime.now(tz=UTC)

    last_success = await last_success_at_bulk(ctx.config.sqlite_path)
    user_overrides = await user_interval_overrides_bulk(ctx.config.sqlite_path)
    suppressed = await _suppressed_until_map(ctx.config.sqlite_path)
    running = await _running_pairs(ctx.config.sqlite_path)

    items: list[NextSyncItem] = []
    # Filter system-owned credentials (Telegram bot etc) — the Next sync
    # panel is per-user-scoped; a dedicated bot-status surface will render
    # system credentials separately.
    async for cred in (
        PlatformCredential.objects.exclude(user_email__in=SYSTEM_USER_EMAILS)
        .order_by("user_email", "platform")
        .aiterator()
    ):
        key = (cred.user_email, cred.platform)
        interval, source = _resolve_interval(cred.platform, config, user_overrides.get(key))
        last = last_success.get(key)
        next_due = (last + timedelta(seconds=interval)) if last is not None else now
        items.append(
            NextSyncItem(
                user_email=cred.user_email,
                platform=cred.platform,
                last_success_at=last.isoformat() if last is not None else None,
                interval_seconds=interval,
                interval_source=source,
                next_due_at=next_due.isoformat(),
                blocked_by=_blocked_by(
                    cred_is_expired=bool(cred.is_expired),
                    cred_is_paused=bool(cred.is_paused),
                    platform=cred.platform,
                    config=config,
                    suppressed_until=suppressed.get(key),
                    now=now,
                ),
                running=key in running,
            )
        )

    return ScheduleNextResponse(items=items)


# --- Diagnostics ----------------------------------------------------------


# The wider vocabulary covers the two states `_blocked_by` doesn't
# enumerate today: ``running`` (a sync is in flight, so "blocked" is
# misleading) and ``not_due`` (eligible but waiting for the interval).
# ``none`` lands when the credential is fully eligible AND would be
# picked up on the next tick — the operator can stop investigating.
WhyNotRunningReason = Literal[
    "emergency_stop",
    "drain_mode",
    "global_paused",
    "expired",
    "paused",
    "platform_disabled",
    "platform_paused",
    "suppressed",
    "running",
    "not_due",
    "none",
]


class WhyNotRunningResponse(BaseModel):
    """Diagnostic explanation for why a single (email, platform) is
    or is not running.

    ``next_due_at`` is None when the credential is permanently blocked
    (paused, expired) — there is no future tick that would unblock it
    without operator action, so a timestamp would be misleading. For
    transient blocks (suppressed, not_due, running), it's the wall-clock
    moment the scheduler would reconsider the row.

    ``reason`` is a human-readable amplification of ``blocked_by``:
    ``"credential is paused"``, ``"global pause is active"``, etc. The
    enum carries the machine-readable bit; the message makes the
    dashboard's empty-state copy useful.
    """

    model_config = ConfigDict(extra="forbid")

    user_email: str
    platform: str
    blocked_by: WhyNotRunningReason
    reason: str
    next_due_at: str | None


# Priority order for resolving the canonical "blocked_by" when multiple
# states apply at once. Mirrors `_blocked_by` for the global → platform →
# credential cascade, then layers running/not-due/none on top in their
# own runtime order. A paused-AND-expired credential reports ``expired``
# (matching ``_blocked_by``: ``cred_is_expired`` is checked before
# ``cred_is_paused``). Operationally this is the more actionable signal —
# unpausing alone won't restore eligibility while the token is expired,
# so the operator needs to re-auth first. The
# ``test_why_not_running_expired_wins_over_paused`` pin test in
# ``tests/edges/s7_endpoints.py`` guards this contract.
_BLOCKED_REASON_MESSAGES: dict[str, str] = {
    "emergency_stop": "scheduler emergency stop is active",
    "drain_mode": "scheduler drain mode is active",
    "global_paused": "scheduler global pause is active",
    "platform_disabled": "platform is disabled in config",
    "platform_paused": "platform is paused via overlay",
    "paused": "credential is paused",
    "expired": "credential is expired",
    "suppressed": "credential is suppressed by dead-letter",
    "running": "a sync for this credential is currently running",
    "not_due": "next sync is not due yet",
    "none": "credential is eligible to sync",
}


@router.get(
    "/why-not-running/{email}/{platform}",
    response_model=WhyNotRunningResponse,
)
async def why_not_running(
    email: str,
    platform: str,
    ctx: ContextDep,
) -> WhyNotRunningResponse:
    """Explain why one (email, platform) credential isn't currently running.

    404 when the credential row doesn't exist — the endpoint targets a
    real (email, platform) pair, so a typo or stale URL deserves a clear
    error rather than a confusing "blocked_by: none".

    The blocked-by priority follows :func:`_blocked_by` for global +
    platform + per-credential states, then surfaces ``running`` /
    ``not_due`` / ``none`` based on the live sync_history + interval
    computation. ``next_due_at`` is None for permanent blocks (paused,
    expired, platform_disabled) and a timestamp for transient ones.
    """
    from core.models import PlatformCredential

    # Lookup the credential row first so a typoed email lands on 404
    # instead of the confusing "blocked_by: none" path that the cred-
    # absent state would otherwise produce.
    try:
        cred = await PlatformCredential.objects.aget(
            user_email=email,
            platform=platform,
        )
    except PlatformCredential.DoesNotExist:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            f"no credential for {email}/{platform}",
            hint="verify the email and platform; case is significant",
        )

    config = load_config(ctx.store)
    now = datetime.now(tz=UTC)
    db_path = ctx.config.sqlite_path

    suppressed_map = await _suppressed_until_map(db_path)
    running_pairs = await _running_pairs(db_path)
    last_success_map = await last_success_at_bulk(db_path)
    overrides_map = await user_interval_overrides_bulk(db_path)

    key = (email, platform)
    blocked = _blocked_by(
        cred_is_expired=bool(cred.is_expired),
        cred_is_paused=bool(cred.is_paused),
        platform=platform,
        config=config,
        suppressed_until=suppressed_map.get(key),
        now=now,
    )

    interval, _src = _resolve_interval(platform, config, overrides_map.get(key))
    last = last_success_map.get(key)
    next_due = (last + timedelta(seconds=interval)) if last is not None else now

    # Compute the surface-level reason. The base ``_blocked_by`` returns
    # the global / platform / per-credential cascade; we extend its
    # vocabulary with running and not_due here.
    surface: WhyNotRunningReason
    next_due_iso: str | None
    if blocked is not None:
        surface = blocked
        # Permanent blocks: a future timestamp would be misleading
        # because there is no tick that would unblock them without
        # operator action. Transient blocks (suppressed) get a real
        # timestamp.
        if blocked == "suppressed":
            until = suppressed_map.get(key)
            next_due_iso = until.isoformat() if until is not None else None
        else:
            next_due_iso = None
    elif key in running_pairs:
        surface = "running"
        # next_due_at is N/A while running — the next tick will see the
        # row is still in flight and skip it; once it completes the
        # interval restarts from finished_at.
        next_due_iso = None
    elif next_due > now:
        surface = "not_due"
        next_due_iso = next_due.isoformat()
    else:
        surface = "none"
        # Eligible: the next due time is "now" (or the past). Surface
        # the past timestamp so the dashboard can render "due since X
        # minutes ago".
        next_due_iso = next_due.isoformat()

    return WhyNotRunningResponse(
        user_email=email,
        platform=platform,
        blocked_by=surface,
        reason=_BLOCKED_REASON_MESSAGES[surface],
        next_due_at=next_due_iso,
    )
