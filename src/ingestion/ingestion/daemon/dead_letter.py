"""Consecutive-failure tracking with cooldown-based suppression.

A run of ``threshold`` failures (in a row, with no success in between) flips
the (user, platform) into a suppressed state until ``suppressed_until``. The
scheduler consults :meth:`is_suppressed` on every tick and skips rows that
are still cooling off.

Control panel actions (future phase) call :meth:`clear` to un-suppress a row
immediately.

When constructed with an :class:`AsyncConnectionPool`, every read and write
checks out a long-lived pooled connection instead of opening a fresh
``aiosqlite.connect`` per call. This skips the per-call open + pragma
round-trip, which under burst dominates the actual write time.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from ingestion.daemon.store.conn_pool import AsyncConnectionPool

# (channel, event_payload) -> awaitable. Matches EventBus.publish so
# the runtime can pass ``bus.publish`` directly. Defined at module
# scope so callers can declare the type without importing the bus.
type EventPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class DeadLetterTracker:
    """Read/write ``dead_letter_state`` and answer "is this row suppressed?"."""

    def __init__(
        self,
        db_path: Path,
        *,
        threshold: int,
        cooldown_seconds: int,
        now: Callable[[], datetime] | None = None,
        pool: AsyncConnectionPool | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        self._db_path = db_path
        self._threshold = threshold
        self._cooldown = timedelta(seconds=cooldown_seconds)
        # Tests inject a frozen clock; production leaves this None and we
        # fall back to wall-clock UTC.
        self._now = now if now is not None else _utc_now
        # Optional shared connection pool. When set, every helper checks out
        # a pooled connection for the duration of one statement; otherwise
        # we fall back to ``aiosqlite.connect`` for backwards compatibility
        # with callers that pre-date the pool (and tests that pass paths
        # only).
        self._pool = pool
        # Optional publisher for ``dead_letter.suppressed`` /
        # ``dead_letter.cleared`` events. Slice 10 wires the EventBus
        # via runtime composition so the dashboard's WS subscriber and
        # the alert engine see state transitions in real time. ``None``
        # at construction is the historical behaviour (events drop on
        # the floor); tests that don't care can leave it unset.
        self._event_publisher = event_publisher
        self._log = get_logger(__name__)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a connection from the pool or a fresh aiosqlite.connect.

        Centralised so the read/write helpers below stay focused on SQL.
        Pool-backed acquisitions return through the pool's release path on
        exit; fallback acquisitions close the connection on exit.
        """
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                yield conn
            return
        async with aiosqlite.connect(self._db_path) as conn:
            yield conn

    async def record_success(self, user_email: str, platform: str) -> None:
        """Reset the counter. Does not alter ``suppressed_until`` rows; that
        exit path is either the cooldown expiring or an explicit :meth:`clear`.
        """
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_state (
                    user_email, platform, consecutive_failures, last_failure_at, suppressed_until
                )
                VALUES (?, ?, 0, NULL, NULL)
                ON CONFLICT(user_email, platform) DO UPDATE SET
                    consecutive_failures = 0,
                    last_failure_at = NULL
                """,
                (user_email, platform),
            )
            await conn.commit()

    async def record_failure(self, user_email: str, platform: str) -> None:
        """Increment the counter; suppress if the new count hits the threshold.

        The increment is atomic in a single SQL statement so concurrent
        failures for the same (user, platform) can't read-then-write
        each other into a lost update. The scheduler dedupes per
        (user, platform) via worker.is_busy, but API-layer callers
        (dead_letter clear + a sync failure) can still race.

        Slice 10: emits a ``dead_letter.suppressed`` event the first
        time the row enters the suppressed state. Detection requires
        a pre-write read of the prior ``suppressed_until`` so the
        transition fires once per pause/unpause/pause cycle (rather
        than every failure that lands while already suppressed).
        """
        now = self._now()
        iso_now = now.isoformat()
        suppress_if_threshold = (now + self._cooldown).isoformat()
        # For a fresh row, new_count == 1; pre-compute whether it should
        # be suppressed on insert. For the ON CONFLICT UPDATE path, the
        # CASE references ``consecutive_failures`` which in that context
        # is the OLD value; adding 1 gives the post-update count.
        new_row_suppressed = suppress_if_threshold if self._threshold <= 1 else None
        async with self._conn() as conn:
            # Read prior state inside the same connection so we can
            # detect transition into suppression. The ON CONFLICT
            # statement's CASE does the actual decision atomically;
            # this read is purely advisory for the transition event.
            prior_cursor = await conn.execute(
                "SELECT consecutive_failures, suppressed_until "
                "FROM dead_letter_state WHERE user_email = ? AND platform = ?",
                (user_email, platform),
            )
            prior = await prior_cursor.fetchone()
            prior_suppressed_until = prior[1] if prior is not None else None
            await conn.execute(
                """
                INSERT INTO dead_letter_state (
                    user_email, platform, consecutive_failures, last_failure_at, suppressed_until
                )
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(user_email, platform) DO UPDATE SET
                    consecutive_failures = consecutive_failures + 1,
                    last_failure_at = excluded.last_failure_at,
                    suppressed_until = CASE
                        WHEN consecutive_failures + 1 >= ?
                        THEN ?
                        ELSE suppressed_until
                    END
                """,
                (
                    user_email,
                    platform,
                    iso_now,
                    new_row_suppressed,
                    self._threshold,
                    suppress_if_threshold,
                ),
            )
            await conn.commit()
            # Re-read the post-write state so the event payload carries
            # the actual final count and suppression window. One extra
            # round-trip per failure is fine — failures are far from a
            # hot path.
            post_cursor = await conn.execute(
                "SELECT consecutive_failures, suppressed_until "
                "FROM dead_letter_state WHERE user_email = ? AND platform = ?",
                (user_email, platform),
            )
            post = await post_cursor.fetchone()
        if post is None:
            return
        new_count = int(post[0])
        new_suppressed_until = post[1]
        # Transition: prior had no suppression, post does. Emit once.
        if prior_suppressed_until is None and new_suppressed_until is not None:
            await self._emit(
                "dead_letter.suppressed",
                user_email=user_email,
                platform=platform,
                consecutive_failures=new_count,
                suppressed_until=new_suppressed_until,
            )

    async def _emit(
        self,
        event_name: str,
        *,
        user_email: str,
        platform: str,
        consecutive_failures: int,
        suppressed_until: str | None,
    ) -> None:
        """Send an event to the structured logger and optional event bus.

        Logs unconditionally — the structured-log feed is the
        canonical record alert subscribers can subscribe to via the
        log table. The publisher fan-out is best-effort: a slow or
        crashing subscriber must not block the dead-letter write
        path. We log the failure and move on so a misbehaving
        subscriber can't take the daemon down.
        """
        self._log.warning(
            event_name,
            user_email=user_email,
            platform=platform,
            consecutive_failures=consecutive_failures,
            suppressed_until=suppressed_until,
        )
        publisher = self._event_publisher
        if publisher is None:
            return
        payload: dict[str, Any] = {
            "event": event_name,
            "user_email": user_email,
            "platform": platform,
            "consecutive_failures": consecutive_failures,
            "suppressed_until": suppressed_until,
        }
        try:
            # Two channels so dashboards can subscribe to either the
            # focused dead_letter feed or the broader errors stream
            # without mirroring events clientside.
            await publisher("dead_letter", payload)
            await publisher("errors", payload)
        except Exception:
            # Subscriber crashes must NOT propagate; log + drop. The
            # bounded-queue EventBus already handles slow subscribers
            # by dropping events; this branch covers programming-error
            # crashes inside a subscriber callback.
            self._log.exception("dead_letter.event_publisher_errored")

    async def is_suppressed(self, user_email: str, platform: str) -> bool:
        """Return True if ``suppressed_until`` is set and still in the future."""
        async with self._conn() as conn:
            cursor = await conn.execute(
                "SELECT suppressed_until FROM dead_letter_state "
                "WHERE user_email = ? AND platform = ?",
                (user_email, platform),
            )
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            return False
        suppressed_until = datetime.fromisoformat(row[0])
        return self._now() < suppressed_until

    async def clear(self, user_email: str, platform: str) -> None:
        """Un-suppress immediately. Resets the counter too.

        Slice 10: emits a ``dead_letter.cleared`` event when the prior
        row was suppressed. A clear on an already-clear row is a no-op
        from the event-bus perspective — no transition occurred.
        """
        async with self._conn() as conn:
            prior_cursor = await conn.execute(
                "SELECT consecutive_failures, suppressed_until "
                "FROM dead_letter_state WHERE user_email = ? AND platform = ?",
                (user_email, platform),
            )
            prior = await prior_cursor.fetchone()
            prior_suppressed_until = prior[1] if prior is not None else None
            prior_count = int(prior[0]) if prior is not None else 0
            await conn.execute(
                """
                INSERT INTO dead_letter_state (
                    user_email, platform, consecutive_failures, last_failure_at, suppressed_until
                )
                VALUES (?, ?, 0, NULL, NULL)
                ON CONFLICT(user_email, platform) DO UPDATE SET
                    consecutive_failures = 0,
                    last_failure_at = NULL,
                    suppressed_until = NULL
                """,
                (user_email, platform),
            )
            await conn.commit()
        if prior_suppressed_until is not None:
            await self._emit(
                "dead_letter.cleared",
                user_email=user_email,
                platform=platform,
                consecutive_failures=prior_count,
                suppressed_until=None,
            )
