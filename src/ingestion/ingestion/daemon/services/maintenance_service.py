"""Operator-maintenance actions: reset_cursor, purge_item, compact, mute_rule,
clear dead-letter, replay dead-letter, force-refresh teams.

These actions don't fit cleanly into "credentials" or "syncs" but each
ends in an audit row and a typed response. They're grouped here to
preserve the seam — routers translate HTTP envelopes; services hold the
logic.

``purge_item`` is the one place this module still touches the Django
ORM directly (``IngestedItem.objects.aget`` + ``adelete``). The audit
snapshot is built from the real model fields (``fetched_by_user_email``
and ``payload['content_type']``). Lifting this to a content-port is
deferred to a later slice — flagged in the slice report as a follow-up.

Difference between ``clear_dead_letter`` and ``replay_dead_letter``
(slice 7):

* ``clear`` resets the dead-letter counter to 0 AND wipes the
  suppressed_until window — the next failure starts a fresh streak. Use
  when the underlying issue has been verified-fixed.
* ``replay`` enqueues a force-sync without touching the counter or the
  suppression. A still-broken pair re-suppresses immediately on next
  failure (because the streak is intact); a now-fixed pair gets a
  success that auto-resets the counter via the scheduler's existing
  on_finish callback. Use when you want to TEST whether the fix took
  without committing to a full reset.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING, Any

import aiosqlite

from ingestion.daemon.services.exceptions import (
    AlertRuleNotFoundError,
    FeatureDisabledError,
    ItemNotFoundError,
    TransientFailureError,
)
from ingestion.daemon.services.results import (
    CompactStorageResult,
    ForceRefreshResult,
    MuteRuleResult,
    PurgeItemResult,
    ReplayDeadLetterResult,
    ResetCursorResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.daemon.api.pubsub import EventBus
    from ingestion.daemon.config import DaemonConfig
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.services.audit_writer import AuditWriter


async def reset_cursor(
    sqlite_path: Path,
    *,
    email: str,
    platform: str,
    scope: str | None,
    audit_writer: AuditWriter,
    reason: str | None,
) -> ResetCursorResult:
    """Delete sync_cursors rows for ``(email, platform, [scope])``.

    The next sync starts from scratch. Destructive: the sink's upsert
    is idempotent but log spam follows. Snapshots the deleted rows in
    the audit ``before`` payload so an operator can review what was
    discarded.
    """
    params: list[Any] = [email, platform]
    sql = "DELETE FROM sync_cursors WHERE user_email = ? AND platform = ?"
    if scope is not None:
        sql += " AND scope = ?"
        params.append(scope)
    async with aiosqlite.connect(sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        before_rows = await (
            await conn.execute(
                "SELECT scope, cursor_json FROM sync_cursors WHERE user_email = ? AND platform = ?",
                (email, platform),
            )
        ).fetchall()
        cursor = await conn.execute(sql, params)
        rows_deleted = max(0, cursor.rowcount)
        await conn.commit()
    before_snapshot = [
        {"scope": r["scope"], "cursor": json.loads(r["cursor_json"])} for r in before_rows
    ]
    await audit_writer.write(
        action="sync.reset_cursor",
        target=f"{email}/{platform}",
        before={"scopes": before_snapshot},
        after={"rows_deleted": rows_deleted, "scope_filter": scope},
        reason=reason,
    )
    return ResetCursorResult(rows_deleted=rows_deleted)


async def purge_item(
    *,
    item_id: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> PurgeItemResult:
    """Hard-delete a normalized content item.

    Raises :class:`ItemNotFoundError` (-> 404) if the id is unknown.
    The audit ``before`` snapshot mirrors the columns ``IngestedItem``
    actually exposes — attribution lives in ``fetched_by_user_email``
    and ``content_type`` lives inside the JSONB ``payload``. Pulling
    these fields lets an operator reconstruct what was purged from the
    audit log alone.
    """
    from core.models import IngestedItem

    try:
        row = await IngestedItem.objects.aget(id=item_id)
    except IngestedItem.DoesNotExist as exc:
        raise ItemNotFoundError(item_id) from exc
    before = _ingested_item_snapshot(row)
    await IngestedItem.objects.filter(id=item_id).adelete()
    await audit_writer.write(
        action="content.purge",
        target=f"item:{item_id}",
        before=before,
        after={"deleted": True},
        reason=reason,
    )
    return PurgeItemResult(ok=True, item_id=item_id)


def _ingested_item_snapshot(row: Any) -> dict[str, Any]:
    """Audit-friendly snapshot of an :class:`IngestedItem`.

    Pulls the columns that survive on the bare model plus the
    payload-resident ``content_type`` (the field is JSONB-only — see
    ``core/models/ingested.py``). Defensive against legacy rows where
    ``payload`` is missing or non-dict.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    return {
        "id": str(row.id),
        "external_id": row.external_id,
        "user_email": row.fetched_by_user_email,
        "platform": row.platform,
        "course_external_id": row.course_external_id,
        "source_url": row.source_url,
        "content_type": payload.get("content_type")
        if isinstance(payload.get("content_type"), str)
        else None,
        "ingested_at": row.created_at.isoformat() if row.created_at is not None else None,
    }


async def compact_storage(
    sqlite_path: Path,
    *,
    audit_writer: AuditWriter,
    reason: str | None,
) -> CompactStorageResult:
    """Run SQLite VACUUM on the local state database.

    VACUUM rebuilds the file, reclaiming space from deleted rows. Blocks
    writes for the duration. Runs in a worker thread so the event loop
    stays responsive.
    """

    def _run_vacuum() -> int:
        conn = sqlite3.connect(str(sqlite_path))
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        return sqlite_path.stat().st_size

    before_size = sqlite_path.stat().st_size if sqlite_path.exists() else 0
    after_size = await asyncio.to_thread(_run_vacuum)
    await audit_writer.write(
        action="storage.compact",
        target="sqlite",
        before={"file_size_bytes": before_size},
        after={"file_size_bytes": after_size},
        reason=reason,
    )
    return CompactStorageResult(before_bytes=before_size, after_bytes=after_size)


async def clear_dead_letter(
    sqlite_path: Path,
    *,
    email: str,
    platform: str,
    threshold: int,
    cooldown_seconds: int,
    audit_writer: AuditWriter,
    reason: str | None,
    event_bus: EventBus | None = None,
) -> None:
    """Reset the dead-letter counter for one ``(email, platform)`` row.

    Snapshots the pre-mutation row so the audit diff carries the
    failure counter + suppression window the operator just reset.

    Slice 10: when ``event_bus`` is supplied the underlying
    :class:`DeadLetterTracker` emits ``dead_letter.cleared`` on the
    transition. Tests that don't care about the event stream omit it
    and the tracker writes to structlog only.
    """
    from ingestion.daemon.dead_letter import DeadLetterTracker

    async with aiosqlite.connect(sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (
            await conn.execute(
                "SELECT consecutive_failures, last_failure_at, suppressed_until "
                "FROM dead_letter_state WHERE user_email = ? AND platform = ?",
                (email, platform),
            )
        ).fetchone()
    before = dict(row) if row is not None else None

    tracker = DeadLetterTracker(
        sqlite_path,
        threshold=threshold,
        cooldown_seconds=cooldown_seconds,
        event_publisher=event_bus.publish if event_bus is not None else None,
    )
    await tracker.clear(email, platform)
    await audit_writer.write(
        action="dead_letter.clear",
        target=f"{email}/{platform}",
        before=before,
        after={
            "consecutive_failures": 0,
            "last_failure_at": None,
            "suppressed_until": None,
        },
        reason=reason,
    )


async def replay_dead_letter(
    sqlite_path: Path,
    *,
    email: str,
    platform: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> ReplayDeadLetterResult:
    """Enqueue a force-sync without touching the dead-letter counter.

    Contrast with :func:`clear_dead_letter`: this call leaves
    ``consecutive_failures`` and ``suppressed_until`` intact. The
    scheduler's ``on_finish`` callback already calls ``record_success``
    on a clean run (which resets the counter) — so a now-fixed pair
    auto-recovers, while a still-broken pair re-suppresses immediately
    on the next failure because the streak was preserved.

    Mechanically: read the pre-state for the audit ``before`` payload,
    enqueue the force-sync row, write one audit row. The scheduler
    picks up the queued row on its next tick regardless of the usual
    interval check (or the suppressed_until — the scheduler's
    force-sync path bypasses the dead-letter check entirely; see
    scheduler.py).

    No StateConflictError is raised even when the row is suppressed —
    the caller's intent is to bypass the suppression for one attempt.
    """
    from ingestion.daemon.force_sync import enqueue_force_sync

    async with aiosqlite.connect(sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (
            await conn.execute(
                "SELECT consecutive_failures, last_failure_at, suppressed_until "
                "FROM dead_letter_state WHERE user_email = ? AND platform = ?",
                (email, platform),
            )
        ).fetchone()
    before = dict(row) if row is not None else None

    await enqueue_force_sync(
        sqlite_path,
        user_email=email,
        platform=platform,
        reason=reason,
    )
    await audit_writer.write(
        action="dead_letter.replay",
        target=f"{email}/{platform}",
        before=before,
        after={
            "force_sync_queued": True,
            # Counter explicitly NOT reset — see docstring.
            "counter_reset": False,
        },
        reason=reason,
    )
    return ReplayDeadLetterResult(
        ok=True,
        user_email=email,
        platform=platform,
        counter_preserved=True,
    )


async def mute_rule(
    sqlite_path: Path,
    *,
    rule_id: int,
    enabled: bool,
    audit_writer: AuditWriter,
    reason: str | None,
) -> MuteRuleResult:
    """Toggle ``alert_rules.enabled`` and emit an audit row.

    Raises :class:`AlertRuleNotFoundError` (-> 404) if the id is unknown.
    """
    from ingestion.daemon.alerts import rules as rule_store

    previous = await rule_store.get_rule(sqlite_path, rule_id)
    if previous is None:
        raise AlertRuleNotFoundError(str(rule_id))
    await rule_store.set_enabled(sqlite_path, rule_id, enabled=enabled)
    await audit_writer.write(
        action="alerts.rule_muted" if not enabled else "alerts.rule_unmuted",
        target=previous.name,
        before={"enabled": previous.enabled},
        after={"enabled": enabled},
        reason=reason,
    )
    return MuteRuleResult(rule_id=rule_id, enabled=enabled)


async def force_refresh_teams(
    *,
    config: DaemonConfig,
    cipher: CredentialCipher,
    user_refresh_locks: dict[str, asyncio.Lock],
    email: str,
    audit_writer: AuditWriter,
    reason: str | None,
) -> ForceRefreshResult:
    """Trigger one silent Teams refresh for ``email`` out-of-band.

    Uses :class:`HttpxTokenRefresher` — pure HTTPS against
    ``login.microsoftonline.com``. Raises :class:`FeatureDisabledError`
    (-> 503) when the refresh feature is disabled,
    :class:`TransientFailureError` (-> 502) when the refresh attempt
    fails for any non-success non-expired reason.

    Slice 8 follow-up: this action still uses the legacy
    :func:`refresh_one` directly instead of dispatching through the
    polymorphic :func:`refresh_credential` coordinator. The migration
    is non-trivial because the audit_writer integration here pivots
    on the legacy :class:`RefreshResult.outcome` enum and ``.error``
    field, while the coordinator returns
    :class:`CredentialRefreshOutcome` with different field names. A
    proper migration also has to extend the polymorphic path to
    surface a force-refresh-specific endpoint per platform (currently
    only Teams has this maintenance action). Tracked as follow-up;
    see review report from slice 8 reviewer-fixer.
    """
    from ingestion.daemon.auth.notifier import LogOnlyNotifier
    from ingestion.daemon.auth.refresh import (
        HttpxTokenRefresher,
        RefreshOutcome,
        refresh_one,
    )
    from ingestion.daemon.ports.token_store import PostgresTokenStore

    refresh_cfg = config.teams_refresh
    if not refresh_cfg.enabled:
        raise FeatureDisabledError("teams refresh is disabled")

    refresher = HttpxTokenRefresher()
    token_store = PostgresTokenStore(cipher, notifier=LogOnlyNotifier())
    # Per-user lock still serialises bulk-cycle vs force-refresh so two
    # silent OIDC flows don't race writes to the same credential row.
    user_lock = user_refresh_locks.setdefault(email, asyncio.Lock())
    result = await refresh_one(
        user_email=email,
        token_store=token_store,
        refresher=refresher,
        timeout_seconds=refresh_cfg.timeout_seconds,
        max_retries=refresh_cfg.max_retries_per_user,
        user_lock=user_lock,
    )
    await audit_writer.write(
        action="teams.force_refresh",
        target=f"{email}/teams",
        after={"outcome": result.outcome.value, "error": result.error},
        reason=reason,
    )
    if result.outcome is RefreshOutcome.SUCCESS:
        return ForceRefreshResult(outcome=result.outcome.value)
    if result.outcome is RefreshOutcome.EXPIRED:
        return ForceRefreshResult(outcome=result.outcome.value)
    raise TransientFailureError({"outcome": result.outcome.value, "error": result.error})


# Result re-exports for callers that import the model name from this
# module. Keeps the surface obvious for service-layer consumers.
__all__ = [
    "clear_dead_letter",
    "compact_storage",
    "force_refresh_teams",
    "mute_rule",
    "purge_item",
    "reset_cursor",
]
