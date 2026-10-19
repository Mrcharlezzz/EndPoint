"""CRUD for the ``alert_rules`` table.

A rule is a scalar comparison: ``metric COMPARATOR threshold``. Optional
``user_email`` and ``platform`` scope a rule to a specific row; leaving
both NULL means "any row that matches the metric query."

The control panel in phase 5 owns the full CRUD surface. Phase 4 only
needs create/list/update/delete so the engine tests can seed rules.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path


class Comparator(enum.StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="


_COMPARATORS = {c.value: c for c in Comparator}


def _comparator_applies(value: float, comparator: Comparator, threshold: float) -> bool:
    if comparator is Comparator.GT:
        return value > threshold
    if comparator is Comparator.GTE:
        return value >= threshold
    if comparator is Comparator.LT:
        return value < threshold
    if comparator is Comparator.LTE:
        return value <= threshold
    return value == threshold


@dataclass(frozen=True)
class AlertRule:
    id: int
    name: str
    enabled: bool
    metric: str
    comparator: Comparator
    threshold: float
    window_seconds: int | None
    cooldown_seconds: int
    user_email: str | None
    platform: str | None

    def evaluate(self, value: float) -> bool:
        return _comparator_applies(value, self.comparator, self.threshold)


async def create_rule(
    db_path: Path,
    *,
    name: str,
    metric: str,
    comparator: Comparator,
    threshold: float,
    window_seconds: int | None = None,
    cooldown_seconds: int = 300,
    user_email: str | None = None,
    platform: str | None = None,
    enabled: bool = True,
) -> int:
    now = datetime.now(tz=UTC).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            """
            INSERT INTO alert_rules (
                name, enabled, metric, comparator, threshold, window_seconds,
                cooldown_seconds, user_email, platform, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                int(enabled),
                metric,
                comparator.value,
                threshold,
                window_seconds,
                cooldown_seconds,
                user_email,
                platform,
                now,
                now,
            ),
        )
        await conn.commit()
        rule_id = cursor.lastrowid
    if rule_id is None:
        raise RuntimeError("alert_rules insert returned no rowid")
    return rule_id


async def list_rules(db_path: Path, *, enabled_only: bool = False) -> list[AlertRule]:
    sql = """
        SELECT id, name, enabled, metric, comparator, threshold, window_seconds,
               cooldown_seconds, user_email, platform
        FROM alert_rules
    """
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    async with aiosqlite.connect(db_path) as conn:
        rows = await (await conn.execute(sql)).fetchall()
    return [_row_to_rule(r) for r in rows]


async def get_rule(db_path: Path, rule_id: int) -> AlertRule | None:
    async with aiosqlite.connect(db_path) as conn:
        row = await (
            await conn.execute(
                """
                SELECT id, name, enabled, metric, comparator, threshold, window_seconds,
                       cooldown_seconds, user_email, platform
                FROM alert_rules WHERE id = ?
                """,
                (rule_id,),
            )
        ).fetchone()
    if row is None:
        return None
    return _row_to_rule(row)


async def set_enabled(db_path: Path, rule_id: int, enabled: bool) -> None:
    now = datetime.now(tz=UTC).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE alert_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now, rule_id),
        )
        await conn.commit()


# Columns the PATCH endpoint is allowed to touch. ``enabled`` is deliberately
# excluded — it already has a dedicated toggle (``/enabled``) with its own
# audit action labels, and reusing this helper would let a malformed edit
# silently flip it.
_UPDATABLE_COLUMNS = (
    "name",
    "metric",
    "comparator",
    "threshold",
    "window_seconds",
    "cooldown_seconds",
    "user_email",
    "platform",
)


async def update_rule(db_path: Path, rule_id: int, fields: dict[str, Any]) -> None:
    """Patch an existing rule's fields in-place.

    Unknown keys are ignored rather than raised — callers validate at the API
    boundary via Pydantic, so anything that reaches here is already a known
    field. Passing an empty ``fields`` dict is a no-op (but still bumps
    ``updated_at``) so callers don't have to pre-check.
    """
    assignments: list[str] = []
    params: list[Any] = []
    for column, value in fields.items():
        if column not in _UPDATABLE_COLUMNS:
            continue
        if column == "comparator" and isinstance(value, Comparator):
            value = value.value
        assignments.append(f"{column} = ?")
        params.append(value)
    now = datetime.now(tz=UTC).isoformat()
    assignments.append("updated_at = ?")
    params.append(now)
    params.append(rule_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            f"UPDATE alert_rules SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        await conn.commit()


async def delete_rule(db_path: Path, rule_id: int) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        await conn.commit()


def _row_to_rule(row: Any) -> AlertRule:
    comparator_value = str(row[4])
    comparator = _COMPARATORS.get(comparator_value)
    if comparator is None:
        raise ValueError(f"alert_rules.comparator={comparator_value!r} is not recognised")
    return AlertRule(
        id=int(row[0]),
        name=str(row[1]),
        enabled=bool(row[2]),
        metric=str(row[3]),
        comparator=comparator,
        threshold=float(row[5]),
        window_seconds=int(row[6]) if row[6] is not None else None,
        cooldown_seconds=int(row[7]),
        user_email=(str(row[8]) if row[8] is not None else None),
        platform=(str(row[9]) if row[9] is not None else None),
    )
