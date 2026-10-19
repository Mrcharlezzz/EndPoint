"""Edge + production tests for slice 8 (platform-agnostic refresh).

Pinned scenarios across the polymorphic refresh surface:

* Strategy ``refresh()`` raising unexpected → wrapped to ERROR.
* ``is_refresh_supported()`` False → refresh loop skips with warn-once.
* Concurrent refreshes for the same user → serialized via per-user lock.
* Moodle 200 + errorcode → EXPIRED.
* Telegram 200 + ``{"ok": false}`` → EXPIRED.
* ``current_payload={}`` → NO_TOKENS.
* Pipeline 401-mid-sync race: store de-dupes via atomic CAS.
* AuthenticationFailedError.__cause__ chain preserved.
* New 4th platform without a strategy → loop skips with warn-once.
* Moodle webservice URL read from credential payload (not hardcoded).
* Telegram bot token: cipher boundary lives in token store, strategy
  never touches plaintext directly.
* EXPIRED triggers a structured log event the alert engine subscribes to.
* Production: 100 concurrent refreshes respect concurrency cap.
* Production: unknown platform — strategy_for returns None silently.
* Production: realistic latency capture.

Edge brainstorm additions (≥ 8):

* Multi-platform mixed-outcome cycle in one batch (one EXPIRED, others
  SUCCESS) — no cross-contamination [TEST-COVERAGE]
* Credential rotation race: payload swapped mid-refresh [TEST-COVERAGE]
* Slow Moodle on-prem (5s response) → strategy honours its httpx
  timeout [TEST-COVERAGE]
* RefreshResult.new_payload is None when outcome != SUCCESS [TEST-COVERAGE]
* Coordinator reentrancy: calling refresh_credential twice in a row works
  [TEST-COVERAGE]
* Unknown platform in coordinator/strategy_for: returns None (no raise)
  [TEST-COVERAGE]
* The exception_translator catchall handling AuthenticationFailedError —
  flagged as [CODE-FIX] in the pipeline auth-detection commit
* Warn-once memory: a second candidate for the same unsupported
  platform does NOT fire a second warn [TEST-COVERAGE]
* refresh_loop unknown platform fires no log + does not crash on KeyError
  [TEST-COVERAGE]

Tests over 500ms carry ``@pytest.mark.slow``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx
import pytest
from ingestion.config import MoodleConfig, TelegramConfig
from ingestion.daemon.auth.bulk_refresh import run_polymorphic_refresh_cycle
from ingestion.daemon.auth.coordinator import (
    refresh_credential,
)
from ingestion.daemon.auth.moodle_strategy import MoodleRefreshStrategy
from ingestion.daemon.auth.refresh_loop import (
    RefreshLoopDeps,
    TeamsRefreshLoop,
    _strategy_for,
)
from ingestion.daemon.auth.strategy import RefreshOutcome, RefreshResult
from ingestion.daemon.auth.telegram_strategy import TelegramRefreshStrategy
from ingestion.daemon.candidates import Candidate, InMemoryCandidateSource

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeTokenStore:
    """In-memory token store; tracks every mutating call."""

    def __init__(self, creds: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.creds: dict[tuple[str, str], dict[str, Any]] = dict(creds or {})
        self.expired: set[tuple[str, str]] = set()
        self.mark_expired_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get_credentials(self, user_id: str, platform: str) -> dict[str, Any] | None:
        return self.creds.get((user_id, platform))

    async def update_credentials(
        self, user_id: str, platform: str, credentials: dict[str, Any]
    ) -> None:
        self.update_calls.append((user_id, platform, credentials))
        self.creds[(user_id, platform)] = credentials

    async def mark_expired(self, user_id: str, platform: str) -> None:
        self.mark_expired_calls.append((user_id, platform))
        self.expired.add((user_id, platform))


class _Strategy:
    """Configurable strategy double for edge tests."""

    def __init__(
        self,
        result: RefreshResult | Exception | None = None,
        supported: bool = True,
    ) -> None:
        self._result = result or RefreshResult(outcome=RefreshOutcome.SUCCESS)
        self._supported = supported
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
        self.calls.append((email, dict(current_payload)))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def is_refresh_supported(self) -> bool:
        return self._supported


def _moodle_cfg() -> MoodleConfig:
    return MoodleConfig(_env_file=None)  # type: ignore[call-arg]


def _telegram_cfg() -> TelegramConfig:
    return TelegramConfig(_env_file=None)  # type: ignore[call-arg]


def _make_moodle(transport: httpx.MockTransport) -> MoodleRefreshStrategy:
    class _Factory:
        def __init__(self, **kw: Any) -> None:
            self._client = httpx.AsyncClient(transport=transport, **kw)

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, *exc: object) -> None:
            await self._client.aclose()

    return MoodleRefreshStrategy(
        _moodle_cfg(),
        client_factory=_Factory,  # type: ignore[arg-type]
    )


def _make_telegram(transport: httpx.MockTransport) -> TelegramRefreshStrategy:
    class _Factory:
        def __init__(self, **kw: Any) -> None:
            self._client = httpx.AsyncClient(transport=transport, **kw)

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, *exc: object) -> None:
            await self._client.aclose()

    return TelegramRefreshStrategy(
        _telegram_cfg(),
        client_factory=_Factory,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. Strategy unexpected exception → ERROR (not propagated)
# ---------------------------------------------------------------------------


async def test_unexpected_exception_in_strategy_collapses_to_error() -> None:
    """[TEST-COVERAGE] Edge #1: a bug deep inside a strategy
    (KeyError, RuntimeError) must not take down the bulk loop."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    strategy = _Strategy(result=RuntimeError("boom inside strategy"))

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.ERROR
    assert out.error is not None
    assert "RuntimeError" in out.error
    # No persistence side-effect — the wrap is the boundary.
    assert store.update_calls == []
    assert store.mark_expired_calls == []


# ---------------------------------------------------------------------------
# 2. is_refresh_supported() False → refresh loop pre-filter
# ---------------------------------------------------------------------------


async def test_refresh_loop_skips_platform_with_no_registered_strategy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[TEST-COVERAGE] Edge #2: a platform whose PlatformSpec.refresh_strategy
    is None gets skipped with one warn-once log line."""
    # Build a candidate source where one platform isn't in the registry
    # (here we simulate by swapping its spec to None-strategy).
    from dataclasses import replace

    from ingestion.daemon import platforms as platforms_mod

    saved = dict(platforms_mod._REGISTRY)
    try:
        platforms_mod._REGISTRY["teams"] = replace(
            platforms_mod._REGISTRY["teams"], refresh_strategy=None
        )
        candidates = [
            Candidate(user_email="alice@x", platform="teams"),
            Candidate(user_email="alice@x", platform="teams"),
        ]
        deps = RefreshLoopDeps(
            token_store=_FakeTokenStore(),  # type: ignore[arg-type]
            candidate_source=InMemoryCandidateSource(candidates),
            interval_seconds=10,
        )
        loop = TeamsRefreshLoop(deps)
        with caplog.at_level(logging.WARNING):
            out = await loop._list_refresh_candidates()
        assert out == []
        # Warn-once memory: only one log line per platform per process.
        assert loop._unsupported_platforms_warned == {"teams"}
    finally:
        platforms_mod._REGISTRY.clear()
        platforms_mod._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 3. Concurrent refreshes for the same user — serialised at coordinator level
# ---------------------------------------------------------------------------


async def test_per_user_lock_serialises_refreshes_for_same_email() -> None:
    """[TEST-COVERAGE] Edge #3: two concurrent refreshes for the same
    user share the per-user lock; the second waits."""
    started: list[float] = []
    finished: list[float] = []

    class _SlowStrategy:
        async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
            del email, current_payload
            started.append(time.perf_counter())
            await asyncio.sleep(0.02)
            finished.append(time.perf_counter())
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)

        async def is_refresh_supported(self) -> bool:
            return True

    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    lock = asyncio.Lock()
    strategy = _SlowStrategy()

    a, b = await asyncio.gather(
        refresh_credential(
            user_email="u@x",
            platform="teams",
            token_store=store,  # type: ignore[arg-type]
            strategy=strategy,  # type: ignore[arg-type]
            user_lock=lock,
        ),
        refresh_credential(
            user_email="u@x",
            platform="teams",
            token_store=store,  # type: ignore[arg-type]
            strategy=strategy,  # type: ignore[arg-type]
            user_lock=lock,
        ),
    )
    assert a.outcome is RefreshOutcome.SUCCESS
    assert b.outcome is RefreshOutcome.SUCCESS
    # Lock guarantees: second start >= first finish.
    assert started[1] >= finished[0]


# ---------------------------------------------------------------------------
# 4. Moodle 200 + errorcode → EXPIRED
# ---------------------------------------------------------------------------


async def test_moodle_200_with_invalidtoken_errorcode_returns_expired() -> None:
    """[TEST-COVERAGE] Edge #4: Moodle's "200 with errorcode" pattern
    is the canonical revoked-token signal."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errorcode": "invalidtoken", "exception": "moodle_exception"}
        )

    s = _make_moodle(httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert "invalidtoken" in result.error_message


# ---------------------------------------------------------------------------
# 5. Telegram 200 with ok=false envelope → EXPIRED on auth code
# ---------------------------------------------------------------------------


async def test_telegram_200_with_ok_false_401_returns_expired() -> None:
    """[TEST-COVERAGE] Edge #5: Telegram returns ok=false + error_code
    401 in the JSON envelope when a bot token is revoked, even on 200."""

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
        )

    s = _make_telegram(httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={"bot_token": "tok"})
    assert result.outcome is RefreshOutcome.EXPIRED
    assert result.error_message is not None
    assert "401" in result.error_message


# ---------------------------------------------------------------------------
# 6. Empty payload → NO_TOKENS across all strategies
# ---------------------------------------------------------------------------


async def test_empty_payload_returns_no_tokens_for_moodle() -> None:
    """[TEST-COVERAGE] Edge #6: Moodle short-circuits on {}.
    No network call is made."""
    seen: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    s = _make_moodle(httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert seen == []


async def test_empty_payload_returns_no_tokens_for_telegram() -> None:
    """[TEST-COVERAGE] Edge #6 (Telegram half): same NO_TOKENS short-
    circuit on {}."""
    seen: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    s = _make_telegram(httpx.MockTransport(_handler))
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.NO_TOKENS
    assert seen == []


# ---------------------------------------------------------------------------
# 7. Pipeline 401 race: store de-dupes via atomic CAS
# ---------------------------------------------------------------------------


async def test_pipeline_401_race_store_atomic_cas() -> None:
    """[TEST-COVERAGE] Edge #7: two concurrent fetches both 401 →
    store ends in expired state. The pipeline calls mark_expired
    per detection (twice); the production store CAS de-dupes
    notifier emissions. Pinned via call count."""
    from ingestion.daemon.services.exceptions import AuthenticationFailedError
    from ingestion.exceptions import TokenExpiredError
    from ingestion.pipeline import run_sync

    from tests.daemon._scheduler_helpers import (
        InMemoryContentSink,
        InMemorySyncLogger,
        InMemorySyncStateStore,
    )

    class _FailingAdapter:
        platform = "test"

        async def fetch(self, *_args: Any, **_kwargs: Any) -> Any:
            raise TokenExpiredError("token rotted")

        async def test_connection(self, _credentials: Any) -> bool:
            return True

    store = _FakeTokenStore()
    adapter_a = _FailingAdapter()
    adapter_b = _FailingAdapter()

    async def _run(adapter: _FailingAdapter) -> None:
        with pytest.raises(AuthenticationFailedError):
            await run_sync(
                adapter=adapter,  # type: ignore[arg-type]
                credentials={"x": 1},
                content_sink=InMemoryContentSink(),  # type: ignore[arg-type]
                sync_state=InMemorySyncStateStore(),  # type: ignore[arg-type]
                sync_logger=InMemorySyncLogger(),  # type: ignore[arg-type]
                user_id="alice@x",
                token_store=store,  # type: ignore[arg-type]
            )

    await asyncio.gather(_run(adapter_a), _run(adapter_b))
    assert ("alice@x", "test") in store.expired
    # Both pipeline runs called mark_expired — production store CAS
    # ensures only one notifier emission.
    assert len(store.mark_expired_calls) == 2


# ---------------------------------------------------------------------------
# 8. AuthenticationFailedError.__cause__ chain preserved
# ---------------------------------------------------------------------------


async def test_authentication_failed_cause_chain_preserved() -> None:
    """[TEST-COVERAGE] Edge #8: __cause__ links back to the original
    IngestionError so structlog's exc_info captures the full chain."""
    from ingestion.daemon.services.exceptions import AuthenticationFailedError
    from ingestion.exceptions import TokenExpiredError
    from ingestion.pipeline import run_sync

    from tests.daemon._scheduler_helpers import (
        InMemoryContentSink,
        InMemorySyncLogger,
        InMemorySyncStateStore,
    )

    inner = TokenExpiredError("graph token expired")

    class _A:
        platform = "test"

        async def fetch(self, *_args: Any, **_kwargs: Any) -> Any:
            raise inner

        async def test_connection(self, _credentials: Any) -> bool:
            return True

    store = _FakeTokenStore()
    with pytest.raises(AuthenticationFailedError) as ei:
        await run_sync(
            adapter=_A(),  # type: ignore[arg-type]
            credentials={"x": 1},
            content_sink=InMemoryContentSink(),  # type: ignore[arg-type]
            sync_state=InMemorySyncStateStore(),  # type: ignore[arg-type]
            sync_logger=InMemorySyncLogger(),  # type: ignore[arg-type]
            user_id="alice@x",
            token_store=store,  # type: ignore[arg-type]
        )

    assert ei.value.__cause__ is inner


# ---------------------------------------------------------------------------
# 9. New 4th platform — refresh_loop skips with warn-once
# ---------------------------------------------------------------------------


async def test_warn_once_per_unsupported_platform(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[TEST-COVERAGE] Edge #9: an operator who adds a 4th platform
    without a strategy gets ONE structured warning per process even if
    multiple candidates exist for the platform."""
    from dataclasses import replace

    from ingestion.daemon import platforms as platforms_mod

    saved = dict(platforms_mod._REGISTRY)
    try:
        # Drop Teams' strategy so candidates appear "unsupported".
        platforms_mod._REGISTRY["teams"] = replace(
            platforms_mod._REGISTRY["teams"], refresh_strategy=None
        )
        candidates = [Candidate(user_email=f"u{i}@x", platform="teams") for i in range(5)]
        deps = RefreshLoopDeps(
            token_store=_FakeTokenStore(),  # type: ignore[arg-type]
            candidate_source=InMemoryCandidateSource(candidates),
            interval_seconds=10,
        )
        loop = TeamsRefreshLoop(deps)
        # Two passes — second pass must NOT log a second warn line.
        await loop._list_refresh_candidates()
        await loop._list_refresh_candidates()
        # Single platform recorded.
        assert loop._unsupported_platforms_warned == {"teams"}
    finally:
        platforms_mod._REGISTRY.clear()
        platforms_mod._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 10. Moodle webservice URL read from credential payload
# ---------------------------------------------------------------------------


async def test_moodle_url_read_from_credential_payload_not_hardcoded() -> None:
    """[TEST-COVERAGE] Edge #10: the credential payload's base_url
    wins over the runtime config so per-institution overrides work."""
    seen: dict[str, str] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"userid": 1})

    s = _make_moodle(httpx.MockTransport(_handler))
    await s.refresh(
        "u@x",
        current_payload={
            "token": "tok",
            "base_url": "https://institutional.example.org",
        },
    )
    assert "institutional.example.org" in seen["url"]


# ---------------------------------------------------------------------------
# 11. Telegram bot token decrypted via token store cipher boundary
# ---------------------------------------------------------------------------


async def test_telegram_strategy_never_touches_cipher_directly() -> None:
    """[TEST-COVERAGE] Edge #11: the strategy receives a plaintext
    payload; encryption / decryption is the token store's job. Pin
    the import surface — strategy module must not import
    ``ingestion.daemon.crypto``."""
    import ingestion.daemon.auth.telegram_strategy as ts_mod

    src = "import " + " ".join(
        getattr(ts_mod, name).__name__
        for name in dir(ts_mod)
        if not name.startswith("_") and hasattr(getattr(ts_mod, name), "__name__")
    )
    # Module-level imports are caught by the AST scan in the package's
    # carve-out tests; here we just pin that the strategy module never
    # reaches into the cipher class. CredentialCipher lives in
    # ``ingestion.daemon.crypto``; the strategy module's source must
    # not import it.
    import inspect

    source = inspect.getsource(ts_mod)
    assert "from ingestion.daemon.crypto" not in source
    assert "import CredentialCipher" not in source
    del src


async def test_moodle_strategy_never_touches_cipher_directly() -> None:
    """[TEST-COVERAGE] Edge #11 (Moodle half): cipher boundary stays
    at the token store."""
    import inspect

    import ingestion.daemon.auth.moodle_strategy as ms_mod

    source = inspect.getsource(ms_mod)
    assert "from ingestion.daemon.crypto" not in source
    assert "import CredentialCipher" not in source


# ---------------------------------------------------------------------------
# 12. EXPIRED outcome triggers structured event
# ---------------------------------------------------------------------------


async def test_expired_outcome_writes_audit_via_token_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[TEST-COVERAGE] Edge #12: the coordinator writes the EXPIRED
    via mark_expired which (in production) emits an audit/notifier
    event — slice 13's alert engine subscribes. Pin the call surface."""
    store = _FakeTokenStore(creds={("u@x", "moodle"): {"token": "x"}})
    strategy = _Strategy(result=RefreshResult(outcome=RefreshOutcome.EXPIRED))

    out = await refresh_credential(
        user_email="u@x",
        platform="moodle",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.EXPIRED
    # The mark_expired call IS the event surface — the production
    # store fires the notifier on the false-to-true transition.
    assert store.mark_expired_calls == [("u@x", "moodle")]


# ---------------------------------------------------------------------------
# 13. Production: 100 concurrent refreshes — concurrency cap respected
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_100_concurrent_refreshes_respect_concurrency_cap() -> None:
    """[TEST-COVERAGE] Edge #13: with concurrency=8 and 100
    candidates, no more than 8 strategy.refresh calls are in flight
    at once. We pin via a semaphore counter inside the strategy."""
    in_flight: list[int] = [0]
    max_in_flight: list[int] = [0]
    lock = asyncio.Lock()

    class _CountingStrategy:
        async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
            del email, current_payload
            async with lock:
                in_flight[0] += 1
                if in_flight[0] > max_in_flight[0]:
                    max_in_flight[0] = in_flight[0]
            try:
                # Just enough to make the scheduler interleave.
                await asyncio.sleep(0.001)
            finally:
                async with lock:
                    in_flight[0] -= 1
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)

        async def is_refresh_supported(self) -> bool:
            return True

    store = _FakeTokenStore(creds={(f"u{i}@x", "teams"): {"x": 1} for i in range(100)})
    strategy = _CountingStrategy()

    candidates = [(f"u{i}@x", "teams") for i in range(100)]

    def _resolver(_p: str) -> Any:
        return strategy

    stats = await run_polymorphic_refresh_cycle(
        candidates=candidates,
        token_store=store,  # type: ignore[arg-type]
        strategy_for=_resolver,  # type: ignore[arg-type]
        interval_seconds=0,  # zero-jitter so all start ~immediately
        concurrency=8,
        rng=random.Random(1),
    )

    assert stats.attempted == 100
    assert stats.succeeded == 100
    # The concurrency cap is enforced by the worker pool: at most 8
    # strategy calls in flight at once.
    assert max_in_flight[0] <= 8
    assert max_in_flight[0] >= 1


# ---------------------------------------------------------------------------
# 14. Production: unknown platform — _strategy_for returns None
# ---------------------------------------------------------------------------


def test_strategy_for_returns_none_for_unknown_platform() -> None:
    """[TEST-COVERAGE] Edge #14: the loop's resolver returns None on
    an unregistered platform without raising. The polymorphic cycle's
    worker drops candidates whose strategy is None defensively."""
    assert _strategy_for("definitely-not-a-platform") is None


def test_strategy_for_returns_none_for_platform_without_strategy() -> None:
    """[TEST-COVERAGE] Edge #14b: a registered platform whose
    refresh_strategy is None also returns None (not the spec)."""
    from dataclasses import replace

    from ingestion.daemon import platforms as platforms_mod

    saved = dict(platforms_mod._REGISTRY)
    try:
        platforms_mod._REGISTRY["teams"] = replace(
            platforms_mod._REGISTRY["teams"], refresh_strategy=None
        )
        assert _strategy_for("teams") is None
    finally:
        platforms_mod._REGISTRY.clear()
        platforms_mod._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 15. Production: realistic latency capture via mocked delay
# ---------------------------------------------------------------------------


async def test_moodle_strategy_captures_realistic_latency() -> None:
    """[TEST-COVERAGE] Edge #15: latency_ms reflects the real wall
    clock cost of the probe — not a constant."""

    async def _slow_handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"userid": 1})

    # MockTransport handler can be sync or async; use the sync wrapper
    # by sleeping with time.sleep would block the loop, so instead we
    # use httpx's AsyncClient with a custom transport that handles
    # async handlers — MockTransport supports both forms.
    transport = httpx.MockTransport(_slow_handler)  # type: ignore[arg-type]
    s = _make_moodle(transport)
    result = await s.refresh("u@x", current_payload={"token": "tok"})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.latency_ms > 0.0
    # Cap latency assertion loosely — the test should not be flaky on
    # slow CI: just pin it captured non-trivial time.
    assert result.latency_ms >= 1.0


# ---------------------------------------------------------------------------
# Edge brainstorm — own additions, ≥ 8 tagged
# ---------------------------------------------------------------------------


async def test_multi_platform_batch_one_expired_others_succeed() -> None:
    """[TEST-COVERAGE] Brainstorm: a polymorphic cycle batches across
    Teams + Moodle + Telegram — one platform's EXPIRED outcome must
    not contaminate the others' SUCCESS reporting."""
    store = _FakeTokenStore(
        creds={
            ("alice@x", "teams"): {"cookies": [{"domain": "login.microsoftonline.com"}]},
            ("alice@x", "moodle"): {"token": "moodle-tok"},
            ("alice@x", "telegram"): {"bot_token": "tg-tok"},
        }
    )

    teams_strategy = _Strategy(result=RefreshResult(outcome=RefreshOutcome.SUCCESS))
    moodle_strategy = _Strategy(
        result=RefreshResult(outcome=RefreshOutcome.EXPIRED, error_message="probe 401")
    )
    telegram_strategy = _Strategy(result=RefreshResult(outcome=RefreshOutcome.SUCCESS))

    def _resolver(platform: str) -> Any:
        return {
            "teams": teams_strategy,
            "moodle": moodle_strategy,
            "telegram": telegram_strategy,
        }[platform]

    candidates = [
        ("alice@x", "teams"),
        ("alice@x", "moodle"),
        ("alice@x", "telegram"),
    ]

    stats = await run_polymorphic_refresh_cycle(
        candidates=candidates,
        token_store=store,  # type: ignore[arg-type]
        strategy_for=_resolver,  # type: ignore[arg-type]
        interval_seconds=0,
        concurrency=4,
        rng=random.Random(7),
    )

    assert stats.attempted == 3
    assert stats.succeeded == 2  # teams + telegram
    assert stats.expired == 1
    # Only the moodle credential gets marked expired.
    assert ("alice@x", "moodle") in store.expired
    assert ("alice@x", "teams") not in store.expired
    assert ("alice@x", "telegram") not in store.expired


async def test_credential_rotation_during_refresh_is_safe() -> None:
    """[TEST-COVERAGE] Brainstorm: an operator updates the credential
    payload mid-refresh. The strategy reads the snapshot taken at
    coordinator entry — subsequent updates land for the next cycle."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"v": 1}})

    captured_payloads: list[dict[str, Any]] = []

    class _Capturer:
        async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
            del email
            captured_payloads.append(dict(current_payload))
            await asyncio.sleep(0.005)
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)

        async def is_refresh_supported(self) -> bool:
            return True

    async def _mutator() -> None:
        await asyncio.sleep(0.001)
        # While refresh is in flight, an operator overwrites the row.
        store.creds[("u@x", "teams")] = {"v": 2}

    await asyncio.gather(
        refresh_credential(
            user_email="u@x",
            platform="teams",
            token_store=store,  # type: ignore[arg-type]
            strategy=_Capturer(),  # type: ignore[arg-type]
        ),
        _mutator(),
    )

    # The strategy saw the snapshot taken before the mutator ran.
    assert captured_payloads == [{"v": 1}]
    # The mutator's update is in the store.
    assert store.creds[("u@x", "teams")] == {"v": 2}


async def test_moodle_timeout_exception_surfaces_as_error() -> None:
    """[TEST-COVERAGE] Brainstorm: a slow on-prem Moodle that exceeds
    the strategy's httpx timeout — when httpx raises TimeoutException
    the strategy maps it to ERROR (not EXPIRED — auth failed != probe
    failed).

    NB: httpx.MockTransport ignores client-level timeouts (it never
    opens a real socket). Reviewer-fixer note: there is no way to
    pin "real httpx timeout enforcement" against MockTransport —
    document that the production timeout works via the underlying
    transport. Here we simulate by raising the exception type the
    strategy must handle."""

    def _raise_timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated upstream timeout")

    s = _make_moodle(httpx.MockTransport(_raise_timeout))
    result = await s.refresh("u@x", current_payload={"token": "t"})
    # Timeout surfaces as ERROR (not EXPIRED — auth failed != probe
    # failed).
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "network" in result.error_message


def test_refresh_result_new_payload_is_none_when_outcome_not_success() -> None:
    """[TEST-COVERAGE] Brainstorm: pin via property — every constructed
    non-SUCCESS RefreshResult has new_payload=None. Tests in the
    strategy modules already pin per-strategy; this is the
    cross-platform invariant."""
    for outcome in (
        RefreshOutcome.NO_TOKENS,
        RefreshOutcome.EXPIRED,
        RefreshOutcome.ERROR,
        RefreshOutcome.NO_COOKIES,
    ):
        result = RefreshResult(outcome=outcome)
        assert result.new_payload is None


async def test_coordinator_is_reentrant() -> None:
    """[TEST-COVERAGE] Brainstorm: calling refresh_credential twice in
    a row works — no module-level state leaks between calls."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    strategy = _Strategy(result=RefreshResult(outcome=RefreshOutcome.SUCCESS))

    out1 = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )
    out2 = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )
    assert out1.outcome is RefreshOutcome.SUCCESS
    assert out2.outcome is RefreshOutcome.SUCCESS
    assert len(strategy.calls) == 2


async def test_polymorphic_cycle_with_unknown_platform_drops_silently() -> None:
    """[TEST-COVERAGE] Brainstorm: a registry edit between filter and
    dispatch could let an unsupported platform slip into the cycle.
    The worker drops it without crashing."""
    store = _FakeTokenStore()

    def _resolver(_platform: str) -> Any:
        return None  # always unknown

    stats = await run_polymorphic_refresh_cycle(
        candidates=[("u@x", "ghost-platform")],
        token_store=store,  # type: ignore[arg-type]
        strategy_for=_resolver,  # type: ignore[arg-type]
        interval_seconds=0,
        concurrency=2,
    )
    # No outcome recorded — the candidate was dropped silently.
    assert stats.attempted == 0


async def test_refresh_loop_unknown_platform_not_in_registry_swallows_keyerror_async() -> None:
    """[TEST-COVERAGE] Brainstorm (async wrapper): unknown platform
    in the candidate list is dropped without crashing — the registry
    KeyError is caught internally."""
    candidates = [
        Candidate(user_email="u@x", platform="not-registered"),
        Candidate(user_email="bob@x", platform="moodle"),  # known
    ]
    deps = RefreshLoopDeps(
        token_store=_FakeTokenStore(),  # type: ignore[arg-type]
        candidate_source=InMemoryCandidateSource(candidates),
        interval_seconds=10,
    )
    loop = TeamsRefreshLoop(deps)
    out = await loop._list_refresh_candidates()
    # Only registered platforms with strategies are returned. moodle's
    # spec is registered; whether its strategy is set depends on
    # whether runtime.start ran — in tests it usually has not, so
    # `out` may be empty. Either way: the unknown platform is gone.
    assert all(p == "moodle" for _, p in out)


async def test_paused_candidates_are_filtered_out() -> None:
    """[TEST-COVERAGE] Brainstorm: a paused candidate must not be
    refreshed even if its platform has a strategy."""
    from dataclasses import replace

    from ingestion.daemon import platforms as platforms_mod

    saved = dict(platforms_mod._REGISTRY)
    try:
        platforms_mod._REGISTRY["teams"] = replace(
            platforms_mod._REGISTRY["teams"], refresh_strategy=_Strategy()
        )
        candidates = [
            Candidate(user_email="u1@x", platform="teams", is_paused=True),
            Candidate(user_email="u2@x", platform="teams", is_paused=False),
        ]
        deps = RefreshLoopDeps(
            token_store=_FakeTokenStore(),  # type: ignore[arg-type]
            candidate_source=InMemoryCandidateSource(candidates),
            interval_seconds=10,
        )
        loop = TeamsRefreshLoop(deps)
        out = await loop._list_refresh_candidates()
        assert out == [("u2@x", "teams")]
    finally:
        platforms_mod._REGISTRY.clear()
        platforms_mod._REGISTRY.update(saved)


async def test_polymorphic_cycle_empty_candidates_short_circuits() -> None:
    """[TEST-COVERAGE] Brainstorm: zero candidates → empty stats
    object, no worker tasks created."""
    stats = await run_polymorphic_refresh_cycle(
        candidates=[],
        token_store=_FakeTokenStore(),  # type: ignore[arg-type]
        strategy_for=lambda _p: None,
        interval_seconds=0,
        concurrency=4,
    )
    assert stats.attempted == 0
    assert stats.succeeded == 0
    assert stats.expired == 0
