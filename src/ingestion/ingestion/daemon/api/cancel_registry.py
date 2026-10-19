"""In-memory registry of per-sync cancellation signals.

The ``POST /api/actions/syncs/{sync_id}/cancel`` endpoint needs to tell the
worker that an in-flight sync should abort. Since the scheduler does not
expose the sync's ``asyncio.Task`` by ``sync_id`` (the sync_id is minted
inside ``sync_logger.log_start`` after the task is already running), we
give the endpoint a dedicated fan-out surface instead: a dict keyed by
``sync_id`` that holds an :class:`asyncio.Event`. Producers (the scheduler
/ pipeline glue, when wired) call :meth:`register` at log_start and
:meth:`unregister` at log_complete/log_failure; the cancel endpoint calls
:meth:`signal` to flip the event. A pipeline that polls the event between
stages can then abort cooperatively.

The registry is best-effort: if no one has registered the sync_id yet
(typical today — the pipeline does not poll), :meth:`signal` returns
``False`` and the cancel endpoint still writes a terminal ``cancelled``
row in ``sync_history`` and emits the WS event. That way the operator's
intent is persisted regardless of whether the running task reaches a
checkpoint before completing.
"""

from __future__ import annotations

import asyncio


class CancelRegistry:
    """Track per-sync_id cancellation Events.

    Thread-safe via a single internal lock. The Events themselves are plain
    :class:`asyncio.Event` instances — cheap, loop-local, easy to poll from
    the pipeline. One registry per app.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, sync_id: str) -> asyncio.Event:
        """Register ``sync_id`` and return its Event. Idempotent: a repeat
        register returns the existing Event so two concurrent callers can't
        clobber each other's cancellation signal."""
        async with self._lock:
            event = self._events.get(sync_id)
            if event is None:
                event = asyncio.Event()
                self._events[sync_id] = event
            return event

    async def unregister(self, sync_id: str) -> None:
        """Drop ``sync_id`` from the registry. Safe to call on a sync_id
        that was never registered."""
        async with self._lock:
            self._events.pop(sync_id, None)

    async def signal(self, sync_id: str) -> bool:
        """Flip the Event for ``sync_id`` if it exists. Returns ``True``
        when a subscriber was present, ``False`` when the sync was not
        registered (typical when the pipeline does not poll the event).
        Callers rely on the return value to decide whether to note
        "cancellation delivered" vs "best-effort persisted" in audit."""
        async with self._lock:
            event = self._events.get(sync_id)
        if event is None:
            return False
        event.set()
        return True

    async def is_signalled(self, sync_id: str) -> bool:
        """Return ``True`` if ``sync_id`` has a registered, set Event."""
        async with self._lock:
            event = self._events.get(sync_id)
        return event is not None and event.is_set()
