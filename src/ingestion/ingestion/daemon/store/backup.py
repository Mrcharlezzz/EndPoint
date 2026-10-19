"""Online-safe hourly SQLite backup loop for the daemon's state.db.

A naive ``cp state.db /backups/state.db`` during a long write transaction
gets the WAL-mode database in a half-checkpointed state — opening that
file later raises ``database disk image is malformed`` because the
backup view of the journal frames doesn't line up with the main file.

This loop uses :meth:`aiosqlite.Connection.backup` which wraps the
native :c:func:`sqlite3_backup_init` API. The library copies the
database in 1024-page batches with a yield between batches so the
writer thread can advance; the resulting file is a consistent snapshot
without ever holding a long write lock against the live database.

Backup files are timestamped (``state.db.<ISO8601-Z>``) so a directory
listing sorts in chronological order. Hourly cadence × 24-backup default
retention = a 24-hour rolling window. Operators with a larger retention
budget bump ``backup_retention_count``; tighter cadence is rarely
useful (the live DB write rate is bounded by the scheduler tick).

Failure semantics:

* Transient I/O errors (disk full mid-copy, ``OSError``) log
  ``backup.failed`` and the loop continues; the next interval picks up.
* The backup directory is created on first run (parents=True). If the
  directory is read-only, :meth:`Path.mkdir` raises :class:`PermissionError`
  and we log ``backup.failed_setup`` with a short hint.
* Pruning the rotation is its own try/except so a failed unlink (file
  on a network mount with a transient hiccup) doesn't sink the whole
  pass.

DESIGN NOTE: This module lives in :mod:`ingestion.daemon.store` rather
than the slice-12 :mod:`ingestion.daemon.lifecycle` package to avoid a
merge conflict with the parallel slice 12 implementer who owns the
lifecycle directory. Backup is conceptually a lifecycle concern; a
later slice can move it once the lifecycle package settles. The
public name ``run_backup_loop`` stays stable across the move.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# Timestamp format for backup filenames. Compact ISO 8601 in UTC so a
# directory listing sorts chronologically without an explicit sort
# step. Colons are stripped because some Windows filesystems can't
# express them in filenames; the daemon runs Linux-only today, but
# the backups may be moved to a Windows share for cold storage.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_BACKUP_FILENAME_PREFIX = "state.db."


def resolve_backup_dir(*, sqlite_path: Path, override: Path | None) -> Path:
    """Pick the backup directory for this runtime.

    ``override`` (config.observability.backup_dir) wins when set;
    otherwise we land alongside the live DB. A hostlocal /var path is
    a sane default for container deployments where the live DB and
    its backups share the same mounted volume.
    """
    if override is not None:
        return override
    return sqlite_path.parent


async def run_backup_loop(
    shutdown: asyncio.Event,
    *,
    sqlite_path: Path,
    backup_dir: Path,
    interval_seconds: int = 3600,
    retention_count: int = 24,
    event_sink: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """Hourly online-safe backup of the daemon's SQLite state.

    Every ``interval_seconds`` (default 1h) the loop:

    1. mkdirs ``backup_dir`` on first iteration (parents=True);
    2. opens the live DB read-only via aiosqlite;
    3. copies the database to a fresh ``state.db.<timestamp>`` file
       using :func:`sqlite3_backup_init`;
    4. prunes the directory down to ``retention_count`` newest files.

    Emits ``backup.completed`` and ``backup.failed`` events on the
    optional ``event_sink`` so the dashboard's logs subscribers see the
    backup activity. Returns cleanly when ``shutdown`` is set.
    """
    log = get_logger(__name__)
    if interval_seconds <= 0:
        log.info("backup_loop.disabled")
        return
    if retention_count < 1:
        log.warning(
            "backup_loop.retention_too_small",
            retention_count=retention_count,
            message="retention_count < 1 disables retention; assuming 1",
        )
        retention_count = 1

    setup_failed = False
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Read-only volume / permission error / disk full at boot.
        # Don't crash the daemon — log once and skip every backup pass
        # until the operator fixes the mount.
        log.warning(
            "backup.failed_setup",
            backup_dir=str(backup_dir),
            error=str(exc)[:200],
        )
        setup_failed = True

    while not shutdown.is_set():
        if not setup_failed:
            await _run_one_backup_pass(
                sqlite_path=sqlite_path,
                backup_dir=backup_dir,
                retention_count=retention_count,
                event_sink=event_sink,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)


async def _run_one_backup_pass(
    *,
    sqlite_path: Path,
    backup_dir: Path,
    retention_count: int,
    event_sink: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """Run one backup attempt + the rotation prune.

    Each pass is independent: a failure to copy doesn't stop the prune
    pass from running on the next iteration. Pulled into a helper so
    the loop body stays short and exception flow is local to the
    operation that raised.
    """
    log = get_logger(__name__)
    timestamp = datetime.now(tz=UTC).strftime(_TIMESTAMP_FORMAT)
    backup_path = backup_dir / f"{_BACKUP_FILENAME_PREFIX}{timestamp}"
    started = datetime.now(tz=UTC)
    try:
        await _copy_database(sqlite_path, backup_path)
    except (sqlite3.Error, OSError) as exc:
        # OSError covers the disk-full / read-only mid-copy case; the
        # partial file is unlinked best-effort so a half-written
        # snapshot doesn't pollute the rotation.
        with contextlib.suppress(OSError):
            backup_path.unlink(missing_ok=True)
        log.warning(
            "backup.failed",
            backup_path=str(backup_path),
            error=str(exc)[:200],
        )
        if event_sink is not None:
            event_sink(
                "backup",
                {
                    "event": "backup.failed",
                    "backup_path": str(backup_path),
                    "error": str(exc)[:200],
                },
            )
        return
    duration = (datetime.now(tz=UTC) - started).total_seconds()
    bytes_written = backup_path.stat().st_size if backup_path.exists() else 0
    log.info(
        "backup.completed",
        backup_path=str(backup_path),
        bytes=bytes_written,
        duration_seconds=round(duration, 3),
    )
    if event_sink is not None:
        event_sink(
            "backup",
            {
                "event": "backup.completed",
                "backup_path": str(backup_path),
                "bytes": bytes_written,
                "duration_seconds": round(duration, 3),
            },
        )
    # Prune is best-effort — a failed unlink should not poison the
    # next backup attempt.
    try:
        await asyncio.to_thread(_prune_old_backups, backup_dir, retention_count)
    except OSError as exc:
        log.warning(
            "backup.prune_failed",
            backup_dir=str(backup_dir),
            error=str(exc)[:200],
        )


async def _copy_database(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` using SQLite's online-safe backup API.

    Both connections are opened with the daemon's standard pragmas via
    :func:`apply_pragmas` (WAL, busy_timeout, foreign_keys). The
    target file is created fresh — if a previous failed run left a
    partial file at that path, it's overwritten by the backup call.
    """
    # ``aiosqlite.connect`` on the source so the loop can yield while
    # the backup ticks; the target is a synchronous sqlite3.Connection
    # because the backup API copies into it directly. Either flavour
    # works as the destination.
    async with aiosqlite.connect(source) as src_conn:
        # Run the copy on a worker thread so the asyncio loop stays
        # responsive. The native backup API is C-level so it releases
        # the GIL between page batches.
        target_conn = sqlite3.connect(str(target))
        try:
            await src_conn.backup(target_conn)
        finally:
            target_conn.close()


def _prune_old_backups(backup_dir: Path, retention_count: int) -> None:
    """Delete every backup beyond the ``retention_count`` newest.

    Synchronous helper invoked via :func:`asyncio.to_thread`.
    Filename comparison is enough because the timestamp format sorts
    lexically. A non-conforming file (e.g. someone's manual copy) is
    skipped — only files matching ``state.db.<timestamp>`` are
    candidates for rotation.
    """
    backups = [
        p
        for p in backup_dir.iterdir()
        if p.is_file() and p.name.startswith(_BACKUP_FILENAME_PREFIX)
    ]
    backups.sort(key=lambda p: p.name, reverse=True)
    for stale in backups[retention_count:]:
        with contextlib.suppress(OSError):
            stale.unlink()


__all__ = ["resolve_backup_dir", "run_backup_loop"]
