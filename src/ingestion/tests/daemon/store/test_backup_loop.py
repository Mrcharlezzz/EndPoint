"""Slice 13 part C: hourly online-safe SQLite backup loop.

Covers :mod:`ingestion.daemon.store.backup`:

* Single backup pass writes a file with an ISO 8601 timestamp filename
  that sorts lexically so directory listing is chronological.
* Online-safe: a write transaction in flight on the source DB does NOT
  corrupt the snapshot — we use :meth:`aiosqlite.Connection.backup` which
  is the C-level :c:func:`sqlite3_backup_init` shim.
* Retention: with ``retention_count=24``, the 25th backup prunes the
  oldest one to keep the rolling window at 24.
* The backup directory is created on first run (parents=True).
* A read-only backup directory logs ``backup.failed_setup`` and the loop
  continues without crashing — operator fixes the volume next pass.
* ``retention_count<1`` is clamped to 1 (the loop logs a warning).
* ``interval_seconds<=0`` short-circuits with ``backup_loop.disabled``.
* Partial backup file is unlinked on copy failure so a half-written
  snapshot never enters the rotation.
* Non-conforming files in ``backup_dir`` are skipped during prune.

The tests call :func:`_run_one_backup_pass` and :func:`_prune_old_backups`
directly where possible to avoid driving the full async loop. The
loop-driver test uses a short interval + immediate shutdown event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest
import structlog
from ingestion.daemon.store.backup import (
    _BACKUP_FILENAME_PREFIX,
    _prune_old_backups,
    _run_one_backup_pass,
    resolve_backup_dir,
    run_backup_loop,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    """Create a real (non-empty) sqlite database on disk so the backup
    has something to copy. WAL mode mirrors the live daemon."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


# ---------------------------------------------------------------------------
# single-pass backup: ISO 8601 filename + reusable snapshot
# ---------------------------------------------------------------------------


async def test_single_backup_pass_creates_iso8601_named_file(
    source_db: Path, backup_dir: Path
) -> None:
    """A successful pass writes ``state.db.<ISO8601-Z>`` under
    ``backup_dir``. Filename matches the documented timestamp format
    (``YYYYMMDDTHHMMSSZ``) so directory listing is chronological."""
    backup_dir.mkdir()
    await _run_one_backup_pass(
        sqlite_path=source_db,
        backup_dir=backup_dir,
        retention_count=24,
        event_sink=None,
    )
    files = sorted(backup_dir.iterdir())
    assert len(files) == 1
    name = files[0].name
    assert name.startswith(_BACKUP_FILENAME_PREFIX)
    timestamp = name[len(_BACKUP_FILENAME_PREFIX) :]
    # ISO 8601 compact UTC: YYYYMMDDTHHMMSSZ
    parsed = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    assert parsed.tzinfo is None  # the format itself has no tz token
    # File contains valid SQLite data — open it back.
    conn = sqlite3.connect(files[0])
    try:
        rows = conn.execute("SELECT v FROM t").fetchall()
    finally:
        conn.close()
    assert rows == [("hello",)]


async def test_event_sink_receives_backup_completed(source_db: Path, backup_dir: Path) -> None:
    """An event sink is invoked with ``backup.completed`` carrying the
    backup_path / bytes / duration_seconds payload — dashboard
    subscribers see live backup activity."""
    backup_dir.mkdir()
    events: list[tuple[str, dict[str, object]]] = []

    def _sink(channel: str, payload: dict[str, object]) -> None:
        events.append((channel, payload))

    await _run_one_backup_pass(
        sqlite_path=source_db,
        backup_dir=backup_dir,
        retention_count=24,
        event_sink=_sink,
    )
    completed = [p for c, p in events if c == "backup" and p.get("event") == "backup.completed"]
    assert len(completed) == 1
    assert "backup_path" in completed[0]
    assert "bytes" in completed[0]


# ---------------------------------------------------------------------------
# online-safe: a writer thread does not corrupt the snapshot
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_backup_during_active_writes_produces_valid_snapshot(
    source_db: Path, backup_dir: Path
) -> None:
    """Online-safe: a burst of writes against the source DB while the
    backup runs must NOT corrupt the snapshot. Drive 100 inserts via
    aiosqlite in parallel with the backup pass and assert the resulting
    file opens without ``database disk image is malformed``."""
    backup_dir.mkdir()

    async def _write_burst() -> None:
        async with aiosqlite.connect(source_db) as conn:
            for i in range(100):
                await conn.execute("INSERT INTO t VALUES (?, ?)", (i + 100, f"v{i}"))
                # Yield so the backup gets a chance to interleave.
                await asyncio.sleep(0)
            await conn.commit()

    await asyncio.gather(
        _write_burst(),
        _run_one_backup_pass(
            sqlite_path=source_db,
            backup_dir=backup_dir,
            retention_count=24,
            event_sink=None,
        ),
    )
    files = list(backup_dir.iterdir())
    assert len(files) == 1
    # Open the snapshot — if it were torn, this would fail.
    conn = sqlite3.connect(files[0])
    try:
        # Integrity check is the canonical way to spot a corrupt sqlite file.
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    assert result == ("ok",)


# ---------------------------------------------------------------------------
# retention: 24 hourly snapshots = 24h rolling window
# ---------------------------------------------------------------------------


def test_prune_keeps_newest_n_files(tmp_path: Path) -> None:
    """Seed 25 backup-named files with monotonic timestamps and prune
    with ``retention_count=24``. The oldest (smallest timestamp) is
    removed; the 24 newest stay."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    timestamps = [f"2026010{i:02d}T000000Z" for i in range(1, 26)]
    for ts in timestamps:
        (backup_dir / f"{_BACKUP_FILENAME_PREFIX}{ts}").write_text("data")
    _prune_old_backups(backup_dir, 24)
    remaining = sorted(p.name for p in backup_dir.iterdir())
    # 25 - 1 = 24, the smallest (oldest) timestamp goes.
    assert len(remaining) == 24
    assert all(_BACKUP_FILENAME_PREFIX + "20260101T000000Z" not in p for p in remaining)


def test_prune_skips_non_conforming_files(tmp_path: Path) -> None:
    """Files that don't match ``state.db.<timestamp>`` are skipped during
    prune. An operator's manual ``state.db.OLD-MANUAL`` rescue copy is
    not deleted by the rotation."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "manual_backup.db").write_text("manual")
    (backup_dir / "README.txt").write_text("notes")
    for ts in ["20260101T000000Z", "20260102T000000Z"]:
        (backup_dir / f"{_BACKUP_FILENAME_PREFIX}{ts}").write_text("data")
    _prune_old_backups(backup_dir, 1)
    names = sorted(p.name for p in backup_dir.iterdir())
    assert "manual_backup.db" in names
    assert "README.txt" in names
    # 2 → 1 conforming file kept.
    conforming = [n for n in names if n.startswith(_BACKUP_FILENAME_PREFIX)]
    assert len(conforming) == 1


def test_prune_does_nothing_when_under_retention_count(tmp_path: Path) -> None:
    """Five files + retention=10 = nothing pruned (the directory is
    smaller than the rolling window)."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(5):
        (backup_dir / f"{_BACKUP_FILENAME_PREFIX}2026010{i + 1:02d}T000000Z").write_text("d")
    _prune_old_backups(backup_dir, 10)
    assert len(list(backup_dir.iterdir())) == 5


# ---------------------------------------------------------------------------
# directory creation + read-only path (graceful degradation)
# ---------------------------------------------------------------------------


async def test_loop_creates_backup_dir_on_first_run(source_db: Path, tmp_path: Path) -> None:
    """``backup_dir`` doesn't exist at start → mkdir(parents=True) inside
    the loop creates it on the first iteration."""
    nested = tmp_path / "deep" / "nested" / "path"
    assert not nested.exists()
    shutdown = asyncio.Event()
    shutdown.set()  # exit immediately after one pass
    # Use a short interval so the loop wakes up quickly even under timing
    # variance; shutdown is already set so wait_for returns at once.
    await run_backup_loop(
        shutdown,
        sqlite_path=source_db,
        backup_dir=nested,
        interval_seconds=1,
        retention_count=2,
    )
    assert nested.exists()


async def test_readonly_backup_dir_logs_setup_failure_and_continues(
    source_db: Path, tmp_path: Path
) -> None:
    """A read-only backup directory makes ``mkdir`` raise
    :class:`PermissionError`. The impl logs ``backup.failed_setup`` and
    skips every backup pass without crashing the loop."""
    parent = tmp_path / "ro"
    parent.mkdir()
    # Make parent read-only so mkdir on the child fails. Skip on environments
    # where chmod isn't honoured (root user, exotic filesystems).
    parent.chmod(0o500)
    if os.access(parent, os.W_OK):
        pytest.skip("filesystem does not honour chmod for permission test")
    target = parent / "child"
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    try:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_backup_loop(
            shutdown,
            sqlite_path=source_db,
            backup_dir=target,
            interval_seconds=1,
            retention_count=2,
        )
    finally:
        structlog.reset_defaults()
        # Restore so cleanup can run.
        parent.chmod(0o700)
    events = [e.get("event") for e in cap.entries]
    assert "backup.failed_setup" in events


# ---------------------------------------------------------------------------
# clamps + short-circuits
# ---------------------------------------------------------------------------


async def test_interval_zero_short_circuits_with_disabled_log(
    source_db: Path, backup_dir: Path
) -> None:
    """``interval_seconds<=0`` is the operator-opt-out. Loop logs
    ``backup_loop.disabled`` and returns immediately — no directory
    is created, no backup pass runs."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    try:
        shutdown = asyncio.Event()
        await run_backup_loop(
            shutdown,
            sqlite_path=source_db,
            backup_dir=backup_dir,
            interval_seconds=0,
        )
    finally:
        structlog.reset_defaults()
    events = [e.get("event") for e in cap.entries]
    assert "backup_loop.disabled" in events
    # No directory was created because the loop short-circuited.
    assert not backup_dir.exists()


async def test_retention_count_below_one_is_clamped_to_one(
    source_db: Path, backup_dir: Path
) -> None:
    """``retention_count<1`` would disable retention entirely (every
    file is older than the window). The impl clamps to 1 and logs
    ``backup_loop.retention_too_small`` — at least one snapshot is
    always preserved.

    Drive one pass via a started loop + delayed shutdown — the loop
    body runs once before shutdown.is_set() is observed."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    try:
        shutdown = asyncio.Event()

        async def _shutdown_after_one_pass() -> None:
            # Wait for the first backup file to land, then trigger shutdown.
            for _ in range(50):
                if backup_dir.exists() and any(backup_dir.iterdir()):
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            run_backup_loop(
                shutdown,
                sqlite_path=source_db,
                backup_dir=backup_dir,
                interval_seconds=1,
                retention_count=0,
            ),
            _shutdown_after_one_pass(),
        )
    finally:
        structlog.reset_defaults()
    events = [e.get("event") for e in cap.entries]
    assert "backup_loop.retention_too_small" in events
    # One backup was still produced.
    assert len(list(backup_dir.iterdir())) >= 1


# ---------------------------------------------------------------------------
# partial backup file unlinked on copy failure
# ---------------------------------------------------------------------------


async def test_partial_file_unlinked_on_copy_failure(
    source_db: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If :meth:`aiosqlite.Connection.backup` raises mid-copy (disk full,
    transient I/O failure), the half-written destination file is
    unlinked best-effort. Pin via a monkeypatched ``backup`` that
    creates the dest file then raises — the dest must NOT be on disk
    after the pass returns."""
    backup_dir.mkdir()

    real_aiosqlite_connect = aiosqlite.connect

    @contextlib.asynccontextmanager
    async def _bad_connect(path: object) -> object:
        async with real_aiosqlite_connect(path) as conn:

            async def _raising_backup(_target: object) -> None:
                # Touch the destination so a "partial file" exists.
                # The runtime impl creates the target via
                # ``sqlite3.connect(str(target))`` BEFORE this is called,
                # so the file is already on disk. We just need to raise.
                raise sqlite3.OperationalError("disk full")

            # ``backup`` is a method on the wrapped connection — replace it.
            monkeypatch.setattr(conn, "backup", _raising_backup)
            yield conn

    monkeypatch.setattr("ingestion.daemon.store.backup.aiosqlite.connect", _bad_connect)

    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    try:
        await _run_one_backup_pass(
            sqlite_path=source_db,
            backup_dir=backup_dir,
            retention_count=24,
            event_sink=None,
        )
    finally:
        structlog.reset_defaults()
    # No backup file should remain.
    files = list(backup_dir.iterdir())
    backup_files = [p for p in files if p.name.startswith(_BACKUP_FILENAME_PREFIX)]
    assert backup_files == []
    events = [e.get("event") for e in cap.entries]
    assert "backup.failed" in events


# ---------------------------------------------------------------------------
# resolve_backup_dir: override wins over default-alongside
# ---------------------------------------------------------------------------


def test_resolve_backup_dir_prefers_override(tmp_path: Path) -> None:
    """Operator-supplied ``backup_dir`` overrides the
    alongside-the-DB default."""
    sqlite_path = tmp_path / "live" / "state.db"
    sqlite_path.parent.mkdir()
    override = tmp_path / "elsewhere"
    assert resolve_backup_dir(sqlite_path=sqlite_path, override=override) == override


def test_resolve_backup_dir_defaults_alongside(tmp_path: Path) -> None:
    """No override → backups land in ``sqlite_path.parent``."""
    sqlite_path = tmp_path / "live" / "state.db"
    sqlite_path.parent.mkdir()
    assert resolve_backup_dir(sqlite_path=sqlite_path, override=None) == sqlite_path.parent


# ---------------------------------------------------------------------------
# event_sink receives backup.failed on copy error
# ---------------------------------------------------------------------------


async def test_prune_failure_does_not_poison_next_backup(
    source_db: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed prune (e.g. file on a network mount with a transient
    hiccup) is logged with ``backup.prune_failed`` and the loop
    continues. The current backup is still on disk."""
    backup_dir.mkdir()

    def _raising_prune(_backup_dir: object, _retention: int) -> None:
        raise OSError("network mount blip")

    monkeypatch.setattr(
        "ingestion.daemon.store.backup._prune_old_backups",
        _raising_prune,
    )
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
    try:
        await _run_one_backup_pass(
            sqlite_path=source_db,
            backup_dir=backup_dir,
            retention_count=24,
            event_sink=None,
        )
    finally:
        structlog.reset_defaults()
    events = [e.get("event") for e in cap.entries]
    assert "backup.prune_failed" in events
    # The new backup file is still there even though prune failed.
    backups = [p for p in backup_dir.iterdir() if p.name.startswith(_BACKUP_FILENAME_PREFIX)]
    assert len(backups) == 1


async def test_event_sink_receives_backup_failed_on_copy_error(
    source_db: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed copy emits ``backup.failed`` to the event sink so the
    dashboard's logs subscribers see the failure live, not just in
    structlog output."""
    backup_dir.mkdir()
    real_connect = aiosqlite.connect

    @contextlib.asynccontextmanager
    async def _bad_connect(path: object) -> object:
        async with real_connect(path) as conn:

            async def _raise(_target: object) -> None:
                raise sqlite3.OperationalError("nope")

            monkeypatch.setattr(conn, "backup", _raise)
            yield conn

    monkeypatch.setattr("ingestion.daemon.store.backup.aiosqlite.connect", _bad_connect)

    events: list[dict[str, object]] = []

    def _sink(_channel: str, payload: dict[str, object]) -> None:
        events.append(payload)

    await _run_one_backup_pass(
        sqlite_path=source_db,
        backup_dir=backup_dir,
        retention_count=24,
        event_sink=_sink,
    )
    failed = [p for p in events if p.get("event") == "backup.failed"]
    assert len(failed) == 1
