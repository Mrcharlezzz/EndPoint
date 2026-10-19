"""Synchronous SQLite helpers used at startup (migration runner, config overlay).

Runtime code inside the daemon uses ``aiosqlite`` for non-blocking I/O. But
startup is single-shot and cheap: we apply migrations and read the overlay
once, with the stdlib :mod:`sqlite3` module. Keeping startup sync avoids an
asyncio loop during container boot.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_SCHEMA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# Migration filenames must match NNNN_snake_name.sql so lexical sort lines up
# with numeric order. A stray "fix.sql" would sort after "0004_*.sql" and
# silently run out of order.
_MIGRATION_NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # WAL set FIRST so the journal mode persists for every subsequent
    # connection (including the aiosqlite ones opened later). An existing
    # DELETE-mode database upgrades transparently here on first boot.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # NORMAL fsync trades a tail of overlay writes for a major write
    # speedup. Cursor data is recoverable from the Postgres source-of-truth,
    # so the durability trade is acceptable for daemon use.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def apply_migrations(db_path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply any un-applied ``.sql`` migrations in lexical order.

    Returns the list of migration names that were applied on this call (empty
    if the database was already up to date). Idempotent: safe to call on every
    startup.

    Each migration runs inside a transaction alongside its bookkeeping INSERT
    so a crash between the DDL and the insert can't leave a migration half-
    recorded. ``executescript`` commits implicitly, so we rebuild the
    transaction with explicit BEGIN / COMMIT around the whole step.

    Filenames must match ``NNNN_snake_name.sql``. Non-conforming files are
    skipped with a warning so a stray file doesn't reorder the sequence.
    Files are decoded as utf-8-sig so a BOM-prefixed export from Windows
    editors doesn't choke ``executescript``.
    """
    applied: list[str] = []
    conn = _connect(db_path)
    try:
        conn.execute(_SCHEMA_TABLE_SQL)
        conn.commit()
        existing = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        all_files = sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())
        files: list[Path] = []
        for path in all_files:
            if _MIGRATION_NAME_RE.match(path.name) is None:
                _log.warning("migration.filename_rejected", extra={"migration_file": path.name})
                continue
            files.append(path)
        for path in files:
            if path.name in existing:
                continue
            script = path.read_text(encoding="utf-8-sig")
            conn.execute("BEGIN")
            try:
                conn.executescript(script)
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (path.name, _now_iso()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied.append(path.name)
    finally:
        conn.close()
    return applied


class LocalStore:
    """Light wrapper around a SQLite connection for overlay access.

    Background loops use ``aiosqlite`` against the same file. FastAPI serves
    requests on a threadpool, so the overlay connection must tolerate
    cross-thread access — we open with ``check_same_thread=False``.

    Two connections, two locks:

    * ``_write_conn`` under ``_write_lock`` handles every mutating call.
      The write lock keeps Python-side interleaving deterministic and
      avoids relying on SQLite's C-level serialization for torn rows in
      the read-modify-write ON CONFLICT path.
    * ``_read_conn`` under ``_read_lock`` handles every read. It shares
      the file in WAL mode, so readers never block writers and the
      scheduler's per-tick ``all_overlay`` doesn't queue behind whatever
      the FastAPI thread pool is writing right now.

    Tradeoff: a reader that fires between ``set_overlay`` returning on
    the writer thread and the scheduler's next tick will observe the
    NEW value (WAL publishes on commit). But a reader that races with
    an in-flight write observes the PRE-write snapshot — one tick
    behind at worst. The scheduler re-reads config on every tick, so
    the staleness window is bounded.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._write_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._write_conn.execute("PRAGMA journal_mode=WAL")
        self._write_conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL fsync + 5s busy timeout match the daemon's aiosqlite path
        # (see store/async_conn.py); cross-thread writers behave identically.
        self._write_conn.execute("PRAGMA synchronous=NORMAL")
        self._write_conn.execute("PRAGMA busy_timeout=5000")
        self._write_conn.execute("PRAGMA temp_store=MEMORY")
        self._read_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # ``query_only`` hardens the reader against accidental writes; the
        # WAL journal was set on the writer, sharing the file is enough.
        self._read_conn.execute("PRAGMA query_only=ON")
        # Reader still needs busy_timeout so a checkpoint racing the read
        # doesn't fail immediately, and temp_store helps aggregate queries.
        self._read_conn.execute("PRAGMA busy_timeout=5000")
        self._read_conn.execute("PRAGMA temp_store=MEMORY")

    def close(self) -> None:
        with self._write_lock, self._read_lock:
            self._write_conn.close()
            self._read_conn.close()

    def __enter__(self) -> LocalStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._write_lock:
            cur = self._write_conn.cursor()
            try:
                yield cur
                self._write_conn.commit()
            finally:
                cur.close()

    def get_overlay(self, key: str) -> Any | None:
        # Reads go through the read-only connection, so a long-running
        # write on the other thread doesn't queue the scheduler behind it.
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT value_json FROM config_overlay WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def set_overlay(self, key: str, value: Any) -> None:
        payload = json.dumps(value, separators=(",", ":"))
        now = _now_iso()
        with self._write_lock:
            self._write_conn.execute(
                """
                INSERT INTO config_overlay (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, now),
            )
            self._write_conn.commit()

    def delete_overlay(self, key: str) -> None:
        with self._write_lock:
            self._write_conn.execute("DELETE FROM config_overlay WHERE key = ?", (key,))
            self._write_conn.commit()

    def all_overlay(self) -> dict[str, Any]:
        with self._read_lock:
            rows = self._read_conn.execute("SELECT key, value_json FROM config_overlay").fetchall()
        return {key: json.loads(value) for key, value in rows}


def open_store(db_path: Path) -> LocalStore:
    """Apply migrations, then return an opened :class:`LocalStore`."""
    apply_migrations(db_path)
    return LocalStore(db_path)
