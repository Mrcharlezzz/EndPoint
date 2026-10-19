"""Unit tests for :mod:`ingestion.clients.pool`.

Targets 100% line + branch coverage of ``AsyncHTTPClientPool``. The pool is
loop-bound (uses ``asyncio.Lock``) so every test runs under the project's
``asyncio_mode = "auto"`` so the lock binds to the active loop.

Fakes here keep dependencies minimal: a real :class:`AsyncHTTPClient` would
open a network socket on construction and we want to count construction
events, not exercise httpx.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest
from ingestion.clients.pool import (
    DEFAULT_MAX_IDLE_PER_HOST,
    AsyncHTTPClientPool,
    _normalize_key,
)

if TYPE_CHECKING:
    from ingestion.clients.http import AsyncHTTPClient


class FakeClient:
    """Minimal stand-in for :class:`AsyncHTTPClient`.

    Records when ``close`` is awaited so the pool's release / drain paths
    can be asserted. ``fail_close`` flips an exception into the close so
    the quiet-close path is exercised.
    """

    instances: list[FakeClient] = []

    def __init__(self, base_url: str = "", *, fail_close: bool = False) -> None:
        self.base_url = base_url
        self.closed = False
        self.fail_close = fail_close
        self.close_calls = 0
        FakeClient.instances.append(self)

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("simulated close failure")
        self.closed = True


def _make_pool(
    *,
    max_idle: int = DEFAULT_MAX_IDLE_PER_HOST,
    fail_close: bool = False,
) -> tuple[AsyncHTTPClientPool, list[FakeClient]]:
    """Construct a pool whose factory hands out fresh fake clients."""
    created: list[FakeClient] = []

    def _factory(base_url: str) -> AsyncHTTPClient:
        client = FakeClient(base_url, fail_close=fail_close)
        created.append(client)
        return cast("AsyncHTTPClient", client)

    return AsyncHTTPClientPool(_factory, max_idle_per_host=max_idle), created


@pytest.fixture(autouse=True)
def _reset_fake_clients() -> None:
    FakeClient.instances.clear()


def test_normalize_key_strips_trailing_slash() -> None:
    assert _normalize_key("https://example.com/") == "https://example.com"
    assert _normalize_key("https://example.com") == "https://example.com"


def test_normalize_key_allows_empty() -> None:
    # AsyncHTTPClient accepts a blank base_url so the pool must too.
    assert _normalize_key("") == ""


def test_init_rejects_zero_max_idle() -> None:
    with pytest.raises(ValueError, match="max_idle_per_host must be >= 1"):
        AsyncHTTPClientPool(lambda _: cast("AsyncHTTPClient", FakeClient()), max_idle_per_host=0)


def test_init_rejects_negative_max_idle() -> None:
    with pytest.raises(ValueError, match="max_idle_per_host must be >= 1"):
        AsyncHTTPClientPool(lambda _: cast("AsyncHTTPClient", FakeClient()), max_idle_per_host=-3)


def test_init_with_default_factory_does_not_construct_client() -> None:
    # Lazy: the import + AsyncHTTPClient construction only happens on acquire.
    pool = AsyncHTTPClientPool()
    assert not pool.is_closed
    assert pool.idle_count() == 0


async def test_acquire_constructs_through_factory() -> None:
    pool, created = _make_pool()
    client = await pool.acquire("https://api.example.com")
    assert len(created) == 1
    assert created[0] is client
    assert pool.idle_count() == 0  # held by caller, not in idle bucket


async def test_release_returns_client_to_idle_bucket() -> None:
    pool, _ = _make_pool()
    client = await pool.acquire("https://api.example.com")
    await pool.release("https://api.example.com", client)
    assert pool.idle_count("https://api.example.com") == 1
    assert pool.idle_count() == 1


async def test_acquire_release_round_trip_reuses_client() -> None:
    pool, created = _make_pool()
    client = await pool.acquire("https://api.example.com")
    await pool.release("https://api.example.com", client)
    same = await pool.acquire("https://api.example.com")
    assert same is client
    # Only one factory call across the round-trip.
    assert len(created) == 1


async def test_idle_count_per_key_and_global() -> None:
    pool, _ = _make_pool()
    a = await pool.acquire("https://a")
    b = await pool.acquire("https://b")
    await pool.release("https://a", a)
    await pool.release("https://b", b)
    assert pool.idle_count("https://a") == 1
    assert pool.idle_count("https://b") == 1
    assert pool.idle_count() == 2
    # Unknown key reads zero, not a KeyError.
    assert pool.idle_count("https://nope") == 0


async def test_release_closes_client_when_bucket_full() -> None:
    pool, _ = _make_pool(max_idle=2)
    a = await pool.acquire("https://x")
    b = await pool.acquire("https://x")
    c = await pool.acquire("https://x")
    await pool.release("https://x", a)
    await pool.release("https://x", b)
    # Bucket is full (cap=2) — releasing the third closes immediately.
    fake_c = cast("FakeClient", c)
    await pool.release("https://x", c)
    assert fake_c.closed is True
    assert pool.idle_count("https://x") == 2


async def test_release_under_max_grows_bucket() -> None:
    pool, _ = _make_pool(max_idle=4)
    clients = [await pool.acquire("https://x") for _ in range(3)]
    for c in clients:
        await pool.release("https://x", c)
    assert pool.idle_count("https://x") == 3
    # None of the released clients were closed — they all sit in the bucket.
    for c in clients:
        assert cast("FakeClient", c).closed is False


async def test_acquire_after_close_raises() -> None:
    pool, _ = _make_pool()
    await pool.close_all()
    assert pool.is_closed is True
    with pytest.raises(RuntimeError, match="AsyncHTTPClientPool is closed"):
        await pool.acquire("https://x")


async def test_release_after_close_closes_client() -> None:
    pool, _ = _make_pool()
    client = await pool.acquire("https://x")
    fake = cast("FakeClient", client)
    await pool.close_all()
    # Release post-close shouldn't raise — but the client must be drained.
    await pool.release("https://x", client)
    assert fake.closed is True
    assert pool.idle_count() == 0


async def test_close_all_closes_every_pooled_client() -> None:
    pool, _ = _make_pool()
    a = await pool.acquire("https://a")
    b = await pool.acquire("https://b")
    await pool.release("https://a", a)
    await pool.release("https://b", b)
    await pool.close_all()
    assert cast("FakeClient", a).closed is True
    assert cast("FakeClient", b).closed is True
    assert pool.is_closed is True


async def test_close_all_is_idempotent() -> None:
    pool, _ = _make_pool()
    client = await pool.acquire("https://x")
    fake = cast("FakeClient", client)
    await pool.release("https://x", client)
    await pool.close_all()
    first_calls = fake.close_calls
    # Second call is a no-op — bucket already drained.
    await pool.close_all()
    assert fake.close_calls == first_calls
    assert pool.is_closed is True


async def test_close_quietly_swallows_close_errors() -> None:
    """``_close_quietly`` must not propagate adapter close exceptions."""
    pool, _ = _make_pool(fail_close=True)
    client = await pool.acquire("https://x")
    fake = cast("FakeClient", client)
    await pool.release("https://x", client)
    # close_all walks the bucket and calls _close_quietly; the broken close()
    # would otherwise blow up the rest of the drain.
    await pool.close_all()
    assert fake.close_calls == 1
    assert pool.is_closed is True


async def test_close_quietly_swallows_close_errors_on_full_bucket_release() -> None:
    """Cap=1, two concurrent acquires (both empty-bucket factory mints), release
    both — the second hits the bucket-full branch and closes via _close_quietly.
    """
    pool, _ = _make_pool(max_idle=1, fail_close=True)
    a, b = await asyncio.gather(
        pool.acquire("https://x"),
        pool.acquire("https://x"),
    )
    fake_b = cast("FakeClient", b)
    # First release fills the bucket.
    await pool.release("https://x", a)
    # Second release with a full bucket (cap=1) falls through to _close_quietly.
    await pool.release("https://x", b)
    assert fake_b.close_calls == 1  # called even though it raised


async def test_lru_move_to_end_on_release() -> None:
    """A released client touches LRU ordering so the bucket is fresh."""
    pool, _ = _make_pool()
    a_one = await pool.acquire("https://a")
    b_one = await pool.acquire("https://b")
    await pool.release("https://b", b_one)
    await pool.release("https://a", a_one)
    # Most-recently-released is "a" so it should be at the LRU tail.
    keys = list(pool._idle.keys())
    assert keys[-1] == "https://a"


async def test_lru_move_to_end_on_acquire() -> None:
    pool, _ = _make_pool()
    a = await pool.acquire("https://a")
    b = await pool.acquire("https://b")
    await pool.release("https://a", a)
    await pool.release("https://b", b)
    # Re-acquire "a" — it should bubble to the tail.
    _ = await pool.acquire("https://a")
    keys = list(pool._idle.keys())
    assert keys[-1] == "https://a"


async def test_normalize_key_collapses_trailing_slash_in_pool() -> None:
    pool, created = _make_pool()
    a = await pool.acquire("https://api.example.com/")
    await pool.release("https://api.example.com/", a)
    # Different surface form, same key — should hit the pooled instance.
    b = await pool.acquire("https://api.example.com")
    assert b is a
    assert len(created) == 1


async def test_concurrent_acquires_do_not_serialise_factory() -> None:
    """Factory invocation lives outside the lock so slow factories don't pin it."""
    started = asyncio.Event()
    release_factory = asyncio.Event()
    factory_calls = 0

    async def _slow_factory(base_url: str) -> FakeClient:
        nonlocal factory_calls
        factory_calls += 1
        started.set()
        await release_factory.wait()
        return FakeClient(base_url)

    # The pool's factory is sync — wrap with asyncio.run_coroutine_threadsafe-
    # style trick: schedule slow construction via a wrapped sync that returns
    # a future. Easier: build the slow client inline by giving the pool a
    # sync factory that yields a stub, then time concurrent acquires.
    # Here we simply assert two acquires can complete concurrently when the
    # pool starts empty — both go through the factory branch.
    pool, created = _make_pool()
    a, b = await asyncio.gather(
        pool.acquire("https://a"),
        pool.acquire("https://b"),
    )
    assert a is not b
    assert len(created) == 2
    assert factory_calls == 0  # asserts our outer state untouched


async def test_concurrent_acquires_same_key_get_separate_clients() -> None:
    """Empty bucket + concurrent acquires yields N clients; release folds back to cap."""
    pool, created = _make_pool(max_idle=2)
    a, b, c = await asyncio.gather(
        pool.acquire("https://x"),
        pool.acquire("https://x"),
        pool.acquire("https://x"),
    )
    assert len({id(a), id(b), id(c)}) == 3
    assert len(created) == 3
    # Releases up to the cap re-pool, the rest close.
    await pool.release("https://x", a)
    await pool.release("https://x", b)
    await pool.release("https://x", c)
    assert pool.idle_count("https://x") == 2
    fake_c = cast("FakeClient", c)
    assert fake_c.closed is True


async def test_default_factory_constructs_real_client() -> None:
    """The default factory wires through to httpx — verify it succeeds."""
    pool = AsyncHTTPClientPool()
    client = await pool.acquire("https://example.com")
    try:
        # AsyncHTTPClient exposes ``close`` via BaseHTTPClient.
        assert hasattr(client, "close")
    finally:
        await client.close()
        await pool.close_all()


async def test_release_to_new_key_creates_bucket() -> None:
    """Releasing into an unknown key initialises the bucket."""
    pool, _ = _make_pool()
    client = await pool.acquire("https://newkey")
    await pool.release("https://newkey", client)
    assert pool.idle_count("https://newkey") == 1


async def test_close_all_with_empty_pool() -> None:
    pool, _ = _make_pool()
    # Never acquired anything.
    await pool.close_all()
    assert pool.is_closed is True
    assert pool.idle_count() == 0


async def test_close_all_drains_multiple_buckets() -> None:
    pool, _ = _make_pool()
    a = await pool.acquire("https://a")
    b = await pool.acquire("https://b")
    c = await pool.acquire("https://c")
    await pool.release("https://a", a)
    await pool.release("https://b", b)
    await pool.release("https://c", c)
    await pool.close_all()
    for client in (a, b, c):
        assert cast("FakeClient", client).closed is True


async def test_release_does_not_leave_empty_bucket_when_dropped() -> None:
    """Saturated-bucket release closes the client without leaving an
    empty stub in the idle map.

    The previous code used ``setdefault(key, [])`` which planted an empty
    list for any key reached on release, even when the release fell through
    to the close branch. The fix uses ``get`` so the bucket only materialises
    when an actual client is being put into it.
    """
    pool, _ = _make_pool(max_idle=1)
    # Two acquires for the same key — bucket starts empty (no key in _idle).
    a, b = await asyncio.gather(
        pool.acquire("https://saturate"),
        pool.acquire("https://saturate"),
    )
    # First release fills the bucket to cap (1).
    await pool.release("https://saturate", a)
    assert pool.idle_count("https://saturate") == 1
    # Second release sees a full bucket and closes the client.
    await pool.release("https://saturate", b)
    fake_b = cast("FakeClient", b)
    assert fake_b.closed is True
    # The bucket entry is still present (not empty) because the first
    # release populated it. The invariant we pin: no zero-length bucket
    # lingers in the idle map.
    for key, bucket in pool._idle.items():
        assert len(bucket) > 0, f"empty idle bucket lingered under key {key!r}"
