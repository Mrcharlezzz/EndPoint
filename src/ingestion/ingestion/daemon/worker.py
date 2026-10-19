"""Bounded-concurrency pool that runs one :func:`run_sync` per task.

Three-layer limiting:

* a global :class:`asyncio.Semaphore` caps total in-flight syncs across
  every (user, platform) pair;
* an optional per-platform semaphore caps concurrency per adapter
  (e.g. Teams has tighter rate limits than Moodle);
* a per-user semaphore caps concurrent syncs per email so a single
  user with 10k items cannot monopolise the global pool slots and
  starve other users.

The per-user dict is lazily populated on first submission for a given
email. Entries are removed when the lifecycle layer signals a user
removal — see :meth:`WorkerPool.remove_user`. The cleanup hook closes
the slice-10 deferral noted in earlier revisions of this docstring;
``cascade_delete_user`` in :mod:`ingestion.daemon.lifecycle.cleanup`
is the canonical caller.

The drain / emergency-stop paths intentionally DO NOT prune
``_per_user_sems`` — a user whose sync was just cancelled by a
broad emergency stop may resume on the next tick, and we don't want
to discard their semaphore mid-flight. Pruning is gated on an
explicit user-removal signal via :meth:`remove_user`.

Tasks are plain ``asyncio.Task`` objects so the scheduler can ``drain`` (await
them all) or ``emergency_stop`` (cancel them all). Success or failure
notifications are routed through a caller-supplied callback so the
:class:`ingestion.daemon.dead_letter.DeadLetterTracker` can update its state
without coupling the pool to it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from ingestion.daemon.logging import get_logger

# (user_email, platform, exc_or_None) -> None. ``None`` means success.
type TaskCallback = Callable[[str, str, BaseException | None], Awaitable[None]]


class WorkerPool:
    """Run async coroutines under global + per-platform concurrency caps."""

    def __init__(
        self,
        *,
        global_limit: int,
        per_platform_limits: dict[str, int] | None = None,
        per_user_limit: int = 2,
        on_finish: TaskCallback | None = None,
    ) -> None:
        if global_limit < 1:
            raise ValueError("global_limit must be >= 1")
        if per_user_limit < 0:
            raise ValueError("per_user_limit must be >= 0")
        self._global_sem = asyncio.Semaphore(global_limit)
        self._platform_sems: dict[str, asyncio.Semaphore] = {
            platform: asyncio.Semaphore(limit)
            for platform, limit in (per_platform_limits or {}).items()
            if limit >= 1
        }
        # Per-user fairness cap. ``0`` is a documented kill switch
        # (admit nothing for any user). Lazy-init in ``submit`` so a
        # user we've never seen doesn't allocate a Semaphore until it
        # actually queues work. Capacity is captured at construction;
        # changes to scheduler.max_syncs_per_user via overlay take
        # effect on the next pool rebuild (today: process restart).
        self._per_user_limit = per_user_limit
        self._per_user_sems: dict[str, asyncio.Semaphore] = {}
        self._on_finish = on_finish
        self._tasks: set[asyncio.Task[None]] = set()
        self._busy: set[tuple[str, str]] = set()
        self._stopped = False
        self._log = get_logger(__name__)

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def is_busy(self, user_email: str, platform: str) -> bool:
        return (user_email, platform) in self._busy

    def submit(
        self,
        *,
        user_email: str,
        platform: str,
        coro: Awaitable[None],
    ) -> asyncio.Task[None] | None:
        """Schedule a sync. Returns ``None`` if (user, platform) is already busy.

        The caller must not await ``coro`` directly — the pool drives it under
        the semaphores. Passing it as an awaitable makes the call site read as
        ``pool.submit(coro=self._run_for(user, platform))``.

        ``per_user_limit=0`` rejects EVERY submission (kill switch).
        Otherwise the per-user semaphore is acquired inside ``_run``
        alongside the platform + global semaphores; first one to hit
        any cap blocks the task at that level.
        """
        key = (user_email, platform)
        if self._stopped or key in self._busy or self._per_user_limit == 0:
            # Close the coro so Python doesn't warn about an un-awaited
            # coroutine. ``close`` on an already-awaited or non-coroutine
            # awaitable is a best-effort no-op.
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return None
        self._busy.add(key)
        task = asyncio.create_task(
            self._run(user_email=user_email, platform=platform, coro=coro),
            name=f"sync:{user_email}:{platform}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(
        self,
        *,
        user_email: str,
        platform: str,
        coro: Awaitable[None],
    ) -> None:
        try:
            # Acquisition order matters: tighter caps first. Per-user
            # semaphore is the tightest (a single user typically only
            # queues 1-2 syncs at once), then per-platform, then
            # global. Holding a global permit while waiting on a
            # tighter cap would let a single platform's queue flood
            # starve every other platform.
            user_sem = self._user_sem(user_email)
            platform_sem = self._platform_sems.get(platform)
            if platform_sem is None:
                async with user_sem, self._global_sem:
                    await coro
            else:
                async with user_sem, platform_sem, self._global_sem:
                    await coro
        except asyncio.CancelledError:
            self._log.info(
                "worker.task_cancelled",
                user_email=user_email,
                platform=platform,
            )
            # Don't call on_finish on cancel: the callback's job is to
            # update dead-letter bookkeeping for an actual sync outcome.
            # An operator-initiated emergency stop is neither success nor
            # failure, and recording it as a failure would mass-suppress
            # every in-flight user after a single emergency stop.
            raise
        except Exception as exc:
            # Only swallow ordinary exceptions. ``KeyboardInterrupt`` and
            # ``SystemExit`` (BaseException but not Exception) must propagate.
            self._log.exception(
                "worker.task_errored",
                user_email=user_email,
                platform=platform,
            )
            await self._notify(user_email, platform, exc)
        else:
            await self._notify(user_email, platform, None)
        finally:
            self._busy.discard((user_email, platform))

    def _user_sem(self, user_email: str) -> asyncio.Semaphore:
        """Get-or-create the per-user semaphore for ``user_email``.

        Lazy: first sync for a never-before-seen user allocates one
        :class:`asyncio.Semaphore` capped at ``per_user_limit``. The
        dict grows with active users seen since process start; entries
        are removed only on an explicit :meth:`remove_user` call so
        the cleanup happens from the lifecycle layer (where we know
        the user has actually been deleted) rather than from a
        guess-based idle-timeout scan.
        """
        sem = self._per_user_sems.get(user_email)
        if sem is None:
            sem = asyncio.Semaphore(self._per_user_limit)
            self._per_user_sems[user_email] = sem
        return sem

    def remove_user(self, user_email: str) -> bool:
        """Drop the per-user semaphore for ``user_email`` if present.

        Called by the cascade-delete path in
        :mod:`ingestion.daemon.lifecycle.cleanup` after the SQL rows
        are gone. Idempotent: removing a user who has no semaphore
        (never submitted, or already pruned) returns ``False`` and
        is otherwise a no-op.

        Race note: the cascade delete runs on the same event loop the
        pool serves, so ``remove_user`` cannot interleave with a
        concurrent ``_user_sem`` call inside ``_run``. A submission
        racing this call would either pre-allocate a fresh semaphore
        AFTER the pop (the new submission keeps running with the new
        semaphore) or BEFORE it (the in-flight sync drains; the next
        submission for that email re-allocates). Either way no task
        loses its semaphore mid-flight.
        """
        return self._per_user_sems.pop(user_email, None) is not None

    async def _notify(
        self,
        user_email: str,
        platform: str,
        exc: BaseException | None,
    ) -> None:
        if self._on_finish is None:
            return
        try:
            await self._on_finish(user_email, platform, exc)
        except Exception:
            self._log.exception("worker.on_finish_callback_errored")

    async def drain(self, timeout: float | None = None) -> list[asyncio.Task[None]]:
        """Await all in-flight tasks. Returns tasks still running past timeout."""
        if not self._tasks:
            return []
        pending: set[asyncio.Task[None]] = set(self._tasks)
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        _ = done
        return list(still_pending)

    def emergency_stop(self) -> int:
        """Cancel every in-flight task and refuse new submissions.

        Returns the count cancelled. Destructive: the pipeline is interrupted
        mid-flight so cursors the interrupted task would have written do NOT
        advance. Once stopped the pool is terminal — construct a new one to
        resume submitting.
        """
        self._stopped = True
        cancelled = 0
        for task in list(self._tasks):
            if task.cancel():
                cancelled += 1
        return cancelled

    async def wait_closed(self) -> None:
        """Await every task to finish (success, failure, or cancellation)."""
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        """Cancel + await every task. Safe to call more than once."""
        self.emergency_stop()
        with contextlib.suppress(Exception):
            await self.wait_closed()
