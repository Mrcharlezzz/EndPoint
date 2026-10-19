"""Slice 13 part B: idempotent default-rule seeding.

:func:`seed_default_rules` runs at boot and inserts a small opinionated
set of rules if they don't already exist. Pin:

* On a clean DB the three default rules (``dead_letter_burst``,
  ``sync_duration_p95_high``, ``credential_refresh_storm``) are inserted.
* Calling it twice on the same DB is a no-op (idempotent on rule.name).
* Two daemons booting concurrently against the same DB don't produce
  duplicates: the UNIQUE index on ``alert_rules.name`` rejects the second
  insert and the impl swallows ``IntegrityError`` as "already seeded".
* The runtime gate ``alerts.seed_default_rules=False`` is respected
  via the wiring in :mod:`runtime` (verified through the config
  surface; the seed function itself is unconditional and the gate
  lives in :func:`runtime._start_observability_loops`).

The seed reads + writes via :mod:`aiosqlite`; tests inspect with stdlib
:mod:`sqlite3` for synchronous, easy-to-reason-about assertions.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from ingestion.daemon.alerts.rules import Comparator, create_rule, list_rules
from ingestion.daemon.alerts.seed import seed_default_rules
from ingestion.daemon.config import AlertsConfig, DaemonConfig
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


# ---------------------------------------------------------------------------
# happy path: clean DB -> 3 rules inserted
# ---------------------------------------------------------------------------


async def test_seed_inserts_three_default_rules_on_first_boot(db_path: Path) -> None:
    """Clean DB → all three opinionated rules are inserted. Names match
    the ones documented in the impl: dead_letter_burst,
    sync_duration_p95_high, credential_refresh_storm."""
    inserted = await seed_default_rules(db_path)
    assert sorted(inserted) == sorted(
        [
            "dead_letter_burst",
            "sync_duration_p95_high",
            "credential_refresh_storm",
        ]
    )
    rules = await list_rules(db_path)
    names = {r.name for r in rules}
    assert names == {"dead_letter_burst", "sync_duration_p95_high", "credential_refresh_storm"}


async def test_seed_uses_documented_thresholds(db_path: Path) -> None:
    """Thresholds match the impl docstring exactly:

    * dead_letter_count > 5
    * sync_duration_seconds_p95 > 300
    * refresh_failures_total > 10

    Pin these so a refactor that flips them wakes a reviewer up."""
    await seed_default_rules(db_path)
    rules = {r.name: r for r in await list_rules(db_path)}
    assert rules["dead_letter_burst"].metric == "dead_letter_count"
    assert rules["dead_letter_burst"].threshold == 5.0
    assert rules["dead_letter_burst"].comparator is Comparator.GT
    assert rules["sync_duration_p95_high"].metric == "sync_duration_seconds_p95"
    assert rules["sync_duration_p95_high"].threshold == 300.0
    assert rules["credential_refresh_storm"].metric == "refresh_failures_total"
    assert rules["credential_refresh_storm"].threshold == 10.0


# ---------------------------------------------------------------------------
# idempotency: second run is a no-op
# ---------------------------------------------------------------------------


async def test_seed_is_idempotent_on_second_call(db_path: Path) -> None:
    """Calling seed twice → first call inserts 3, second returns []. The
    table still has exactly 3 rows (unchanged from the first call)."""
    first = await seed_default_rules(db_path)
    second = await seed_default_rules(db_path)
    assert len(first) == 3
    assert second == []
    rules = await list_rules(db_path)
    assert len(rules) == 3


async def test_seed_preserves_operator_pre_seeded_rules(db_path: Path) -> None:
    """An operator who pre-created their own ``dead_letter_burst`` with
    a different threshold keeps their version. The seed must not
    overwrite — only fill in the missing names."""
    await create_rule(
        db_path,
        name="dead_letter_burst",
        metric="dead_letter_count",
        comparator=Comparator.GT,
        threshold=42.0,  # operator-customised
        cooldown_seconds=60,
    )
    inserted = await seed_default_rules(db_path)
    # Only the two rules NOT in the table should be inserted.
    assert sorted(inserted) == sorted(["sync_duration_p95_high", "credential_refresh_storm"])
    # The pre-seeded threshold stays.
    rules = {r.name: r for r in await list_rules(db_path)}
    assert rules["dead_letter_burst"].threshold == 42.0


# ---------------------------------------------------------------------------
# concurrent-boot race: UNIQUE constraint catches the duplicate
# ---------------------------------------------------------------------------


async def test_concurrent_seed_does_not_duplicate_rules(db_path: Path) -> None:
    """Two daemons against the same SQLite file (rare but possible in a
    misconfigured docker-compose) race to seed. Pin: the
    ``alert_rules.name`` UNIQUE index rejects the second insert; the
    impl catches :class:`aiosqlite.IntegrityError` and continues. The
    end state is exactly 3 rules, never 6."""
    # Run two seeds concurrently. asyncio.gather is enough to interleave
    # them at the aiosqlite layer — the UNIQUE index serialises the two
    # inserts at the SQLite level.
    results = await asyncio.gather(
        seed_default_rules(db_path),
        seed_default_rules(db_path),
    )
    # The combined inserted set is exactly the 3 default rule names.
    combined = sorted(set(results[0]) | set(results[1]))
    assert combined == sorted(
        ["dead_letter_burst", "sync_duration_p95_high", "credential_refresh_storm"]
    )
    # The DB has exactly 3 rows.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]
    finally:
        conn.close()
    assert count == 3


# ---------------------------------------------------------------------------
# config gate: seed_default_rules=False is respected at the runtime layer
# ---------------------------------------------------------------------------


def test_default_alerts_config_seeds_default_rules() -> None:
    """Default-on: a clean :class:`AlertsConfig` has
    ``seed_default_rules=True`` so an operator who never touched the
    config still gets the opinionated set."""
    cfg = AlertsConfig()
    assert cfg.seed_default_rules is True


def test_alerts_config_can_disable_seed() -> None:
    """An operator who wants to manage rules themselves sets the field
    to False. Pin: the field accepts the override."""
    cfg = AlertsConfig(seed_default_rules=False)
    assert cfg.seed_default_rules is False


def test_daemon_config_carries_alerts_config() -> None:
    """The runtime path reads ``runtime.config.alerts.seed_default_rules``;
    pin that the field round-trips via :class:`DaemonConfig` so the
    runtime gate has a value to read."""
    cfg = DaemonConfig()
    assert hasattr(cfg, "alerts")
    assert cfg.alerts.seed_default_rules is True
