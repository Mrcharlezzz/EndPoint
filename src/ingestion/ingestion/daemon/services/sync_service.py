"""Sync-control actions: force, force-all, dry-run, cancel, scheduler toggles.

These functions own the queue + sync_history mutations that previously
lived inline in :mod:`ingestion.daemon.api.routers.actions`. They emit
audit rows themselves and surface typed exceptions for the router to
translate.

The dry-run helper here owns its in-process fakes (``_ReadThroughState``,
``_InMemoryDryRunLogger``) so the router stays free of pipeline plumbing.
``_build_adapter`` is module-level so tests can monkey-patch it the same
way they did against the old monolithic router (see migration note in the
test fixture for the new symbol path).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import aiosqlite

from ingestion.daemon.force_sync import enqueue_force_sync, force_sync_queue_depth
from ingestion.daemon.services.exceptions import (
    BackpressureError,
    StateConflictError,
    SyncNotFoundError,
    UserNotFoundError,
)
from ingestion.daemon.services.results import (
    CancelSyncResult,
    DrainModeResult,
    DryRunResult,
    ForceSyncAllResult,
    ForceSyncResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ingestion.daemon.api.cancel_registry import CancelRegistry
    from ingestion.daemon.api.pubsub import EventBus
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.ports.credential_port import CredentialPort
    from ingestion.daemon.services.audit_writer import AuditWriter
    from ingestion.daemon.store.local import LocalStore
    from ingestion.ports import SyncLogger, SyncStateStore


# Message written to ``sync_history.error`` when an operator cancels a
# run. Mirrors the shape the log_failure path uses so the Errors / Syncs
# views can filter cancels off by string match if they ever need to.
_CANCEL_ERROR_MESSAGE = "cancelled by operator"


async def force_sync_one(
    sqlite_path: Path,
    *,
    email: str,
    platform: str,
    audit_writer: AuditWriter,
    reason: str | None,
    max_queue_depth: int | None = None,
) -> ForceSyncResult:
    """Queue one force-sync request. The scheduler picks it up on the
    next tick regardless of the usual due-check.

    ``max_queue_depth`` is the backpressure cap from
    ``config.api.max_force_sync_queue_depth``. When the cap is set and
    the current queue depth meets or exceeds it, raises
    :class:`BackpressureError` instead of inserting another row.
    Passing ``None`` (or 0) disables the check — used by tests that
    don't care about the cap.
    """
    if max_queue_depth is not None and max_queue_depth > 0:
        depth = await force_sync_queue_depth(sqlite_path)
        if depth >= max_queue_depth:
            raise BackpressureError(queue_depth=depth, max_depth=max_queue_depth)
    await enqueue_force_sync(
        sqlite_path,
        user_email=email,
        platform=platform,
        reason=reason,
    )
    await audit_writer.write(
        action="sync.force_requested",
        target=f"{email}/{platform}",
        after={"queued": True},
        reason=reason,
    )
    return ForceSyncResult(ok=True, user_email=email, platform=platform)


async def force_sync_all_for_user(
    sqlite_path: Path,
    *,
    port: CredentialPort,
    email: str,
    audit_writer: AuditWriter,
    reason: str | None,
    max_queue_depth: int | None = None,
) -> ForceSyncAllResult:
    """Queue a force-sync for every connected platform this user has.

    Expired credentials are skipped — the scheduler won't run them
    anyway — so the operator doesn't queue no-ops.

    Honours the same ``max_queue_depth`` backpressure check as
    :func:`force_sync_one`. The check fires up-front against the
    *current* depth: if we'd push past the cap mid-loop the
    BackpressureError surfaces with the depth at the moment of the
    first rejection. Partial enqueues that completed before the cap
    was hit stay in the queue — operators can read the audit row to
    see what landed before the cap.
    """
    rows = await port.list_for_user(email)
    enqueued: list[str] = []
    skipped: list[str] = []
    for row in rows:
        if row.is_expired:
            skipped.append(row.platform)
            continue
        if max_queue_depth is not None and max_queue_depth > 0:
            depth = await force_sync_queue_depth(sqlite_path)
            if depth >= max_queue_depth:
                # Surface the cap so the operator sees a typed
                # rejection with the partial state in the audit row.
                if enqueued:
                    await audit_writer.write(
                        action="sync.force_requested_all",
                        target=email,
                        after={
                            "enqueued": enqueued,
                            "skipped": skipped,
                            "rejected_at": depth,
                        },
                        reason=reason,
                    )
                raise BackpressureError(queue_depth=depth, max_depth=max_queue_depth)
        await enqueue_force_sync(
            sqlite_path,
            user_email=email,
            platform=row.platform,
            reason=reason,
        )
        enqueued.append(row.platform)
    if not enqueued and not skipped:
        raise UserNotFoundError(email)
    await audit_writer.write(
        action="sync.force_requested_all",
        target=email,
        after={"enqueued": enqueued, "skipped": skipped},
        reason=reason,
    )
    return ForceSyncAllResult(user_email=email, enqueued=enqueued, skipped=skipped)


async def dry_run_sync(
    sqlite_path: Path,
    *,
    cipher: CredentialCipher,
    email: str,
    platform: str,
    audit_writer: AuditWriter,
) -> DryRunResult:
    """Run a single sync cycle with writes suppressed and return counts.

    Cursor state is read but not advanced (a read-through fake reads
    the real cursor and swallows writes). Content writes are suppressed
    by passing ``is_shadow_mode=lambda: True`` into a fresh sink. No
    ``sync_history`` row is written (in-memory sync-logger fake).

    Audit row is written on every invocation so dry-runs are traceable.
    Raises :class:`CredentialNotFoundError`-equivalent (returns no row
    -> the router translates the missing-credential signal to 404).
    """
    from ingestion.daemon.auth.notifier import LogOnlyNotifier
    from ingestion.daemon.config import load_config
    from ingestion.daemon.ingestion_controls import build_live_adapter
    from ingestion.daemon.ports.content_sink import PostgresContentSink
    from ingestion.daemon.ports.sync_state import SqliteSyncStateStore
    from ingestion.daemon.ports.token_store import PostgresTokenStore
    from ingestion.daemon.store.local import open_store
    from ingestion.pipeline import run_sync

    token_store = PostgresTokenStore(cipher, notifier=LogOnlyNotifier())
    credentials = await token_store.get_credentials(email, platform)
    if credentials is None:
        # The router maps the None-return on dry-run to a 404 with a
        # platform-specific message; keep the signal explicit by raising
        # the same NotFoundError type the credential service uses.
        from ingestion.daemon.services.exceptions import CredentialNotFoundError

        raise CredentialNotFoundError(f"no credentials for {email} on {platform}")

    # Forced shadow for THIS invocation only; the overlay value stays
    # untouched so a concurrent real sync keeps its normal semantics.
    dry_sink = PostgresContentSink(is_shadow_mode=lambda: True)

    real_state = SqliteSyncStateStore(sqlite_path)
    read_through = _ReadThroughState(real_state)
    dry_logger = _InMemoryDryRunLogger()
    with open_store(sqlite_path) as store:
        adapter = _build_adapter(platform)
        adapter = build_live_adapter(adapter, load_config(store))

    stats, _ = await run_sync(
        adapter=adapter,
        credentials=credentials,
        content_sink=dry_sink,
        sync_state=cast("SyncStateStore", read_through),
        sync_logger=cast("SyncLogger", dry_logger),
        user_id=email,
        token_store=None,
    )

    errors: list[str] = []
    if dry_logger.failure is not None:
        errors.append(dry_logger.failure)
    # stats.errors is a count; the pipeline only exposes FetchError
    # details via structlog. Surface the total so the UI can report
    # "N scope errors" without claiming to enumerate them.
    if stats.errors > 0 and dry_logger.failure is None:
        errors.append(f"{stats.errors} per-scope error(s) — see logs")

    result = DryRunResult(
        items_fetched=stats.items_fetched,
        # items_new + items_updated == "would write" because the sink's
        # shadow gate suppresses BOTH insert and update paths.
        items_would_write=stats.items_new + stats.items_updated,
        items_would_skip=stats.items_skipped,
        errors=errors,
    )
    await audit_writer.write(
        action="sync.dry_run",
        target=f"{email}/{platform}",
        after=result.model_dump(),
    )
    return result


async def cancel_sync(
    sqlite_path: Path,
    *,
    sync_id: str,
    cancels: CancelRegistry,
    events: EventBus,
    audit_writer: AuditWriter,
    reason: str | None,
) -> CancelSyncResult:
    """Cancel an in-flight sync.

    Raises :class:`SyncNotFoundError` (-> 404) if ``sync_id`` is unknown,
    :class:`StateConflictError` (-> 409) if the sync is not currently
    ``running``. Emits ``sync.cancelled`` on the ``/ws/syncs`` channel
    and writes a terminal ``state=cancelled`` row in ``sync_history``
    with ``error=cancelled by operator``.
    """
    async with aiosqlite.connect(sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (
            await conn.execute(
                "SELECT id, user_email, platform, state, started_at FROM sync_history WHERE id = ?",
                (sync_id,),
            )
        ).fetchone()
        if row is None:
            raise SyncNotFoundError(sync_id)
        previous_state = row["state"]
        if previous_state != "running":
            raise StateConflictError(f"sync is in state {previous_state!r}, cannot cancel")
        finished_at = datetime.now(tz=UTC).isoformat()
        # Mirror log_failure's column set so downstream readers (Syncs
        # list, Errors feed) see a complete terminal row. stats_json is
        # reset in case a concurrent log_complete raced in.
        await conn.execute(
            """
            UPDATE sync_history
            SET state = ?, finished_at = ?, error = ?, stats_json = NULL
            WHERE id = ?
            """,
            ("cancelled", finished_at, _CANCEL_ERROR_MESSAGE, sync_id),
        )
        await conn.commit()
        user_email = row["user_email"]
        platform = row["platform"]
        started_at = row["started_at"]

    # Best-effort signal — a registered checkpoint observes this on its
    # next poll. ``signal`` returning False just means no one was
    # registered, which is the common case today.
    await cancels.signal(sync_id)

    await events.publish(
        "syncs",
        {
            "event": "sync.cancelled",
            "sync_id": sync_id,
            "user_email": user_email,
            "platform": platform,
            "timestamp": finished_at,
        },
    )

    await audit_writer.write(
        action="sync.cancel",
        target=sync_id,
        before={
            "sync_id": sync_id,
            "user_email": user_email,
            "platform": platform,
            "state": previous_state,
            "started_at": started_at,
        },
        after={
            "sync_id": sync_id,
            "user_email": user_email,
            "platform": platform,
            "state": "cancelled",
            "finished_at": finished_at,
            "error": _CANCEL_ERROR_MESSAGE,
        },
        reason=reason,
    )

    return CancelSyncResult(
        sync_id=sync_id,
        previous_state=previous_state,
        new_state="cancelled",
    )


async def set_drain_mode(
    *,
    store: LocalStore,
    enabled: bool,
    audit_writer: AuditWriter,
    reason: str | None,
) -> DrainModeResult:
    """Toggle scheduler.drain_mode in the overlay; takes effect next tick.

    Audit-first: write the row BEFORE flipping the overlay so a crash
    between the two leaves a "I intended to drain" trail instead of a
    silent state change with no record.
    """
    before = bool(store.get_overlay("scheduler.drain_mode"))
    await audit_writer.write(
        action="scheduler.drain_set",
        target="scheduler.drain_mode",
        before={"drain_mode": before},
        after={"drain_mode": enabled},
        reason=reason,
    )
    store.set_overlay("scheduler.drain_mode", enabled)
    return DrainModeResult(drain_mode=enabled, previous=before)


async def emergency_stop(
    *,
    store: LocalStore,
    audit_writer: AuditWriter,
    reason: str | None,
) -> None:
    """Cancel every in-flight sync and refuse new submissions until the
    scheduler is re-enabled. The scheduler clears the overlay flag on
    its next tick after cancelling tasks, so the API returns promptly
    without blocking on the scheduler's response.
    """
    await audit_writer.write(
        action="scheduler.emergency_stop",
        target="scheduler",
        before={"emergency_stop_requested": False},
        after={"emergency_stop_requested": True},
        reason=reason,
    )
    store.set_overlay("scheduler.emergency_stop_requested", True)


async def bulk_refresh_teams(
    sqlite_path: Path,
    *,
    port: CredentialPort,
    audit_writer: AuditWriter,
    reason: str | None,
) -> int:
    """Queue a force-sync for every Teams user. Returns the count.

    The Playwright pool processes the queue one at a time to avoid MS
    throttling, so enqueuing in bulk is safe.
    """
    rows = await port.list_for_platform("teams")
    count = 0
    for row in rows:
        await enqueue_force_sync(
            sqlite_path,
            user_email=row.user_email,
            platform="teams",
            reason=reason,
        )
        count += 1
    await audit_writer.write(
        action="teams.bulk_refresh",
        target="teams",
        after={"enqueued": count},
        reason=reason,
    )
    return count


# ---- helpers (private to this module) -------------------------------


def _build_adapter(platform: str) -> Any:
    """Platform -> Adapter. Thin wrapper around the central registry.

    Symbol kept at module scope (rather than inlining the call) so the
    existing test fixtures can continue to monkey-patch
    ``ingestion.daemon.services.sync_service._build_adapter`` — see the
    migration note in the slice-2 handoff. Re-shapes
    :class:`UnknownPlatformError` into the historical ``ValueError`` so
    test_build_adapter_raises_for_unknown_platform stays green.
    """
    from ingestion.config import IngestionConfig
    from ingestion.daemon.platforms import UnknownPlatformError, make_adapter

    ingestion_config = IngestionConfig()
    try:
        return make_adapter(platform, ingestion_config)
    except UnknownPlatformError as exc:
        raise ValueError(f"no adapter registered for platform {platform!r}") from exc


class _ReadThroughState:
    """SyncStateStore that reads real cursors but swallows writes.

    Dry-run must NOT advance cursors — the real scheduler's next tick
    would otherwise skip the range we just "consumed".
    """

    def __init__(self, real_state: Any) -> None:
        self._real = real_state

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict[str, Any] | None:
        return await self._real.get_cursor(user_id, platform, scope)

    async def set_cursor(
        self,
        _user_id: str,
        _platform: str,
        _scope: str,
        _cursor: dict[str, Any],
    ) -> None:
        # Drop the write. A dry-run must not advance cursors.
        return


class _InMemoryDryRunLogger:
    """SyncLogger that captures calls without persisting to sync_history."""

    def __init__(self) -> None:
        self.failure: str | None = None
        self.stats: Any = None

    async def log_start(self, _user_id: str, _platform: str, *, sync_kind: str = "") -> str:
        # Sentinel id so the pipeline's bind_sync_context still has a
        # key; prefix lets grep identify dry-run logs.
        return "dry-run"

    async def log_complete(self, _sync_id: str, stats: Any) -> None:
        self.stats = stats

    async def log_failure(self, _sync_id: str, error: str) -> None:
        self.failure = error


# Re-export so consumers can build their own audit-emitting wrappers.
__all__ = [
    "_CANCEL_ERROR_MESSAGE",
    "bulk_refresh_teams",
    "cancel_sync",
    "dry_run_sync",
    "emergency_stop",
    "force_sync_all_for_user",
    "force_sync_one",
    "set_drain_mode",
]
