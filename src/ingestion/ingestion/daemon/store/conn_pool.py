"""Bounded async pool of ``aiosqlite`` connections to the daemon SQLite file.

Every async caller in the daemon currently does
``async with aiosqlite.connect(path) as conn:`` per-call. Each open does the
SQLite file open dance plus the per-connection pragma round-trip — modest
cost per call, but under burst (a tick that fans into N writers) the open
calls serialise on the OS file lock and the per-call pragmas multiply.

This pool checks out a long-lived ``aiosqlite.Connection`` per acquire and
returns it on release, applying the daemon's pragmas exactly once when the
connection is first created. Callers use it with::

    async with pool.acquire() as conn:
        await conn.execute(...)
        await conn.commit()

Bounding:

* ``max_connections`` caps the total number of concurrent connections the
  pool will hand out. Acquires past the ceiling block on an
  ``asyncio.Queue`` until a release frees one — this is intentional
  back-pressure, far better than letting unbounded opens swamp SQLite.
* The pool warms eagerly with two connections so a cold tick is not
  paying the open cost on every checkout. Lazy growth up to the cap
  handles bursts.

Cancellation safety:

* ``acquire`` uses a try/finally pattern so a task cancelled while
  waiting on the queue does not leak its slot count. A task cancelled
  *after* it has the connection still releases it via the context
  manager's ``__aexit__``.

Shutdown:

* ``close_all`` drains every connection. Pending acquires raise
  ``RuntimeError`` so callers can fail fast instead of hanging.
* Calling ``close_all`` twice is a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import aiosqlite

from ingestion.daemon.store.async_conn import apply_pragmas

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

DEFAULT_MAX_CONNECTIONS = 4
DEFAULT_WARM_CONNECTIONS = 2


class AsyncConnectionPool:
    """Bounded pool of ``aiosqlite.Connection`` instances.

    Pool is lazy: the first ``DEFAULT_WARM_CONNECTIONS`` connections are
    created on :meth:`warm` (typically called once at startup). Subsequent
    acquires either grab an idle connection or create a new one up to
    ``max_connections``. Past the cap, acquires block until a release
    returns a connection.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        warm_connections: int = DEFAULT_WARM_CONNECTIONS,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be >= 1")
        if warm_connections < 0 or warm_connections > max_connections:
            raise ValueError("warm_connections must be between 0 and max_connections")
        self._db_path = db_path
        self._max = max_connections
        self._warm_target = warm_connections
        # Idle queue — released connections land here. Bounded at max so a
        # mis-paired release can't grow the pool beyond the cap.
        self._idle: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=max_connections)
        # Lazy creation lock so two parallel acquires racing past the
        # idle-empty check don't both spin up new connections beyond the
        # cap. The counter tracks how many have been minted in total.
        self._size_lock = asyncio.Lock()
        self._size = 0
        self._closed = False

    async def warm(self) -> None:
        """Create the initial pool of connections.

        Idempotent: subsequent calls past the warm target are no-ops. Safe
        to call from startup before any acquire.
        """
        if self._closed:
            raise RuntimeError("AsyncConnectionPool is closed")
        async with self._size_lock:
            while self._size < self._warm_target:
                conn = await self._open_connection()
                self._size += 1
                await self._idle.put(conn)

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Check out a connection for the duration of the ``async with`` block.

        Releases automatically on exit, even if the body raises or the task
        is cancelled. The connection is returned to the idle queue unless
        the pool is closing, in which case it is closed in-place.
        """
        if self._closed:
            raise RuntimeError("AsyncConnectionPool is closed")
        conn = await self._checkout()
        try:
            yield conn
        finally:
            # Release path runs even on cancellation. ``_release`` swallows
            # close errors so a broken connection cannot block the pool
            # from accepting new acquires.
            await self._release(conn)

    async def _checkout(self) -> aiosqlite.Connection:
        # Fast path: pull an idle connection without blocking.
        try:
            return self._idle.get_nowait()
        except asyncio.QueueEmpty:
            pass
        # Slow path under contention: try to mint a new connection if the
        # cap allows; otherwise wait on the idle queue.
        async with self._size_lock:
            if self._closed:
                raise RuntimeError("AsyncConnectionPool is closed")
            if self._size < self._max:
                conn = await self._open_connection()
                self._size += 1
                return conn
        # Pool is at capacity — block until a release lands a connection.
        return await self._idle.get()

    async def _release(self, conn: aiosqlite.Connection) -> None:
        if self._closed:
            await self._close_quietly(conn)
            return
        try:
            self._idle.put_nowait(conn)
        except asyncio.QueueFull:
            # Pool already saturated — bound the connection count by closing.
            # In practice the idle queue cap equals _max, so the only way to
            # hit this is if the pool was reset between acquire and release.
            await self._close_quietly(conn)
            async with self._size_lock:
                self._size = max(0, self._size - 1)

    async def close_all(self) -> None:
        """Close every connection. Idempotent.

        Pending acquires that come in after this call will raise. In-flight
        acquires (already past the queue) finish their work, then their
        release path closes the connection rather than re-pooling it.
        """
        async with self._size_lock:
            if self._closed:
                return
            self._closed = True
        # Drain whatever is currently idle. In-flight checkouts will land
        # on the closed branch of ``_release`` and close their conn there.
        while True:
            try:
                conn = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._close_quietly(conn)

    async def _open_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(self._db_path))
        await apply_pragmas(conn)
        return conn

    async def _close_quietly(self, conn: aiosqlite.Connection) -> None:
        try:
            await conn.close()
        except Exception:
            # Pool teardown is best-effort; a busted connection cannot hold
            # the rest of the pool hostage. The size counter has already
            # accounted for the slot.
            return

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def size(self) -> int:
        """Total connections currently minted (idle + checked-out)."""
        return self._size

    def idle_count(self) -> int:
        return self._idle.qsize()
