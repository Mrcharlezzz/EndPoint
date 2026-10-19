"""Background loops that periodically prune the lifecycle-managed tables.

Two loop drivers live here:

* :func:`run_lifecycle_prune_loop` — wraps the three TTL prune helpers
  (sync_history, dead_letter, audit_log) in one shutdown-aware loop
  that runs hourly by default. Independent failures are logged and
  the next prune still runs, matching the pattern used by
  :mod:`ingestion.daemon.observability_loops`.

The loop is composed once in :func:`runtime.run`. Tests can drive it
directly by passing a short interval and a manually-set shutdown
event.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from ingestion.daemon.lifecycle.cleanup import (
    prune_audit_log,
    prune_dead_letter,
    prune_sync_history,
)
from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path


async def run_lifecycle_prune_loop(
    shutdown: asyncio.Event,
    *,
    db_path: Path,
    sync_history_retention_days: int,
    dead_letter_retention_days: int,
    audit_log_retention_days: int,
    interval_seconds: int = 3600,
) -> None:
    """Hourly prune of sync_history, dead_letter, and audit_log.

    Each prune runs independently inside its own try/except so a
    failure on one table does not block the others. ``retention_days``
    of ``0`` for any table hard-disables that prune (the kill switch
    mirrors :func:`prune_*` semantics: 0 means "delete everything"
    only when the loop actively runs, so a per-table 0 is treated as
    "don't run this prune at all" inside the loop driver).

    Set ``interval_seconds=0`` to disable the entire loop. Useful in
    tests that exercise the prune helpers directly.
    """
    log = get_logger(__name__)
    if interval_seconds <= 0:
        log.info("lifecycle_prune_loop.disabled")
        return
    while not shutdown.is_set():
        if sync_history_retention_days > 0:
            try:
                deleted = await prune_sync_history(
                    db_path, retention_days=sync_history_retention_days
                )
                if deleted:
                    log.info(
                        "lifecycle_prune_loop.sync_history_pruned",
                        deleted=deleted,
                        retention_days=sync_history_retention_days,
                    )
            except Exception:
                log.exception("lifecycle_prune_loop.sync_history_errored")
        if dead_letter_retention_days > 0:
            try:
                deleted = await prune_dead_letter(
                    db_path, retention_days=dead_letter_retention_days
                )
                if deleted:
                    log.info(
                        "lifecycle_prune_loop.dead_letter_pruned",
                        deleted=deleted,
                        retention_days=dead_letter_retention_days,
                    )
            except Exception:
                log.exception("lifecycle_prune_loop.dead_letter_errored")
        if audit_log_retention_days > 0:
            try:
                deleted = await prune_audit_log(db_path, retention_days=audit_log_retention_days)
                if deleted:
                    log.info(
                        "lifecycle_prune_loop.audit_log_pruned",
                        deleted=deleted,
                        retention_days=audit_log_retention_days,
                    )
            except Exception:
                log.exception("lifecycle_prune_loop.audit_log_errored")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
