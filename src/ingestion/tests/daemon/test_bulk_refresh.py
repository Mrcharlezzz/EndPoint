"""Bulk refresh: jitter, per-user outcomes, aggregation."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import jwt
from ingestion.daemon.auth.bulk_refresh import (
    BulkRefreshStats,
    plan_jittered_starts,
    run_bulk_refresh_cycle,
)
from ingestion.daemon.auth.refresh import (
    REQUIRED_GRAPH_SCOPE,
    CapturedTokens,
    RefreshOutcome,
    TokenRefresher,
    refresh_one,
)

from tests.daemon._scheduler_helpers import InMemoryTokenStore


def _scoped_jwt(scope: str = REQUIRED_GRAPH_SCOPE, *, exp_in: int = 3600) -> str:
    """Mint an unsigned JWT carrying ``scope`` with an exp claim.

    refresh_one's "fresh enough to write back" check uses
    ``has_scope`` + ``get_expiry_timestamp``; both parse the JWT payload
    without signature verification.
    """
    payload = {"scp": scope, "exp": int(time.time()) + exp_in}
    return jwt.encode(payload, "test-secret", algorithm="HS256")


@dataclass
class FakeRefresher:
    """Test double. Decides what to return per-call so failure paths exercise."""

    outputs: list[CapturedTokens]
    calls: list[tuple[list[dict[str, Any]], str, int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def capture_tokens(
        self,
        *,
        cookies: list[dict[str, Any]],
        refresh_url: str,
        timeout_seconds: int,
        refresh_token: str | None = None,
        user_email: str | None = None,
    ) -> CapturedTokens:
        # ``refresh_token`` and ``user_email`` are part of the protocol added
        # when the daemon switched to silent-OIDC. Tests don't yet exercise
        # them; capture for completeness if a future test wants to assert.
        del refresh_token, user_email
        self.calls.append((cookies, refresh_url, timeout_seconds))
        if not self.outputs:
            return CapturedTokens()
        return self.outputs.pop(0)


def _sso_cookies() -> list[dict[str, Any]]:
    return [
        {"domain": "login.microsoftonline.com", "name": "ESTSAUTH", "value": "x"},
        {"domain": "not-sso.example.com", "name": "junk", "value": "y"},
    ]


# --- plan_jittered_starts ---


def test_plan_jittered_starts_returns_empty_for_zero_count() -> None:
    assert plan_jittered_starts(0, interval_seconds=300.0) == []


def test_plan_jittered_starts_zero_interval_returns_zeros() -> None:
    assert plan_jittered_starts(5, interval_seconds=0.0) == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_plan_jittered_starts_are_within_window() -> None:
    rng = random.Random(42)
    offsets = plan_jittered_starts(20, interval_seconds=300.0, rng=rng)
    assert len(offsets) == 20
    assert all(0.0 <= o < 300.0 for o in offsets)


def test_plan_jittered_starts_spread_across_window_not_clustered() -> None:
    rng = random.Random(123)
    offsets = plan_jittered_starts(10, interval_seconds=100.0, rng=rng)
    # Each slot is 10s wide; slot i sits in [10i, 10(i+1)). The max offset
    # should be > half the window and the min < half — proves slots are used.
    assert min(offsets) < 50.0
    assert max(offsets) >= 50.0


def test_plan_jittered_starts_deterministic_with_seed() -> None:
    a = plan_jittered_starts(10, interval_seconds=300.0, rng=random.Random(7))
    b = plan_jittered_starts(10, interval_seconds=300.0, rng=random.Random(7))
    assert a == b


# --- refresh_one ---


async def test_refresh_one_success_merges_and_writes_back() -> None:
    old_graph = _scoped_jwt(exp_in=60)  # older expiry
    new_graph = _scoped_jwt(exp_in=3600)  # newer expiry
    fresh_cookie = {
        "domain": ".login.microsoftonline.com",
        "name": "ESTSAUTH",
        "value": "fresh",
    }
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {
                "graph_token": old_graph,
                "csa_token": "old-csa",
                "team_ids": ["t1", "t2"],
                "refresh_url": "https://teams.microsoft.com/channel/X",
                "cookies": _sso_cookies(),
            },
        }
    )
    refresher = FakeRefresher(
        outputs=[
            CapturedTokens(graph_token=new_graph, csa_token="new-csa", cookies=(fresh_cookie,))
        ]
    )

    result = await refresh_one(
        user_email="alice@x",
        token_store=store,
        refresher=refresher,
    )

    assert result.outcome is RefreshOutcome.SUCCESS
    merged = await store.get_credentials("alice@x", "teams")
    assert merged is not None
    assert merged["graph_token"] == new_graph
    assert merged["csa_token"] == "new-csa"
    assert merged["team_ids"] == ["t1", "t2"]  # preserved
    assert merged["refresh_url"] == "https://teams.microsoft.com/channel/X"
    # Cookies merge by (domain, name); the non-SSO junk cookie from _sso_cookies
    # stays, the ESTSAUTH entry is overwritten by the fresh capture.
    by_key = {(c["domain"], c["name"]): c for c in merged["cookies"]}
    assert by_key[(".login.microsoftonline.com", "ESTSAUTH")]["value"] == "fresh"
    assert ("not-sso.example.com", "junk") in by_key


async def test_refresh_one_persists_refresh_token_alongside_graph() -> None:
    """A captured refresh_token survives the merge + round-trip through the store.

    HttpxTokenRefresher returns CapturedTokens with refresh_token set (the
    rolled value from the OAuth2 grant). _merge_creds must write it
    through, and the next get_credentials must return it for the next
    refresh cycle to pick up.
    """
    new_graph = _scoped_jwt(exp_in=3600)
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {
                "graph_token": _scoped_jwt(exp_in=60),
                "team_ids": ["t1"],
                "cookies": _sso_cookies(),
                # No refresh_token in the existing blob; this is the first
                # cycle that captures one.
            },
        }
    )
    refresher = FakeRefresher(
        outputs=[
            CapturedTokens(
                graph_token=new_graph,
                refresh_token="rolled-rt-1",
            )
        ]
    )

    result = await refresh_one(
        user_email="alice@x",
        token_store=store,
        refresher=refresher,
    )

    assert result.outcome is RefreshOutcome.SUCCESS
    merged = await store.get_credentials("alice@x", "teams")
    assert merged is not None
    assert merged["graph_token"] == new_graph
    assert merged["refresh_token"] == "rolled-rt-1"
    # Existing fields preserved.
    assert merged["team_ids"] == ["t1"]


async def test_refresh_one_passes_stored_refresh_token_to_driver() -> None:
    """_refresh_one_inner forwards the previously persisted refresh_token.

    Without this, the daemon would always do the cookie-bootstrap path
    even when a refresh_token is available.
    """
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {
                "graph_token": _scoped_jwt(exp_in=60),
                "refresh_token": "saved-rt",
                "cookies": _sso_cookies(),
            },
        }
    )

    seen_refresh_token: list[str | None] = []

    @dataclass
    class CapturingRefresher:
        async def capture_tokens(
            self,
            *,
            cookies: list[dict[str, Any]],
            refresh_url: str,
            timeout_seconds: int,
            refresh_token: str | None = None,
            user_email: str | None = None,
        ) -> CapturedTokens:
            del cookies, refresh_url, timeout_seconds, user_email
            seen_refresh_token.append(refresh_token)
            return CapturedTokens(
                graph_token=_scoped_jwt(exp_in=3600),
                refresh_token="rolled-rt",
            )

    await refresh_one(
        user_email="alice@x",
        token_store=store,
        refresher=CapturingRefresher(),
    )

    assert seen_refresh_token == ["saved-rt"]


async def test_refresh_one_no_tokens_captured_keeps_creds_unchanged() -> None:
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {
                "graph_token": "old-graph",
                "cookies": _sso_cookies(),
            },
        }
    )
    refresher = FakeRefresher(outputs=[CapturedTokens()])

    result = await refresh_one(user_email="alice@x", token_store=store, refresher=refresher)

    assert result.outcome is RefreshOutcome.NO_TOKENS
    # Original token untouched.
    creds = await store.get_credentials("alice@x", "teams")
    assert creds is not None
    assert creds["graph_token"] == "old-graph"


async def test_refresh_one_no_credentials_row_is_handled() -> None:
    store = InMemoryTokenStore()
    refresher = FakeRefresher(outputs=[])

    result = await refresh_one(user_email="ghost@x", token_store=store, refresher=refresher)

    assert result.outcome is RefreshOutcome.NO_COOKIES
    assert refresher.calls == []  # did not invoke the driver


async def test_refresh_one_no_sso_cookies_short_circuits() -> None:
    # User has cookies but none from an SSO domain.
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {
                "cookies": [{"domain": "other.example.com", "name": "x"}],
            },
        }
    )
    refresher = FakeRefresher(outputs=[])

    result = await refresh_one(user_email="alice@x", token_store=store, refresher=refresher)

    assert result.outcome is RefreshOutcome.NO_COOKIES
    assert refresher.calls == []


async def test_refresh_one_driver_exception_is_returned_not_raised() -> None:
    store = InMemoryTokenStore(
        {("alice@x", "teams"): {"cookies": _sso_cookies()}},
    )

    class BoomRefresher:
        async def capture_tokens(
            self,
            *,
            cookies: list[dict[str, Any]],
            refresh_url: str,
            timeout_seconds: int,
            refresh_token: str | None = None,
            user_email: str | None = None,
        ) -> CapturedTokens:
            raise RuntimeError("refresher exploded")

    result = await refresh_one(user_email="alice@x", token_store=store, refresher=BoomRefresher())
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error is not None and "exploded" in result.error


async def test_refresh_one_passes_only_sso_cookies_to_driver() -> None:
    store = InMemoryTokenStore(
        {("alice@x", "teams"): {"cookies": _sso_cookies()}},
    )
    refresher = FakeRefresher(outputs=[CapturedTokens(graph_token="g")])

    await refresh_one(user_email="alice@x", token_store=store, refresher=refresher)

    assert len(refresher.calls) == 1
    sent_cookies, _url, _timeout = refresher.calls[0]
    assert all(c["domain"].endswith("microsoftonline.com") for c in sent_cookies)


# --- run_bulk_refresh_cycle ---


async def _noop_sleep(_seconds: float) -> None:
    return None


async def test_bulk_cycle_empty_user_list() -> None:
    store = InMemoryTokenStore()
    refresher = FakeRefresher(outputs=[])
    stats = await run_bulk_refresh_cycle(
        user_emails=[],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        sleep=_noop_sleep,
    )
    assert stats == BulkRefreshStats()


async def test_bulk_cycle_mixes_success_and_failure() -> None:
    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {"cookies": _sso_cookies(), "graph_token": "g-a"},
            ("bob@x", "teams"): {"cookies": _sso_cookies(), "graph_token": "g-b"},
            ("carol@x", "teams"): {"cookies": []},
        }
    )
    refresher = FakeRefresher(
        outputs=[
            CapturedTokens(graph_token="new-g-a", csa_token="new-c-a"),
            CapturedTokens(),  # bob: driver returns nothing
        ]
    )

    stats = await run_bulk_refresh_cycle(
        user_emails=["alice@x", "bob@x", "carol@x"],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        sleep=_noop_sleep,
    )

    assert stats.attempted == 3
    assert stats.succeeded == 1  # alice
    assert stats.no_tokens == 1  # bob
    assert stats.no_cookies == 1  # carol
    assert stats.errored == 0

    alice_creds = await store.get_credentials("alice@x", "teams")
    assert alice_creds is not None
    assert alice_creds["graph_token"] == "new-g-a"
    # Bob's original token should be untouched because refresh failed.
    bob_creds = await store.get_credentials("bob@x", "teams")
    assert bob_creds is not None
    assert bob_creds["graph_token"] == "g-b"


async def test_bulk_cycle_uses_sleep_for_jittered_offsets() -> None:
    store = InMemoryTokenStore(
        {("alice@x", "teams"): {"cookies": _sso_cookies()}},
    )
    refresher = FakeRefresher(outputs=[CapturedTokens(graph_token="g")])
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    await run_bulk_refresh_cycle(
        user_emails=["alice@x"],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        rng=random.Random(1),
        sleep=record_sleep,
    )

    # Exactly one user means one slot covering [0, 60). Jitter lands somewhere
    # inside. If the offset was strictly positive, sleep fired once.
    assert len(slept) <= 1  # tolerant: 0 if the RNG happened to pick 0
    for s in slept:
        assert 0.0 <= s < 60.0


# Ensure the Protocol import doesn't go unused.
_ = TokenRefresher


# --- sync_logger piggyback (sync_history rows for credential refreshes) ---


async def test_bulk_cycle_emits_sync_history_rows_per_user(tmp_path: Any) -> None:
    """Each user in the cycle gets one sync_history row with
    sync_kind='credential_refresh'; success closes with 'complete', the rest
    close with 'failed'.

    concurrency=1 pins the worker order so the FakeRefresher's FIFO output
    queue lines up with the user list; under the default concurrency=8 the
    capture calls race and alice could pop bob's scripted tokens.
    """
    import sqlite3

    from ingestion.daemon.ports.sync_logger import (
        KIND_CREDENTIAL_REFRESH,
        SqliteSyncLogger,
    )
    from ingestion.daemon.store.local import apply_migrations

    db_path = tmp_path / "state.db"
    apply_migrations(db_path)
    sync_logger = SqliteSyncLogger(db_path)

    store = InMemoryTokenStore(
        {
            ("alice@x", "teams"): {"cookies": _sso_cookies(), "graph_token": "g-a"},
            ("bob@x", "teams"): {"cookies": _sso_cookies(), "graph_token": "g-b"},
            ("carol@x", "teams"): {"cookies": []},
        }
    )
    refresher = FakeRefresher(
        outputs=[
            CapturedTokens(graph_token=_scoped_jwt(), csa_token="new-c-a"),
            CapturedTokens(),  # bob: no tokens
        ]
    )

    await run_bulk_refresh_cycle(
        user_emails=["alice@x", "bob@x", "carol@x"],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        sleep=_noop_sleep,
        sync_logger=sync_logger,
        concurrency=1,
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            row["user_email"]: dict(row)
            for row in conn.execute(
                "SELECT user_email, platform, state, sync_kind, error FROM sync_history"
            )
        }
    finally:
        conn.close()

    assert set(rows) == {"alice@x", "bob@x", "carol@x"}
    for row in rows.values():
        assert row["sync_kind"] == KIND_CREDENTIAL_REFRESH
        assert row["platform"] == "teams"

    assert rows["alice@x"]["state"] == "complete"
    assert rows["alice@x"]["error"] is None
    assert rows["bob@x"]["state"] == "failed"
    # Error prefix encodes the RefreshOutcome so the UI can render it.
    assert rows["bob@x"]["error"] is not None
    assert rows["bob@x"]["error"].startswith("no_tokens")
    assert rows["carol@x"]["state"] == "failed"
    assert rows["carol@x"]["error"] is not None
    assert rows["carol@x"]["error"].startswith("no_cookies")


async def test_bulk_cycle_without_sync_logger_is_a_no_op(tmp_path: Any) -> None:
    """When sync_logger is None, no sync_history rows are written and the
    cycle still reports the normal BulkRefreshStats."""
    import sqlite3

    from ingestion.daemon.store.local import apply_migrations

    db_path = tmp_path / "state.db"
    apply_migrations(db_path)

    store = InMemoryTokenStore(
        {("alice@x", "teams"): {"cookies": _sso_cookies()}},
    )
    refresher = FakeRefresher(outputs=[CapturedTokens(graph_token=_scoped_jwt())])

    stats = await run_bulk_refresh_cycle(
        user_emails=["alice@x"],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        sleep=_noop_sleep,
        # sync_logger omitted on purpose.
    )

    assert stats.attempted == 1
    assert stats.succeeded == 1
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM sync_history").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


async def test_bulk_cycle_sync_logger_start_failure_does_not_abort_refresh(
    tmp_path: Any,
) -> None:
    """A logger that raises on log_start is swallowed; refresh_one still runs
    and the normal outcome stats are returned."""
    from ingestion.daemon.ports.sync_logger import SqliteSyncLogger

    store = InMemoryTokenStore(
        {("alice@x", "teams"): {"cookies": _sso_cookies()}},
    )
    refresher = FakeRefresher(outputs=[CapturedTokens(graph_token=_scoped_jwt())])

    class BrokenLogger(SqliteSyncLogger):
        async def log_start(
            self,
            user_id: str,
            platform: str,
            *,
            sync_kind: str = "content_sync",
        ) -> str:
            raise RuntimeError("disk full")

    broken = BrokenLogger(tmp_path / "state.db")

    stats = await run_bulk_refresh_cycle(
        user_emails=["alice@x"],
        token_store=store,
        refresher=refresher,
        interval_seconds=60,
        sleep=_noop_sleep,
        sync_logger=broken,
    )

    assert stats.attempted == 1
    assert stats.succeeded == 1
