"""/api/config: read-all, get one overlay key, set one overlay key.

This router also hosts the per-user schedule-override resource at
``/api/config/overrides``. Overrides are a dedicated resource rather than a
dotted key under the generic ``/api/config/overlay`` surface because the
``user_schedule_overrides`` table has its own composite-PK schema distinct
from :class:`DaemonConfig` — shoehorning it into the overlay would skip the
model-backed validation the overlay endpoints rely on, and the payload shape
doesn't map to a single dotted key anyway.

Bulk endpoint (slice 7):

* ``POST /api/config/overlay/reset-all`` — clear every overlay key in
  one atomic-ish operation. Useful as an incident-response "back to
  defaults" lever. Idempotent: a second call returns ``cleared_keys=[]``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field, RootModel

from ingestion.daemon import schedule_queries
from ingestion.daemon.api.audit_actor import make_actor
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.schemas import OkResponse
from ingestion.daemon.config import DaemonConfig, load_config
from ingestion.daemon.services.results import OverlayResetResult
from ingestion.daemon.store import audit as audit_store

router = APIRouter(prefix="/config", tags=["config"])


class OverlaySet(BaseModel):
    value: Any


class OverlayMap(RootModel[dict[str, Any]]):
    """Flat dotted-key-to-value map as stored in ``config_overlay``."""


class OverlaySetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any


@router.get("", response_model=DaemonConfig)
async def effective_config(ctx: ContextDep) -> DaemonConfig:
    """Return the fully merged config (defaults + env + overlay)."""
    return load_config(ctx.store)


@router.get("/overlay", response_model=OverlayMap)
async def list_overlay(ctx: ContextDep) -> OverlayMap:
    return OverlayMap(ctx.store.all_overlay())


@router.post("/overlay/reset-all", response_model=OverlayResetResult)
async def reset_all_overlay(
    ctx: ContextDep,
    request: Request,
) -> OverlayResetResult:
    """Clear every overlay key in one operation. Idempotent.

    Snapshot the full overlay map BEFORE deletion so the audit row's
    ``before`` payload preserves the pre-reset state — operators can
    reconstruct the prior config from the audit log alone if a reset
    turns out to have been a mistake.

    The ``deleted`` loop is per-key rather than a single SQL TRUNCATE
    because the existing :class:`LocalStore` only exposes per-key
    deletes; a future store-level batch delete would let this become
    truly atomic. Today the window between deleting key 1 and key N
    is small (in-process, single-thread under the write lock).

    Returns ``cleared_keys=[]`` when the overlay was already empty —
    the audit row is still emitted so the action attempt is recorded.
    """
    snapshot = ctx.store.all_overlay()
    cleared = sorted(snapshot.keys())
    for key in cleared:
        ctx.store.delete_overlay(key)
    writer_actor = make_actor(request)
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor=writer_actor,
        action="config.overlay_reset_all",
        target="overlay",
        before={"overlay": snapshot, "key_count": len(cleared)},
        after={"cleared_keys": cleared, "key_count": 0},
    )
    return OverlayResetResult(cleared_keys=cleared)


@router.put("/overlay/{key:path}", response_model=OverlaySetResponse)
async def set_overlay(
    key: str,
    payload: OverlaySet,
    ctx: ContextDep,
) -> OverlaySetResponse:
    # Validate: the key must resolve against the pydantic model, otherwise
    # the overlay silently no-ops at load_config time. Reject with a clear
    # error instead of accepting a row the runtime will log and drop.
    if not _key_resolves(key):
        raise_http_exception(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.VALIDATION_ERROR,
            f"unknown config key: {key!r}",
        )
    # Type-check the value against the pydantic schema BEFORE writing it.
    # Without this a caller could write {"value": "not-a-number"} under a
    # numeric key; load_config would reject the overlay at the next tick
    # and silently keep the bogus row around.
    error = _validate_overlay_value(key, payload.value)
    if error is not None:
        raise_http_exception(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_ERROR,
            error,
            hint=f"key: {key}",
        )
    before = ctx.store.get_overlay(key)
    ctx.store.set_overlay(key, payload.value)
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="config.overlay_set",
        target=key,
        before={"value": before} if before is not None else None,
        after={"value": payload.value},
    )
    return OverlaySetResponse(key=key, value=payload.value)


@router.delete("/overlay/{key:path}", response_model=OkResponse)
async def delete_overlay(key: str, ctx: ContextDep) -> OkResponse:
    before = ctx.store.get_overlay(key)
    if before is None:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            f"overlay key {key!r} not set",
        )
    ctx.store.delete_overlay(key)
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="config.overlay_cleared",
        target=key,
        before={"value": before},
    )
    return OkResponse(ok=True)


def _key_resolves(dotted: str) -> bool:
    """Walk the DaemonConfig schema to confirm the dotted key maps to a field."""
    from ingestion.daemon.config import DaemonConfig

    schema: dict[str, Any] = DaemonConfig().model_dump()
    parts = dotted.split(".")
    cursor: Any = schema
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def _validate_overlay_value(dotted: str, value: Any) -> str | None:
    """Simulate applying ``value`` at ``dotted`` against a fresh DaemonConfig.

    Returns a short error string if validation fails, ``None`` if the value
    is accepted. Avoids the "write now, discover later" problem where a
    bogus row is logged as unknown at the next load_config tick.
    """
    from pydantic import ValidationError

    from ingestion.daemon.config import DaemonConfig

    data: dict[str, Any] = DaemonConfig().model_dump()
    parts = dotted.split(".")
    cursor: Any = data
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return f"key path {dotted!r} does not exist on DaemonConfig"
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return f"key path {dotted!r} does not exist on DaemonConfig"
    cursor[parts[-1]] = value
    try:
        DaemonConfig.model_validate(data)
    except ValidationError as exc:
        return exc.errors(include_url=False, include_input=False)[0].get("msg", "invalid value")
    return None


# --- Per-user schedule overrides -------------------------------------------
#
# Dedicated resource — see module docstring. The ``user_schedule_overrides``
# table has composite PK ``(user_email, platform, scope)`` so one email can
# legitimately carry multiple rows: one per (platform, scope) combination.
# The URL surface uses ``{email}`` as the key but the body carries platform
# and scope so a single PUT targets a single row. GET ``/{email}`` returns
# the full list of that email's rows, DELETE ``/{email}`` wipes them all.


class UserScheduleOverride(BaseModel):
    """Shape of one ``user_schedule_overrides`` row on the wire."""

    model_config = ConfigDict(extra="forbid")

    user_email: str
    platform: str
    scope: str
    interval_seconds: int
    updated_at: str


class UserScheduleOverrideBody(BaseModel):
    """Editable fields of an override row. Excludes user_email (URL) and
    updated_at (server-populated)."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1)
    scope: str = Field(default="default", min_length=1)
    # SQLite enforces >= 1 inside set_user_interval_override; validate here
    # too so the 422 comes back before touching the DB.
    interval_seconds: int = Field(ge=1)


@router.get("/overrides", response_model=list[UserScheduleOverride])
async def list_schedule_overrides(ctx: ContextDep) -> list[UserScheduleOverride]:
    rows = await schedule_queries.list_all_overrides(ctx.config.sqlite_path)
    return [UserScheduleOverride(**dict(row)) for row in rows]


@router.get("/overrides/{email}", response_model=list[UserScheduleOverride])
async def get_schedule_overrides_for_email(
    email: str, ctx: ContextDep
) -> list[UserScheduleOverride]:
    """Every override row for the given email. 404 when the email has none."""
    rows = await schedule_queries.list_overrides_for_email(ctx.config.sqlite_path, email)
    if not rows:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            f"no schedule overrides for {email!r}",
        )
    return [UserScheduleOverride(**dict(row)) for row in rows]


@router.put("/overrides/{email}", response_model=UserScheduleOverride)
async def upsert_schedule_override(
    email: str,
    payload: UserScheduleOverrideBody,
    ctx: ContextDep,
) -> UserScheduleOverride:
    # We do NOT verify ``email`` exists in the ``users`` table here: operators
    # must be able to pre-stage an override before the user's first sync
    # creates their credential row (option (a) in the task spec).
    db_path = ctx.config.sqlite_path
    before = await schedule_queries.get_override_row(
        db_path, email, payload.platform, payload.scope
    )
    await schedule_queries.set_user_interval_override(
        db_path,
        user_email=email,
        platform=payload.platform,
        interval_seconds=payload.interval_seconds,
        scope=payload.scope,
    )
    after = await schedule_queries.get_override_row(db_path, email, payload.platform, payload.scope)
    if after is None:  # pragma: no cover — the row was just written
        raise_http_exception(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorCode.INTERNAL_ERROR,
            "upsert succeeded but row is missing on read-back",
        )
    await audit_store.write_entry(
        db_path,
        actor="admin",
        action="config.schedule_override_set",
        target=f"{email}/{payload.platform}/{payload.scope}",
        before=before,
        after=after,
    )
    return UserScheduleOverride(**after)


@router.delete("/overrides/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_overrides_for_email(email: str, ctx: ContextDep) -> None:
    """Delete every override row for the given email. 404 if none exist."""
    db_path = ctx.config.sqlite_path
    before = await schedule_queries.list_overrides_for_email(db_path, email)
    if not before:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            f"no schedule overrides for {email!r}",
        )
    deleted = await schedule_queries.clear_overrides_for_email(db_path, email)
    await audit_store.write_entry(
        db_path,
        actor="admin",
        action="config.schedule_override_cleared",
        target=email,
        before={"rows": before, "count": deleted},
    )
    return None
