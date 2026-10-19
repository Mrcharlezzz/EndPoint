"""/api/syncs (list), /api/syncs/{id} (detail with profile + stats).

List rows carry the decoded ``stats_json`` (items_*, error counts) and the
cursor snapshots so the UI's Sync Run entity (handoff §9) lands without the
frontend doing a separate drill-down. Filter params are enum-validated and
bounded.

Diagnostics endpoints (slice 7):

* ``GET /api/syncs/running`` — read-only list of every sync_history row
  whose ``state='running'``. Pure-SQLite read; no side effects. Returns an
  empty list when nothing is in flight (NOT 404 — the empty state is a
  valid steady state, not a missing resource).
* ``GET /api/syncs/{sync_id}/logs`` — convenience wrapper around
  ``GET /api/logs?sync_id=<sync_id>`` so callers don't have to assemble
  the query themselves. 200 with an empty list when the sync_id has no
  log rows yet (a brand-new run). 404 when the sync_id doesn't exist in
  ``sync_history`` — the distinction matters because "no logs" is a
  legitimate transient state, but "no such sync" is an operator typo.
"""

from __future__ import annotations

import contextlib
import enum
import json
from datetime import UTC, datetime
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.routers.logs import LogEntry

router = APIRouter(prefix="/syncs", tags=["syncs"])

_MAX_LIMIT = 500


class SyncHistoryItem(BaseModel):
    """One decoded row from ``sync_history``.

    ``stats`` / ``cursor_before`` / ``cursor_after`` were stored as JSON
    strings and come back either as a parsed object or ``None``; we keep
    them permissive because the shape varies per connector.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    user_email: str
    platform: str
    started_at: str | None
    finished_at: str | None
    state: str
    error: str | None
    stats: dict[str, Any] | None
    cursor_before: Any = None
    cursor_after: Any = None
    # ``sync_history`` carries a ``sync_kind`` column (content_sync /
    # credential_refresh); declare it here so the nested detail response
    # model doesn't reject it under ``extra="forbid"``.
    sync_kind: str | None = None


class SyncProfile(BaseModel):
    """``sync_profiles`` row decoded. ``per_scope`` is the parsed
    ``per_scope_json`` (or ``None`` when the JSON fails to decode);
    ``per_scope_json`` stays in the payload so clients can still read
    the raw text if they need it."""

    model_config = ConfigDict(extra="forbid")

    sync_id: str
    user_email: str
    platform: str
    total_duration_seconds: float | None
    api_call_duration_seconds: float | None
    normalization_duration_seconds: float | None
    items_fetched: int | None
    items_new: int | None
    items_updated: int | None
    error_count: int | None
    per_scope_json: str | None = None
    per_scope: Any = None
    created_at: str


class SyncDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: SyncHistoryItem
    profile: SyncProfile | None


class SyncStateFilter(enum.StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class PlatformFilter(enum.StrEnum):
    TEAMS = "teams"
    MOODLE = "moodle"
    TELEGRAM = "telegram"


class SyncKindFilter(enum.StrEnum):
    CONTENT_SYNC = "content_sync"
    CREDENTIAL_REFRESH = "credential_refresh"


def _decode_row(row: aiosqlite.Row) -> SyncHistoryItem:
    """Decode a ``sync_history`` row into a typed :class:`SyncHistoryItem`.

    The DB stores ``stats`` / ``cursor_before`` / ``cursor_after`` as JSON
    text; we parse them eagerly and fall back to ``None`` (or the raw
    text for cursors) on bad JSON. The Pydantic model declared here is
    the single source of truth for the wire shape — callers can't
    accidentally bypass it by destructuring a dict and shipping a stale
    field set.
    """
    data: dict[str, Any] = dict(row)
    stats_json = data.pop("stats_json", None)
    if stats_json:
        try:
            data["stats"] = json.loads(stats_json)
        except (TypeError, ValueError):
            data["stats"] = None
    else:
        data["stats"] = None
    for key in ("cursor_before", "cursor_after"):
        raw = data.get(key)
        if raw:
            # Keep the raw text on parse failure; downstream can still
            # render it as a string.
            with contextlib.suppress(TypeError, ValueError):
                data[key] = json.loads(raw)
    return SyncHistoryItem(**data)


@router.get("", response_model=list[SyncHistoryItem])
async def list_syncs(
    ctx: ContextDep,
    user_email: str | None = None,
    platform: PlatformFilter | None = None,
    state: SyncStateFilter | None = None,
    # Default ``None`` (all kinds) keeps existing callers unaware of the
    # discriminator; the V2 Syncs page should pass ``content_sync`` and the
    # V6 "Refresh activity" tab should pass ``credential_refresh`` so the
    # two views don't cross-contaminate.
    kind: SyncKindFilter | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 100,
) -> list[SyncHistoryItem]:
    clauses: list[str] = []
    params: list[Any] = []
    if user_email:
        clauses.append("user_email = ?")
        params.append(user_email)
    if platform is not None:
        clauses.append("platform = ?")
        params.append(platform.value)
    if state is not None:
        clauses.append("state = ?")
        params.append(state.value)
    if kind is not None:
        clauses.append("sync_kind = ?")
        params.append(kind.value)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                f"""
                SELECT id, user_email, platform, started_at, finished_at,
                       state, error, stats_json, cursor_before, cursor_after
                FROM sync_history
                {where}
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                params,
            )
        ).fetchall()
    return [_decode_row(r) for r in rows]


# --- Diagnostics ----------------------------------------------------------
#
# IMPORTANT: ``/running`` MUST register before ``/{sync_id}``. FastAPI
# dispatches routes in registration order, so a wildcard ``/{sync_id}``
# registered first would capture ``/running`` as a literal sync-id and
# 404 (no such row). Keep the diagnostics block above ``sync_detail``.


class RunningSync(BaseModel):
    """One in-flight sync as observed in ``sync_history``.

    ``elapsed_seconds`` is computed at response time (now - started_at)
    rather than persisted: a long-running sync's elapsed value updates
    every poll without requiring a writer to refresh the row.

    ``items_fetched_so_far`` is intentionally nullable: the daemon's
    sync_profiles row only lands at log_complete, and there's no
    incremental counter on the running row today. Callers should treat
    None as "live count not available" rather than zero — the field is
    declared so a future enhancement can fill it in without a wire-shape
    break.
    """

    model_config = ConfigDict(extra="forbid")

    sync_id: str
    user_email: str
    platform: str
    started_at: str
    elapsed_seconds: float
    items_fetched_so_far: int | None


class RunningSyncList(BaseModel):
    """Wrapper for :class:`RunningSync` so OpenAPI gets a stable name."""

    model_config = ConfigDict(extra="forbid")

    items: list[RunningSync]


def _elapsed_since(iso_timestamp: str | None) -> float:
    """Seconds between ``iso_timestamp`` and now (UTC).

    Returns ``0.0`` when ``iso_timestamp`` is ``None`` or unparseable;
    the caller treats a non-positive value as "not started yet" rather
    than failing the whole request.
    """
    if not iso_timestamp:
        return 0.0
    try:
        started = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return 0.0
    now = datetime.now(tz=UTC)
    if started.tzinfo is None:
        # Defensive: legacy rows may carry naive timestamps. Treat them
        # as UTC rather than raising — operator visibility wins over
        # strict typing on a read endpoint.
        started = started.replace(tzinfo=UTC)
    delta = (now - started).total_seconds()
    # Clamp to >= 0: clock skew between writer and reader could yield a
    # negative elapsed which is operationally nonsense.
    return max(0.0, delta)


@router.get("/running", response_model=RunningSyncList)
async def list_running_syncs(ctx: ContextDep) -> RunningSyncList:
    """Every sync currently in flight (sync_history.state='running').

    Reads only from ``sync_history``: the worker pool tracks tasks in
    memory, but the canonical source for "is this sync still running"
    is the persisted row — a worker that crashed without patching its
    row is reconciled at next daemon boot
    (:meth:`SqliteSyncLogger.reconcile_orphaned_running`).
    """
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                """
                SELECT id, user_email, platform, started_at
                FROM sync_history
                WHERE state = 'running' AND finished_at IS NULL
                ORDER BY started_at ASC, id ASC
                """,
            )
        ).fetchall()
    items: list[RunningSync] = [
        RunningSync(
            sync_id=str(row["id"]),
            user_email=str(row["user_email"]),
            platform=str(row["platform"]),
            started_at=str(row["started_at"]) if row["started_at"] else "",
            elapsed_seconds=_elapsed_since(row["started_at"]),
            # See RunningSync.items_fetched_so_far docstring — None until
            # the pipeline grows an incremental counter.
            items_fetched_so_far=None,
        )
        for row in rows
    ]
    return RunningSyncList(items=items)


# --- Detail (must register AFTER /running so the wildcard does not shadow it)


@router.get("/{sync_id}", response_model=SyncDetailResponse)
async def sync_detail(sync_id: str, ctx: ContextDep) -> SyncDetailResponse:
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        history = await (
            await conn.execute("SELECT * FROM sync_history WHERE id = ?", (sync_id,))
        ).fetchone()
        if history is None:
            raise_http_exception(
                status.HTTP_404_NOT_FOUND,
                ErrorCode.NOT_FOUND,
                "sync not found",
            )
        profile = await (
            await conn.execute("SELECT * FROM sync_profiles WHERE sync_id = ?", (sync_id,))
        ).fetchone()
    history_model = _decode_row(history)
    profile_model: SyncProfile | None = None
    if profile is not None:
        profile_dict: dict[str, Any] = dict(profile)
        if profile_dict.get("per_scope_json"):
            try:
                profile_dict["per_scope"] = json.loads(profile_dict["per_scope_json"])
            except (TypeError, ValueError):
                profile_dict["per_scope"] = None
        profile_model = SyncProfile(**profile_dict)
    return SyncDetailResponse(
        history=history_model,
        profile=profile_model,
    )


# --- Recovery -------------------------------------------------------------


class LogList(BaseModel):
    """Wrapper around the per-sync log rows.

    The ``items`` field carries the same :class:`LogEntry` shape the
    primary ``/api/logs`` endpoint returns; this convenience wrapper
    exists only so a frontend that already knows a sync_id doesn't have
    to assemble the query string itself.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[LogEntry]


_LOGS_DEFAULT_LIMIT = 200
_LOGS_MAX_LIMIT = 1000


@router.get("/{sync_id}/logs", response_model=LogList)
async def list_sync_logs(
    sync_id: str,
    ctx: ContextDep,
    limit: Annotated[int, Query(ge=1, le=_LOGS_MAX_LIMIT)] = _LOGS_DEFAULT_LIMIT,
) -> LogList:
    """Logs for a single sync run by ``sync_id``.

    404 when ``sync_id`` doesn't exist in ``sync_history`` — telling an
    operator their typoed UUID returns "no logs" would be confusing. A
    valid sync_id with zero log rows still returns 200 with an empty
    list (a brand-new run that hasn't logged yet).
    """
    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        # First confirm the sync_id exists in sync_history; cheaper than
        # joining for read in case the logs table has stale rows from a
        # purged history row.
        history_row = await (
            await conn.execute(
                "SELECT 1 FROM sync_history WHERE id = ? LIMIT 1",
                (sync_id,),
            )
        ).fetchone()
        if history_row is None:
            raise_http_exception(
                status.HTTP_404_NOT_FOUND,
                ErrorCode.NOT_FOUND,
                "sync not found",
            )
        log_rows = await (
            await conn.execute(
                """
                SELECT id, timestamp, level, event,
                       user_email, platform, sync_id, fields_json
                FROM logs
                WHERE sync_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (sync_id, limit),
            )
        ).fetchall()
    entries: list[LogEntry] = []
    for row in log_rows:
        row_dict = dict(row)
        fields_json = row_dict.pop("fields_json", None)
        row_dict["context"] = json.loads(fields_json) if fields_json else None
        entries.append(LogEntry(**row_dict))
    return LogList(items=entries)
