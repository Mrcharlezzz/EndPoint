"""Per-user concurrency cap on :class:`WorkerPool` (slice 10).

Pins the new ``per_user_limit`` knob:

* the per-user semaphore caps concurrent in-flight syncs per email so
  one chatty user can't monopolise the global pool;
* different users are not mutually blocked;
* ``per_user_limit=0`` is a documented kill switch — every submission
  is rejected;
* the per-user dict grows lazily as new users are seen; entries are
  removed only on an explicit :meth:`WorkerPool.remove_user` call (the
  slice-12 lifecycle hook), not on drain or emergency_stop;
* the per-user cap interacts with the global + per-platform caps —
  whichever is hit first throttles the task.

Tests use very short sleeps and ``asyncio.gather`` to drive overlapping
submissions deterministically — the global semaphore is held only for
the duration of the wrapped coroutine, so brief sleeps + a peak counter
suffice to catch cap violations.
"""

from __future__ import annotations

import asyncio

import pytest
from ingestion.daemon.worker import WorkerPool

# ---------------------------------------------------------------------------
# Per-user cap
# ---------------------------------------------------------------------------


async def test_three_concurrent_submits_for_one_user_blocks_third() -> None:
    """``per_user_limit=2``: two of three submissions for the same email
    run concurrently, the third waits for one to finish."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)
    active = 0
    peak = 0

    async def tracker() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1

    # Three different platforms so the busy-dedup doesn't reject them.
    tasks = [
        pool.submit(user_email="alice@x", platform="moodle", coro=tracker()),
        pool.submit(user_email="alice@x", platform="teams", coro=tracker()),
        pool.submit(user_email="alice@x", platform="telegram", coro=tracker()),
    ]
    assert all(t is not None for t in tasks)
    await pool.drain(timeout=2.0)
    assert peak <= 2


async def test_different_users_not_mutually_blocked() -> None:
    """A throttled user does not block sync slots for another user."""
    pool = WorkerPool(global_limit=10, per_user_limit=1)
    alice_active = 0
    alice_peak = 0
    bob_active = 0
    bob_peak = 0

    async def alice_tracker() -> None:
        nonlocal alice_active, alice_peak
        alice_active += 1
        alice_peak = max(alice_peak, alice_active)
        await asyncio.sleep(0.02)
        alice_active -= 1

    async def bob_tracker() -> None:
        nonlocal bob_active, bob_peak
        bob_active += 1
        bob_peak = max(bob_peak, bob_active)
        await asyncio.sleep(0.02)
        bob_active -= 1

    pool.submit(user_email="alice@x", platform="moodle", coro=alice_tracker())
    pool.submit(user_email="alice@x", platform="teams", coro=alice_tracker())
    pool.submit(user_email="bob@x", platform="moodle", coro=bob_tracker())
    pool.submit(user_email="bob@x", platform="teams", coro=bob_tracker())
    await pool.drain(timeout=2.0)
    # Each user observes their own cap.
    assert alice_peak <= 1
    assert bob_peak <= 1


# ---------------------------------------------------------------------------
# Kill switch (per_user_limit=0)
# ---------------------------------------------------------------------------


async def test_per_user_limit_zero_rejects_every_submission() -> None:
    """``per_user_limit=0`` is a documented kill switch — every submit
    returns ``None`` (not scheduled)."""
    pool = WorkerPool(global_limit=10, per_user_limit=0)
    ran = False

    async def fn() -> None:
        nonlocal ran
        ran = True

    task = pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    assert task is None
    await asyncio.sleep(0.02)
    # Coroutine never invoked.
    assert ran is False


async def test_per_user_limit_zero_rejects_for_every_user() -> None:
    """The kill switch is global — different users are also rejected."""
    pool = WorkerPool(global_limit=10, per_user_limit=0)

    async def fn() -> None:
        return None

    for email in ("alice@x", "bob@x", "carol@x"):
        assert pool.submit(user_email=email, platform="moodle", coro=fn()) is None


# ---------------------------------------------------------------------------
# Cap interaction with global / per-platform caps
# ---------------------------------------------------------------------------


async def test_per_user_cap_interacts_with_per_platform_cap() -> None:
    """Two different users on the same platform are bounded by the
    per-platform cap (1) — the per-user cap (2) is the looser bound,
    so the platform cap wins."""
    pool = WorkerPool(
        global_limit=10,
        per_platform_limits={"moodle": 1},
        per_user_limit=2,
    )
    moodle_active = 0
    moodle_peak = 0

    async def tracker() -> None:
        nonlocal moodle_active, moodle_peak
        moodle_active += 1
        moodle_peak = max(moodle_peak, moodle_active)
        await asyncio.sleep(0.02)
        moodle_active -= 1

    pool.submit(user_email="alice@x", platform="moodle", coro=tracker())
    pool.submit(user_email="bob@x", platform="moodle", coro=tracker())
    pool.submit(user_email="carol@x", platform="moodle", coro=tracker())
    await pool.drain(timeout=2.0)
    assert moodle_peak <= 1


async def test_per_user_cap_interacts_with_global_cap() -> None:
    """A loose per-user cap doesn't bypass the global cap."""
    pool = WorkerPool(global_limit=2, per_user_limit=5)
    active = 0
    peak = 0

    async def tracker() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1

    # Five different users so the per-user cap doesn't fire.
    for i, user in enumerate(["a", "b", "c", "d", "e"]):
        pool.submit(user_email=f"{user}@x", platform=f"p{i}", coro=tracker())
    await pool.drain(timeout=2.0)
    assert peak <= 2


# ---------------------------------------------------------------------------
# Per-user dict population + leak pin
# ---------------------------------------------------------------------------


async def test_per_user_dict_lazily_populated_for_new_users() -> None:
    """The dict is private but observable through ``_per_user_sems``
    after submission. New users get fresh semaphores allocated lazily
    inside ``_run``, so we drain to let the task body execute."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)

    async def fn() -> None:
        return None

    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001
    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    pool.submit(user_email="bob@x", platform="moodle", coro=fn())
    # Drain so the wrapped coroutines actually invoke ``_user_sem`` and
    # populate the dict — submit only schedules the task.
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    assert "bob@x" in pool._per_user_sems  # noqa: SLF001


async def test_per_user_dict_does_not_clean_up_on_drain() -> None:
    """Drain alone does not remove entries — only ``remove_user`` does.

    A user whose sync just finished may submit again on the next tick;
    we don't want to discard the semaphore proactively on every drain.
    Cleanup is gated on the explicit lifecycle signal, asserted via
    :func:`test_remove_user_pops_entry`.
    """
    pool = WorkerPool(global_limit=10, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    # Sync done, but the dict still carries the entry until remove_user.
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001


async def test_remove_user_pops_entry() -> None:
    """Slice 12: ``remove_user`` is the explicit cleanup hook and pops
    the per-user semaphore from the dict."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)

    async def fn() -> None:
        return None

    pool.submit(user_email="alice@x", platform="moodle", coro=fn())
    await pool.drain(timeout=1.0)
    assert "alice@x" in pool._per_user_sems  # noqa: SLF001
    assert pool.remove_user("alice@x") is True
    assert "alice@x" not in pool._per_user_sems  # noqa: SLF001


async def test_remove_user_no_op_for_unseen_email() -> None:
    """Removing a user without a semaphore is a no-op returning False
    (idempotent — cascade_delete_user can call it unconditionally)."""
    pool = WorkerPool(global_limit=10, per_user_limit=2)
    assert pool.remove_user("ghost@x") is False


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_negative_per_user_limit_raises() -> None:
    """Negative per-user limits are caller misuse; reject up front."""
    with pytest.raises(ValueError, match="per_user_limit"):
        WorkerPool(global_limit=2, per_user_limit=-1)


def test_default_per_user_limit_is_two() -> None:
    """Default cap matches the slice 10 config (``max_syncs_per_user=2``)
    so a fresh ``WorkerPool(global_limit=N)`` already has the bound."""
    pool = WorkerPool(global_limit=4)
    # Private but observable for the test pin.
    assert pool._per_user_limit == 2  # noqa: SLF001
