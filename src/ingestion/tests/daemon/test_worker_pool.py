"""WorkerPool: concurrency caps, dedup, drain, emergency stop."""

from __future__ import annotations

import asyncio

import pytest
from ingestion.daemon.worker import WorkerPool


async def _finite(delay: float = 0.01) -> None:
    await asyncio.sleep(delay)


async def test_submit_runs_coro_and_drains() -> None:
    pool = WorkerPool(global_limit=2)
    pool.submit(user_email="a@x", platform="moodle", coro=_finite())
    pool.submit(user_email="b@x", platform="moodle", coro=_finite())
    still_pending = await pool.drain(timeout=1.0)
    assert still_pending == []
    assert pool.in_flight == 0


async def test_duplicate_submission_is_a_noop() -> None:
    pool = WorkerPool(global_limit=4)

    event = asyncio.Event()

    async def blocking() -> None:
        await event.wait()

    first = pool.submit(user_email="a@x", platform="moodle", coro=blocking())
    second = pool.submit(user_email="a@x", platform="moodle", coro=blocking())
    assert first is not None
    assert second is None
    assert pool.is_busy("a@x", "moodle")

    event.set()
    await pool.drain(timeout=1.0)
    assert not pool.is_busy("a@x", "moodle")


async def test_global_concurrency_is_capped() -> None:
    pool = WorkerPool(global_limit=2)

    active = 0
    peak = 0

    async def tracker() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    for i in range(5):
        pool.submit(user_email=f"u{i}@x", platform="moodle", coro=tracker())
    await pool.drain(timeout=5.0)
    assert peak <= 2
    assert pool.in_flight == 0


async def test_per_platform_concurrency_is_capped() -> None:
    pool = WorkerPool(global_limit=10, per_platform_limits={"moodle": 1})

    moodle_active = 0
    moodle_peak = 0

    async def tracker(platform: str) -> None:
        nonlocal moodle_active, moodle_peak
        if platform == "moodle":
            moodle_active += 1
            moodle_peak = max(moodle_peak, moodle_active)
        await asyncio.sleep(0.05)
        if platform == "moodle":
            moodle_active -= 1

    pool.submit(user_email="a@x", platform="moodle", coro=tracker("moodle"))
    pool.submit(user_email="b@x", platform="moodle", coro=tracker("moodle"))
    pool.submit(user_email="c@x", platform="teams", coro=tracker("teams"))
    await pool.drain(timeout=5.0)
    assert moodle_peak <= 1


async def test_emergency_stop_cancels_in_flight() -> None:
    pool = WorkerPool(global_limit=4)

    stuck = asyncio.Event()

    async def stuck_task() -> None:
        await stuck.wait()

    for i in range(3):
        pool.submit(user_email=f"u{i}@x", platform="moodle", coro=stuck_task())

    await asyncio.sleep(0.01)  # let tasks start
    cancelled = pool.emergency_stop()
    assert cancelled == 3
    await pool.wait_closed()
    assert pool.in_flight == 0


async def test_on_finish_called_with_success_none() -> None:
    results: list[tuple[str, str, BaseException | None]] = []

    async def on_finish(user: str, platform: str, exc: BaseException | None) -> None:
        results.append((user, platform, exc))

    pool = WorkerPool(global_limit=2, on_finish=on_finish)
    pool.submit(user_email="a@x", platform="moodle", coro=_finite())
    await pool.drain(timeout=1.0)

    assert len(results) == 1
    assert results[0] == ("a@x", "moodle", None)


async def test_on_finish_called_with_exception() -> None:
    results: list[tuple[str, str, BaseException | None]] = []

    async def on_finish(user: str, platform: str, exc: BaseException | None) -> None:
        results.append((user, platform, exc))

    async def failing() -> None:
        raise ValueError("boom")

    pool = WorkerPool(global_limit=2, on_finish=on_finish)
    pool.submit(user_email="a@x", platform="moodle", coro=failing())
    await pool.drain(timeout=1.0)

    assert len(results) == 1
    user, platform, exc = results[0]
    assert user == "a@x"
    assert platform == "moodle"
    assert isinstance(exc, ValueError)


async def test_on_finish_skipped_on_cancellation() -> None:
    # Cancellation (operator-initiated emergency stop) is neither success nor
    # failure, so on_finish must not fire. Otherwise every cancelled task
    # would bump the dead-letter counter and a single emergency stop would
    # mass-suppress every in-flight user.
    results: list[tuple[str, str, BaseException | None]] = []

    async def on_finish(user: str, platform: str, exc: BaseException | None) -> None:
        results.append((user, platform, exc))

    async def stuck() -> None:
        await asyncio.Event().wait()

    pool = WorkerPool(global_limit=2, on_finish=on_finish)
    pool.submit(user_email="a@x", platform="moodle", coro=stuck())
    await asyncio.sleep(0.01)
    pool.emergency_stop()
    await pool.wait_closed()

    assert results == []


def test_invalid_global_limit_raises() -> None:
    with pytest.raises(ValueError, match="global_limit"):
        WorkerPool(global_limit=0)
