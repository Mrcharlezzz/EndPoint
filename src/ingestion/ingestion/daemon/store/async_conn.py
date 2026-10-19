"""Shared ``aiosqlite`` connection factory.

Every async caller that opens a connection against the daemon's local SQLite
must go through :func:`connect` so the per-connection pragmas
(``foreign_keys``, ``busy_timeout``, ``synchronous``, ``journal_mode``,
``temp_store``) land consistently.

Opening aiosqlite without these leaves ``foreign_keys=OFF`` (the SQLite
default) and no busy timeout, so ``ON DELETE CASCADE`` never fires and
``SQLITE_BUSY`` is returned immediately under contention.

Pragma rationale:

* ``journal_mode=WAL`` — readers don't block writers; daemon-suitable.
  The synchronous startup path in ``store.local._connect`` already sets
  WAL on the same file, so this is a redundant safety net for the rare
  case an aiosqlite caller opens a file the synchronous path hasn't
  touched yet (e.g. fresh tests pointing at a tmp DB).
* ``synchronous=NORMAL`` — fsync on commit becomes a checkpoint instead
  of every write. Trades a tail of cursor writes (recoverable from the
  Postgres source-of-truth on the content side) for a major write speed
  improvement on bursts. Power-cut is the only loss vector and the
  daemon is designed to redrive cursors from the last successful sync.
* ``busy_timeout=5000`` — readers in WAL never block readers; writers
  block other writers, but five seconds covers any realistic burst.
* ``temp_store=MEMORY`` — sort/temp tables in RAM rather than on disk.
  Small but non-zero win on aggregate queries the metrics pane runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

BUSY_TIMEOUT_MS = 5000


async def apply_pragmas(conn: aiosqlite.Connection) -> None:
    """Set the daemon's standard pragmas on ``conn``.

    Extracted so the connection-pool path applies the same settings as the
    one-shot ``connect`` context manager. Pragmas that can fail benignly on
    a freshly-opened connection (a long-held WAL switch on the same file)
    are tolerated — the synchronous startup path is the authoritative WAL
    setter; this is belt-and-braces.
    """
    # Order matters: WAL first so the rest of the pragmas apply against a
    # WAL-mode database. journal_mode is a no-op when already WAL.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA temp_store=MEMORY")


@asynccontextmanager
async def connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open an ``aiosqlite`` connection with the daemon's standard pragmas.

    Use as an async context manager: ``async with connect(path) as conn: ...``.
    The connection is closed automatically on exit.
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        await apply_pragmas(conn)
        yield conn
