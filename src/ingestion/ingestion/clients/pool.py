"""Shared async HTTP client pool keyed on ``(host, base_url)``.

Each adapter call today instantiates a fresh :class:`AsyncHTTPClient`, which
in turn opens a fresh :class:`httpx.AsyncClient` connection pool. Under bursts
the TCP/TLS handshakes pile up and dominate the wire time. A long-lived
client per remote host reuses keep-alive sockets and the TLS session cache,
trimming hundreds of milliseconds off every call after the first.

This module is the foundation: a thin keyed pool with explicit acquire /
release semantics. Adapters keep their existing per-call construction in
this slice — the pool is a future opt-in, registered in the runtime so
callers that have a handle to ``Runtime`` can swap to ``acquire`` at a
later carve-out without touching the adapter wiring path.

The pool maintains an LRU per host so a long-tail of one-off bases (think:
webhook URLs) cannot grow the cache without bound. ``close_all`` drains
every pooled client on shutdown so the loop can close cleanly.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from ingestion.clients.http import AsyncHTTPClient

    _ClientFactory = Callable[[str], AsyncHTTPClient]

# Per-host idle cap. A burst above this ceiling spawns transient clients that
# are closed on release rather than re-pooled. Eight idle keep-alives per
# host is well past the typical demand and bounded enough to keep socket
# usage predictable inside a daemon container.
DEFAULT_MAX_IDLE_PER_HOST = 8


def _normalize_key(base_url: str) -> str:
    """Canonicalise ``base_url`` for cache lookup.

    Strips a trailing slash so callers passing ``"https://x/"`` and
    ``"https://x"`` hash to the same client. Empty input is allowed —
    :class:`AsyncHTTPClient` accepts a blank base.
    """
    return base_url.rstrip("/")


class AsyncHTTPClientPool:
    """Per-base-URL pool of reusable :class:`AsyncHTTPClient` instances.

    Designed for opt-in use from the daemon runtime: callers acquire a
    client for a base URL, do some requests, and release it back. Clients
    are reused across acquires keyed on the normalised URL so long-lived
    HTTPS sessions stay warm.

    Acquire is non-blocking — if no idle client exists for the key one is
    created on the spot. Release re-pools the client unless the per-host
    LRU is already saturated, in which case the released client is closed
    immediately to bound socket use.

    Lifecycle:
        pool = AsyncHTTPClientPool(client_factory)
        client = await pool.acquire(base_url)
        try:
            await client.get(...)
        finally:
            await pool.release(base_url, client)
        await pool.close_all()  # at shutdown
    """

    def __init__(
        self,
        client_factory: _ClientFactory | None = None,
        *,
        max_idle_per_host: int = DEFAULT_MAX_IDLE_PER_HOST,
    ) -> None:
        if max_idle_per_host < 1:
            raise ValueError("max_idle_per_host must be >= 1")
        self._factory: _ClientFactory = client_factory or _default_factory
        self._max_idle = max_idle_per_host
        # OrderedDict per key gives us LRU ordering with O(1) move-to-end.
        # Outer key is the normalised base URL; inner deque is idle clients.
        self._idle: OrderedDict[str, list[AsyncHTTPClient]] = OrderedDict()
        # Acquire/release contention is rare (each acquire touches one entry
        # for a few microseconds), but a single lock keeps the OrderedDict
        # mutation atomic against concurrent coroutines.
        self._lock = asyncio.Lock()
        self._closed = False

    async def acquire(self, base_url: str) -> AsyncHTTPClient:
        """Return an :class:`AsyncHTTPClient` for ``base_url``.

        Reuses an idle pooled instance when one is available, otherwise
        constructs a new client through the configured factory. The caller
        must :meth:`release` the client when done so it returns to the pool.
        """
        if self._closed:
            raise RuntimeError("AsyncHTTPClientPool is closed")
        key = _normalize_key(base_url)
        async with self._lock:
            bucket = self._idle.get(key)
            if bucket:
                client = bucket.pop()
                # Touch the entry so the LRU ordering reflects recent use.
                self._idle.move_to_end(key)
                return client
        # Construction is outside the lock so a slow client_factory
        # (e.g. one that does TLS handshake eagerly) can't serialise the
        # whole pool. Multiple parallel acquires for the same fresh key
        # produce N clients, which release will fold back into the bucket
        # up to the per-host cap.
        return self._factory(base_url)

    async def release(self, base_url: str, client: AsyncHTTPClient) -> None:
        """Return ``client`` to the pool, or close it if the bucket is full.

        Closing under saturation bounds the socket footprint to
        ``max_idle_per_host`` per remote host; without it a parallel burst
        could leave hundreds of idle keep-alives hanging across hosts.
        """
        if self._closed:
            await self._close_quietly(client)
            return
        key = _normalize_key(base_url)
        async with self._lock:
            bucket = self._idle.get(key)
            if bucket is None:
                # First release for this key — create the bucket only when
                # we know we'll use it. Avoids leaving an empty ``[]`` stub
                # behind for a key whose only release is dropped on the
                # floor (e.g. a saturated bucket racing a misuse).
                self._idle[key] = [client]
                self._idle.move_to_end(key)
                return
            if len(bucket) >= self._max_idle:
                # Dropped from the pool — close outside the lock to avoid
                # holding it during network I/O.
                pass
            else:
                bucket.append(client)
                self._idle.move_to_end(key)
                return
        await self._close_quietly(client)

    async def close_all(self) -> None:
        """Close every pooled client. Idempotent — safe to call twice.

        The shutdown path drains in two phases: snapshot the buckets under
        the lock, then close each client outside the lock so a slow close
        on one client cannot block release of another. After this call the
        pool refuses further acquires.
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            buckets = list(self._idle.values())
            self._idle.clear()
        for bucket in buckets:
            for client in bucket:
                await self._close_quietly(client)

    async def _close_quietly(self, client: AsyncHTTPClient) -> None:
        try:
            await client.close()
        except Exception:
            # Pool teardown is best-effort: a broken client cannot block
            # the rest of the pool from draining. Callers that care about
            # close errors should track them per-client themselves.
            return

    @property
    def is_closed(self) -> bool:
        return self._closed

    def idle_count(self, base_url: str | None = None) -> int:
        """Diagnostic helper: count of idle clients (per key or global)."""
        if base_url is None:
            return sum(len(bucket) for bucket in self._idle.values())
        key = _normalize_key(base_url)
        bucket = self._idle.get(key)
        return len(bucket) if bucket else 0


def _default_factory(base_url: str) -> AsyncHTTPClient:
    # Imported lazily so the pool module doesn't pull in httpx at import time
    # for callers that only construct the type for their factory (e.g. tests).
    from ingestion.clients.http import AsyncHTTPClient

    return AsyncHTTPClient(base_url=base_url)
