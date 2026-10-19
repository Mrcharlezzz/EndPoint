"""/api/alerts/rules (CRUD) and /api/alerts/firings (read)."""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from ingestion.daemon.alerts import rules as rule_store
from ingestion.daemon.alerts.rules import Comparator
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.schemas import OkResponse
from ingestion.daemon.store import audit as audit_store

router = APIRouter(prefix="/alerts", tags=["alerts"])


class RuleCreate(BaseModel):
    name: str
    metric: str
    comparator: Comparator
    threshold: float
    window_seconds: int | None = None
    cooldown_seconds: int = Field(default=300, ge=0)
    user_email: str | None = None
    platform: str | None = None
    enabled: bool = True


class RuleEnablePatch(BaseModel):
    enabled: bool


class AlertRuleUpdate(BaseModel):
    """Partial edit surface for an alert rule. ``enabled`` is deliberately
    absent — use ``PATCH /rules/{id}/enabled`` for the on/off toggle so
    the two code paths don't silently race on the same row."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    metric: str | None = None
    comparator: Comparator | None = None
    threshold: float | None = None
    window_seconds: int | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0)
    user_email: str | None = None
    platform: str | None = None


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    enabled: bool
    metric: str
    comparator: str
    threshold: float
    window_seconds: int | None
    cooldown_seconds: int
    user_email: str | None
    platform: str | None


class AlertFiring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    rule_id: int
    fired_at: str
    metric_value: float
    user_email: str | None
    platform: str | None
    state: str
    resolved_at: str | None
    trigger_context: dict[str, Any] | None


class AlertFiringDetail(BaseModel):
    """Heavyweight per-firing view for the drawer/drill-down screen.

    Intentionally separate from ``AlertFiring`` so the list endpoint stays
    cheap — loading every trigger payload and joining rules for a table
    render would turn the History tab into a pull-the-world query."""

    model_config = ConfigDict(extra="forbid")

    id: int
    rule_id: int
    rule_name: str | None
    fired_at: str
    severity: str | None
    state: str
    resolved_at: str | None
    triggering_metric_value: float
    affected_users: list[str]
    raw_event_payload: dict[str, Any]


@router.get("/rules", response_model=list[AlertRuleOut])
async def list_rules(ctx: ContextDep, enabled_only: bool = False) -> list[AlertRuleOut]:
    rules = await rule_store.list_rules(ctx.config.sqlite_path, enabled_only=enabled_only)
    return [_rule_to_model(r) for r in rules]


@router.post("/rules", status_code=status.HTTP_201_CREATED, response_model=AlertRuleOut)
async def create_rule(payload: RuleCreate, ctx: ContextDep) -> AlertRuleOut:
    rule_id = await rule_store.create_rule(
        ctx.config.sqlite_path,
        name=payload.name,
        metric=payload.metric,
        comparator=payload.comparator,
        threshold=payload.threshold,
        window_seconds=payload.window_seconds,
        cooldown_seconds=payload.cooldown_seconds,
        user_email=payload.user_email,
        platform=payload.platform,
        enabled=payload.enabled,
    )
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="alerts.rule_created",
        target=payload.name,
        after=payload.model_dump(),
    )
    rule = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    assert rule is not None
    return _rule_to_model(rule)


@router.patch("/rules/{rule_id}/enabled", response_model=AlertRuleOut)
async def set_rule_enabled(
    rule_id: int,
    payload: RuleEnablePatch,
    ctx: ContextDep,
) -> AlertRuleOut:
    previous = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    if previous is None:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "rule not found",
        )
    await rule_store.set_enabled(ctx.config.sqlite_path, rule_id, enabled=payload.enabled)
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="alerts.rule_enabled" if payload.enabled else "alerts.rule_disabled",
        target=str(rule_id),
        before={"enabled": previous.enabled},
        after={"enabled": payload.enabled},
    )
    rule = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    assert rule is not None
    return _rule_to_model(rule)


@router.patch("/rules/{rule_id}", response_model=AlertRuleOut)
async def update_rule(
    rule_id: int,
    payload: AlertRuleUpdate,
    ctx: ContextDep,
) -> AlertRuleOut:
    """Full-rule edit. Accepts any subset of the mutable fields; omits
    ``enabled`` to preserve the dedicated toggle's audit trail."""
    previous = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    if previous is None:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "rule not found",
        )
    # ``exclude_unset=True`` gives the PATCH semantics: only fields the client
    # actually sent are applied. A missing key means "leave the column alone",
    # which differs from ``None`` — which would null the column.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _rule_to_model(previous)
    await rule_store.update_rule(ctx.config.sqlite_path, rule_id, changes)
    after = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    assert after is not None
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="alerts.rule_updated",
        target=str(rule_id),
        before=_rule_to_model(previous).model_dump(),
        after=_rule_to_model(after).model_dump(),
    )
    return _rule_to_model(after)


@router.delete("/rules/{rule_id}", response_model=OkResponse)
async def delete_rule(rule_id: int, ctx: ContextDep) -> OkResponse:
    previous = await rule_store.get_rule(ctx.config.sqlite_path, rule_id)
    if previous is None:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "rule not found",
        )
    await rule_store.delete_rule(ctx.config.sqlite_path, rule_id)
    await audit_store.write_entry(
        ctx.config.sqlite_path,
        actor="admin",
        action="alerts.rule_deleted",
        target=str(rule_id),
        before=_rule_to_model(previous).model_dump(),
    )
    return OkResponse(ok=True)


@router.get("/firings", response_model=list[AlertFiring])
async def list_firings(
    ctx: ContextDep,
    rule_id: int | None = None,
    limit: int = 100,
) -> list[AlertFiring]:
    clauses: list[str] = []
    params: list[Any] = []
    if rule_id is not None:
        clauses.append("rule_id = ?")
        params.append(rule_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))
    import json

    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (
            await conn.execute(
                f"""
                SELECT id, rule_id, fired_at, metric_value, user_email, platform,
                       state, resolved_at, trigger_context_json
                FROM alert_firings
                {where}
                ORDER BY fired_at DESC
                LIMIT ?
                """,
                params,
            )
        ).fetchall()
    result: list[AlertFiring] = []
    for r in rows:
        row = dict(r)
        ctx_json = row.pop("trigger_context_json", None)
        row["trigger_context"] = json.loads(ctx_json) if ctx_json else None
        result.append(AlertFiring(**row))
    return result


@router.get("/firings/{firing_id}", response_model=AlertFiringDetail)
async def get_firing(firing_id: int, ctx: ContextDep) -> AlertFiringDetail:
    """Drawer/drill-down view for one firing. Pulls the rule in the same
    query so the response carries both the firing-level payload and the
    rule-level context (name) the dashboard renders as a heading."""
    import json

    async with aiosqlite.connect(ctx.config.sqlite_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (
            await conn.execute(
                """
                SELECT f.id, f.rule_id, f.fired_at, f.metric_value, f.user_email,
                       f.platform, f.state, f.resolved_at, f.trigger_context_json,
                       r.name AS rule_name
                FROM alert_firings f
                LEFT JOIN alert_rules r ON r.id = f.rule_id
                WHERE f.id = ?
                """,
                (firing_id,),
            )
        ).fetchone()
    if row is None:
        raise_http_exception(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "firing not found",
        )
    mapping = dict(row)
    ctx_json = mapping.get("trigger_context_json")
    payload = json.loads(ctx_json) if ctx_json else {}
    # ``affected_users`` is a list even though the schema only persists a
    # single ``user_email`` per firing — the envelope mirrors the handoff's
    # drawer spec (§7.9) so the UI can render "N users" without per-row
    # normalisation. If the trigger payload names additional users we
    # surface them too; duplicates are squashed while preserving order.
    users: list[str] = []
    if mapping.get("user_email"):
        users.append(str(mapping["user_email"]))
    for candidate in payload.get("affected_users", []) if isinstance(payload, dict) else []:
        if isinstance(candidate, str) and candidate not in users:
            users.append(candidate)
    return AlertFiringDetail(
        id=int(mapping["id"]),
        rule_id=int(mapping["rule_id"]),
        rule_name=mapping.get("rule_name"),
        fired_at=mapping["fired_at"],
        # ``severity`` is not yet modeled at the rule level; surface None so
        # the contract is stable for when the column lands.
        severity=None,
        state=mapping["state"],
        resolved_at=mapping.get("resolved_at"),
        triggering_metric_value=float(mapping["metric_value"]),
        affected_users=users,
        raw_event_payload=payload if isinstance(payload, dict) else {},
    )


def _rule_to_model(rule: rule_store.AlertRule) -> AlertRuleOut:
    return AlertRuleOut(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        metric=rule.metric,
        comparator=rule.comparator.value,
        threshold=rule.threshold,
        window_seconds=rule.window_seconds,
        cooldown_seconds=rule.cooldown_seconds,
        user_email=rule.user_email,
        platform=rule.platform,
    )
