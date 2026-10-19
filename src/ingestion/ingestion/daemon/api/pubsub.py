"""In-memory event bus for WebSocket fan-out.

One :class:`EventBus` instance per API app. Publishers (the log handler,
scheduler) call :meth:`EventBus.publish` from any thread; subscribers (WS
connections) register an :class:`asyncio.Queue` via :meth:`subscribe` and
pull events off until they close.

Channels keep things simple: the bus holds a dict of str → list[queue]. A
publisher declares its channel; a subscriber picks which ones to listen to.
Slow subscribers get backpressure via the queue's bounded size — if the
queue fills the publisher drops the event for that subscriber (better than
blocking the whole bus).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ingestion.daemon.logging import get_logger

DEFAULT_QUEUE_SIZE = 256


class EventBus:
    """Thread-safe-ish: publish is callable from any thread via
    :func:`asyncio.run_coroutine_threadsafe`. In-process only; no network."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._log = get_logger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Call from startup so threadsafe publishers can schedule on it."""
        self._loop = loop

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        """Enqueue ``event`` to every subscriber of ``channel``. Drops events
        for subscribers whose queue is full (never blocks the publisher)."""
        async with self._lock:
            queues = list(self._subs.get(channel, ()))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._log.warning("eventbus.subscriber_slow_dropped", channel=channel)

    def publish_threadsafe(self, channel: str, event: dict[str, Any]) -> None:
        """Safe to call from non-loop threads. No-op if ``attach_loop``
        wasn't called yet (tests can opt out of wiring the loop)."""
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.publish(channel, event), loop)

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        channel: str,
        *,
        max_size: int = DEFAULT_QUEUE_SIZE,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_size)
        async with self._lock:
            self._subs.setdefault(channel, []).append(q)
        try:
            yield q
        finally:
            async with self._lock:
                subs = self._subs.get(channel)
                if subs is not None and q in subs:
                    subs.remove(q)
                    if not subs:
                        del self._subs[channel]

    async def subscriber_count(self, channel: str) -> int:
        async with self._lock:
            return len(self._subs.get(channel, ()))
