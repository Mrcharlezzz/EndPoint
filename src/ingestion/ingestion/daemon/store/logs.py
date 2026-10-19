"""Persist every structlog record into the local SQLite ``logs`` table.

structlog's :class:`structlog.stdlib.LoggerFactory` routes records through
stdlib logging, so we just attach a logging.Handler that writes each record
to SQLite. Writes happen on a dedicated background thread fed by a bounded
queue: :meth:`emit` returns immediately so the asyncio event loop is never
blocked on disk I/O.

Retention is a separate background job (:func:`prune_logs_older_than`) that
deletes rows older than the configured retention window. Phase 7 will add
hot/cold tiering for audit logs; regular logs stay in this single table.

Structured-log fields (``user_email``, ``platform``, ``sync_id``) are
promoted into dedicated columns so the common UI queries can index them.
Everything else lands in ``fields_json``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from ingestion.daemon.store.async_conn import connect

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# Upper bound on a single event string before we truncate. 64KiB is already
# far more than any sane log line; past this we risk unbounded row growth.
_EVENT_MAX_BYTES = 64 * 1024

# Queue bound. Overflow drops the oldest record and increments a counter
# rather than blocking the publisher thread. 10_000 sustains a burst of
# 2-3s at the expected 10-50 syncs/minute peak.
_QUEUE_MAXSIZE = 10_000

# Graceful drain timeout on close(). If the writer can't keep up we would
# rather lose a tail of logs than hang SIGTERM.
_CLOSE_DRAIN_SECONDS = 2.0

# Sentinel pushed onto the queue to signal writer shutdown.
_SHUTDOWN_SENTINEL: tuple[logging.LogRecord | None, dict[str, Any] | None] = (None, None)


class SqliteLogHandler(logging.Handler):
    """stdlib logging handler that appends to the ``logs`` SQLite table.

    One connection is held open for the handler's lifetime and used only by
    the dedicated writer thread, so it sees no cross-thread contention. The
    public :meth:`emit` is non-blocking: it parses the record on the calling
    thread and then enqueues the row for the writer.

    Best-effort: if a write raises (disk full, schema missing), the handler
    logs to stderr via :meth:`handleError` and drops the record rather than
    blowing up the daemon. If the queue is full, :meth:`emit` drops silently
    and increments :attr:`dropped_records` so operators can track pressure.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        on_write: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._on_write = on_write
        self._queue: queue.Queue[tuple[logging.LogRecord | None, dict[str, Any] | None]] = (
            queue.Queue(maxsize=_QUEUE_MAXSIZE)
        )
        self.dropped_records = 0
        self._shutdown_signal = threading.Event()

        # Writer thread owns the connection exclusively to avoid the
        # sqlite3.Connection cross-thread caveats entirely.
        self._writer = threading.Thread(
            target=self._drain_queue,
            name="sqlite-log-writer",
            daemon=True,
        )
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        if self._shutdown_signal.is_set():
            return
        try:
            row = _record_to_row(record)
        except Exception:
            self.handleError(record)
            return
        try:
            self._queue.put_nowait((record, row))
        except queue.Full:
            self.dropped_records += 1

    def close(self) -> None:
        if self._shutdown_signal.is_set():
            super().close()
            return
        self._shutdown_signal.set()
        # Signal writer to drain then exit.
        try:
            self._queue.put_nowait(_SHUTDOWN_SENTINEL)
        except queue.Full:
            # Best effort: force-drain one slot to make room for the sentinel.
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_SHUTDOWN_SENTINEL)
        self._writer.join(timeout=_CLOSE_DRAIN_SECONDS)
        super().close()

    def _drain_queue(self) -> None:
        # Connection lives on the writer thread; no cross-thread access.
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # synchronous=NORMAL trades fsync-per-write for fsync-per-
            # checkpoint; logs are best-effort observability so the
            # tradeoff is favourable. temp_store=MEMORY helps if the
            # retention pruner ever joins this connection's plan.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            while True:
                record, row = self._queue.get()
                if record is None or row is None:
                    return
                try:
                    conn.execute(
                        """
                        INSERT INTO logs (
                            timestamp, level, event, user_email, platform, sync_id,
                            fields_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["timestamp"],
                            row["level"],
                            row["event"],
                            row["user_email"],
                            row["platform"],
                            row["sync_id"],
                            row["fields_json"],
                            row["created_at"],
                        ),
                    )
                except Exception:
                    # Can't use ``handleError`` reliably from a background
                    # thread; fall back to stderr via the stdlib logger.
                    self.handleError(record)
                    continue
                if self._on_write is not None:
                    try:
                        self._on_write(row)
                    except Exception:
                        self.handleError(record)
        finally:
            conn.close()


def _record_to_row(record: logging.LogRecord) -> dict[str, Any]:
    """Extract the fields we care about from a stdlib record.

    structlog's JSONRenderer stringifies the record into ``record.msg``. We
    parse it back to recover the structured fields. If parsing fails we
    store the raw message as ``event`` and leave the promoted columns NULL.

    Timestamps and levels are canonicalised before storage:
    - timestamps with the trailing ``Z`` marker are rewritten to
      ``+00:00`` so retention cutoffs and range queries compare cleanly.
    - level is upper-cased; structlog emits lowercase and the stdlib path
      emits uppercase, so the column would otherwise mix cases and break
      exact-match filtering via ``/api/logs?level=ERROR``.
    """
    raw = record.getMessage()
    now = datetime.now(tz=UTC).isoformat()
    timestamp = now
    level = record.levelname
    event: str
    user_email: str | None = None
    platform: str | None = None
    sync_id: str | None = None
    fields_json: str | None = None

    parsed_any: Any = _try_json(raw)
    if isinstance(parsed_any, dict):
        parsed = cast("dict[str, Any]", parsed_any)
        event = str(parsed.pop("event", raw))
        if "timestamp" in parsed:
            ts = parsed.pop("timestamp")
            if isinstance(ts, str):
                timestamp = ts
        level = str(parsed.pop("level", level))
        user_email = _pop_str(parsed, "user_email")
        platform = _pop_str(parsed, "platform")
        sync_id = _pop_str(parsed, "sync_id")
        if parsed:
            # ``default=repr`` so a non-serialisable value is captured as its
            # repr (still readable) rather than silently coerced to str which
            # loses type info for nested objects.
            fields_json = json.dumps(parsed, separators=(",", ":"), default=repr)
    else:
        event = raw

    event = _truncate(event)

    return {
        "timestamp": _canonical_ts(timestamp),
        "level": level.upper(),
        "event": event,
        "user_email": user_email,
        "platform": platform,
        "sync_id": sync_id,
        "fields_json": fields_json,
        "created_at": now,
    }


def _canonical_ts(ts: str) -> str:
    """Normalise an ISO-8601 UTC timestamp to the ``+00:00`` form.

    Python's ``datetime.now(tz=UTC).isoformat()`` emits ``...+00:00``;
    structlog's ``TimeStamper(fmt='iso', utc=True)`` emits ``...Z``. The
    column ends up mixing both forms, which is a correctness hazard for
    cutoff comparisons — ``"Z" > "+"`` lexically, so a row stored with
    ``Z`` at the same moment as a cutoff stored with ``+00:00`` does not
    register as less-than.
    """
    if ts.endswith("Z"):
        return ts[:-1] + "+00:00"
    return ts


def _truncate(s: str) -> str:
    """Cap ``s`` at ``_EVENT_MAX_BYTES`` bytes, preserving valid UTF-8."""
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= _EVENT_MAX_BYTES:
        return s
    trimmed = encoded[:_EVENT_MAX_BYTES].decode("utf-8", errors="ignore")
    return trimmed + "...[truncated]"


def _try_json(value: str) -> Any:
    # Accept leading whitespace so a log pipeline with a prefix processor
    # doesn't break structured parsing. Catch every exception: a malformed
    # log line must not crash the daemon via the root logger.
    if not value:
        return None
    stripped = value.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _pop_str(fields: dict[str, Any], key: str) -> str | None:
    value = fields.pop(key, None)
    return str(value) if value is not None else None


async def prune_logs_older_than(
    db_path: Path,
    *,
    retention_days: int,
    batch_size: int = 10_000,
) -> int:
    """Delete rows older than ``retention_days``. Returns the count deleted.

    Deletes in ``batch_size`` chunks with a commit between each so the
    write lock doesn't stall concurrent log handler writes on a mature
    logs table. Yields to the event loop between batches so other
    coroutines can progress.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    total = 0
    async with connect(db_path) as conn:
        while True:
            cursor = await conn.execute(
                "DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE timestamp < ? LIMIT ?)",
                (cutoff, batch_size),
            )
            await conn.commit()
            rowcount = max(0, cursor.rowcount)
            if rowcount <= 0:
                break
            total += rowcount
            if rowcount < batch_size:
                break
    return total


async def prune_alert_firings_older_than(
    db_path: Path,
    *,
    retention_days: int,
    batch_size: int = 10_000,
) -> int:
    """Delete alert firings older than ``retention_days``. Returns the count.

    Without this the table grows monotonically because cooldown prevents
    re-firing but doesn't clean up. Resolved firings are also dropped once
    they age out.

    Deletes in ``batch_size`` chunks with a commit between each so the
    write lock doesn't stall the engine's firing inserts on a mature
    alert_firings table. Yields to the event loop between batches so other
    coroutines can progress.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    total = 0
    async with connect(db_path) as conn:
        while True:
            cursor = await conn.execute(
                """
                DELETE FROM alert_firings
                WHERE id IN (SELECT id FROM alert_firings WHERE fired_at < ? LIMIT ?)
                """,
                (cutoff, batch_size),
            )
            await conn.commit()
            rowcount = max(0, cursor.rowcount)
            if rowcount <= 0:
                break
            total += rowcount
            if rowcount < batch_size:
                break
    return total


def install_sqlite_log_handler(
    db_path: Path,
    *,
    on_write: Callable[[dict[str, Any]], None] | None = None,
) -> SqliteLogHandler:
    """Attach a :class:`SqliteLogHandler` to the root logger.

    Pass ``on_write`` to fan each persisted log row out to an event bus for
    WebSocket subscribers. Not idempotent: calling twice installs two
    handlers, so callers manage lifecycle (create once at daemon startup,
    close on shutdown).
    """
    handler = SqliteLogHandler(db_path, on_write=on_write)
    logging.getLogger().addHandler(handler)
    return handler
