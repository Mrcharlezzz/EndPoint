"""Idempotent default-rule seeding for the alert engine.

Slice 13 ships a small set of opinionated rules that pop up the moment
something is wrong without an operator having to crack open the SQL
console. The seed runs once at daemon startup; rules are matched on
unique ``name`` so re-running is a no-op:

* ``dead_letter_burst`` — :data:`metric=dead_letter_count`, fires when
  more than 5 platform/user pairs are currently suppressed. Catches
  cascading platform failures (every Teams account in dead-letter
  state at the same time).
* ``sync_duration_p95_high`` — :data:`metric=sync_duration_seconds_p95`,
  300s threshold over a 1-hour window. A sync that consistently runs
  over 5 minutes is dragging and probably needs investigation.
* ``credential_refresh_storm`` — :data:`metric=refresh_failures_total`,
  fires when more than 10 credential-refresh syncs fail in 1h. The
  refresh path failing in volume is usually a credential-system
  outage (login.microsoftonline.com, Moodle SSO, etc.).

Operators who want to disable, edit, or extend these can do it through
the standard rules API once the rule is in the table — the seed only
runs on first boot when the rule name doesn't already exist. To stop
seeding entirely, set ``alerts.seed_default_rules=false``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from ingestion.daemon.alerts.rules import Comparator, create_rule
from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path


# Default rules. Tuple shape: (name, metric, comparator, threshold,
# window_seconds, cooldown_seconds, platform). ``platform`` is None
# (global) for now; an operator can clone-and-scope a rule per platform
# without touching this list.
_DEFAULT_RULES: tuple[tuple[str, str, Comparator, float, int | None, int, str | None], ...] = (
    (
        "dead_letter_burst",
        "dead_letter_count",
        Comparator.GT,
        5.0,
        # dead_letter_count ignores window_seconds (snapshot of the
        # current suppressed set). Pass None so the rule's window
        # column is NULL and the metric reads the whole table.
        None,
        # 1h cooldown — we don't want a paging storm on a sustained
        # platform outage. The dashboard surfaces every firing
        # individually; the page is for the on-call's pager.
        3600,
        None,
    ),
    (
        "sync_duration_p95_high",
        "sync_duration_seconds_p95",
        Comparator.GT,
        300.0,
        3600,  # 1-hour window
        3600,  # 1h cooldown
        None,
    ),
    (
        "credential_refresh_storm",
        "refresh_failures_total",
        Comparator.GT,
        10.0,
        3600,  # 1-hour window
        1800,  # 30min cooldown — catch back-to-back storms
        None,
    ),
)


async def seed_default_rules(db_path: Path) -> list[str]:
    """Insert the default rule set if missing. Returns names actually inserted.

    Idempotent: each rule is created only when no row with the same name
    exists. Operators who pre-seeded their own copy of a rule (same
    name) keep their version untouched.

    The lookup runs against the ``alert_rules`` table directly via a
    cheap ``SELECT 1 ... LIMIT 1`` per rule. ``alert_rules.name`` is
    UNIQUE so the absence-check has at most one row to fetch.
    """
    log = get_logger(__name__)
    inserted: list[str] = []
    existing = await _existing_rule_names(db_path)
    for (
        name,
        metric,
        comparator,
        threshold,
        window_seconds,
        cooldown_seconds,
        platform,
    ) in _DEFAULT_RULES:
        if name in existing:
            continue
        try:
            await create_rule(
                db_path,
                name=name,
                metric=metric,
                comparator=comparator,
                threshold=threshold,
                window_seconds=window_seconds,
                cooldown_seconds=cooldown_seconds,
                platform=platform,
            )
        except aiosqlite.IntegrityError:
            # Race: another startup raced us to the insert (multiple
            # daemons against the same DB). Treat as already-seeded.
            log.info("alerts.seed_race_skipped", name=name)
            continue
        inserted.append(name)
        log.info("alerts.seed_inserted", name=name, metric=metric)
    return inserted


async def _existing_rule_names(db_path: Path) -> set[str]:
    async with aiosqlite.connect(db_path) as conn:
        rows = await (await conn.execute("SELECT name FROM alert_rules")).fetchall()
    return {str(r[0]) for r in rows}


__all__ = ["seed_default_rules"]
