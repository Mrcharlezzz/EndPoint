"""Unit tests for ``ingestion.daemon.auth.coordinator``.

Covers the polymorphic refresh dispatcher's full surface:

* SUCCESS with new_payload → ``token_store.update_credentials`` called.
* SUCCESS with new_payload=None → no write-back.
* EXPIRED → ``token_store.mark_expired`` called.
* NO_TOKENS / NO_COOKIES / ERROR → no persistence.
* Strategy raises → wrapped to ERROR via ``wrap_unexpected``.
* token_store.get_credentials raises → ERROR.
* token_store.update_credentials raises → ERROR.
* token_store.mark_expired raises → outcome stays EXPIRED but error
  message present.
* Per-user lock serialises concurrent refreshes for same user.
* Missing credential row → NO_COOKIES.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ingestion.daemon.auth.coordinator import (
    CredentialRefreshOutcome,
    refresh_credential,
)
from ingestion.daemon.auth.strategy import RefreshOutcome, RefreshResult


class _FakeTokenStore:
    """In-memory token store with optional injection of failures."""

    def __init__(
        self,
        creds: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.creds: dict[tuple[str, str], dict[str, Any]] = dict(creds or {})
        self.expired: set[tuple[str, str]] = set()
        self.update_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.mark_expired_calls: list[tuple[str, str]] = []
        self.get_failure: Exception | None = None
        self.update_failure: Exception | None = None
        self.mark_expired_failure: Exception | None = None

    async def get_credentials(self, user_id: str, platform: str) -> dict[str, Any] | None:
        if self.get_failure is not None:
            raise self.get_failure
        return self.creds.get((user_id, platform))

    async def update_credentials(
        self, user_id: str, platform: str, credentials: dict[str, Any]
    ) -> None:
        if self.update_failure is not None:
            raise self.update_failure
        self.update_calls.append((user_id, platform, credentials))
        self.creds[(user_id, platform)] = credentials

    async def mark_expired(self, user_id: str, platform: str) -> None:
        if self.mark_expired_failure is not None:
            raise self.mark_expired_failure
        self.mark_expired_calls.append((user_id, platform))
        self.expired.add((user_id, platform))


class _ScriptedStrategy:
    """Returns a queued list of RefreshResult / Exception, FIFO."""

    def __init__(self, outputs: list[RefreshResult | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
        self.calls.append((email, current_payload))
        if not self.outputs:
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)
        nxt = self.outputs.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def is_refresh_supported(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# CredentialRefreshOutcome dataclass
# ---------------------------------------------------------------------------


def test_credential_refresh_outcome_is_frozen_with_slots() -> None:
    """Frozen + slots so the bulk loop's aggregator never mutates."""
    import dataclasses

    out = CredentialRefreshOutcome(
        user_email="u@x",
        platform="teams",
        outcome=RefreshOutcome.SUCCESS,
    )
    assert out.error is None  # default
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.outcome = RefreshOutcome.ERROR  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SUCCESS path
# ---------------------------------------------------------------------------


async def test_success_with_new_payload_writes_through() -> None:
    """SUCCESS + new_payload → token_store.update_credentials called."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"old": True}})
    new_payload = {"graph_token": "new"}
    strategy = _ScriptedStrategy(
        [RefreshResult(outcome=RefreshOutcome.SUCCESS, new_payload=new_payload)]
    )

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.SUCCESS
    assert out.error is None
    assert store.update_calls == [("u@x", "teams", new_payload)]
    assert ("u@x", "teams") not in store.expired


async def test_success_with_none_payload_skips_write() -> None:
    """SUCCESS + new_payload=None (probe-not-rotate) → no write."""
    store = _FakeTokenStore(creds={("u@x", "moodle"): {"token": "t"}})
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.SUCCESS, new_payload=None)])

    out = await refresh_credential(
        user_email="u@x",
        platform="moodle",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    # NOTE: looking at coordinator.py lines 143-144, the only path
    # back from a SUCCESS-with-no-payload is the fallthrough; the
    # outcome IS SUCCESS but no write happened. Pin both halves.
    assert out.outcome is RefreshOutcome.SUCCESS
    assert store.update_calls == []


# ---------------------------------------------------------------------------
# EXPIRED path
# ---------------------------------------------------------------------------


async def test_expired_calls_mark_expired() -> None:
    store = _FakeTokenStore(creds={("u@x", "telegram"): {"bot_token": "t"}})
    strategy = _ScriptedStrategy(
        [
            RefreshResult(
                outcome=RefreshOutcome.EXPIRED,
                error_message="proxy 401",
            )
        ]
    )

    out = await refresh_credential(
        user_email="u@x",
        platform="telegram",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.EXPIRED
    assert out.error == "proxy 401"
    assert store.mark_expired_calls == [("u@x", "telegram")]
    assert ("u@x", "telegram") in store.expired


async def test_expired_with_mark_failure_returns_error_message() -> None:
    """When mark_expired itself errors, outcome stays EXPIRED but the
    error string surfaces the persistence failure."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    store.mark_expired_failure = RuntimeError("disk full")
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.EXPIRED)])

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.EXPIRED
    assert out.error is not None
    assert "mark_expired" in out.error
    assert "RuntimeError" in out.error


# ---------------------------------------------------------------------------
# NO_TOKENS / NO_COOKIES / ERROR — no write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [RefreshOutcome.NO_TOKENS, RefreshOutcome.NO_COOKIES, RefreshOutcome.ERROR],
)
async def test_non_success_non_expired_skips_persistence(
    outcome: RefreshOutcome,
) -> None:
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    strategy = _ScriptedStrategy([RefreshResult(outcome=outcome, error_message="probe diag")])

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is outcome
    assert out.error == "probe diag"
    assert store.update_calls == []
    assert store.mark_expired_calls == []


# ---------------------------------------------------------------------------
# Missing credential row → NO_COOKIES
# ---------------------------------------------------------------------------


async def test_missing_credential_row_returns_no_cookies() -> None:
    """token_store.get_credentials returns None → NO_COOKIES; strategy
    is never invoked."""
    store = _FakeTokenStore()  # empty
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.SUCCESS)])

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.NO_COOKIES
    assert out.error == "no credential row"
    # Strategy not invoked because creds returned None.
    assert strategy.calls == []


# ---------------------------------------------------------------------------
# Strategy / store failures
# ---------------------------------------------------------------------------


async def test_strategy_unexpected_exception_collapses_to_error() -> None:
    """A strategy that raises is wrapped via wrap_unexpected → ERROR."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    strategy = _ScriptedStrategy([RuntimeError("internal")])

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.ERROR
    assert out.error is not None
    assert "RuntimeError" in out.error
    assert store.update_calls == []
    assert store.mark_expired_calls == []


async def test_get_credentials_error_returns_error_outcome() -> None:
    """A token_store.get_credentials failure → ERROR with typed message."""
    store = _FakeTokenStore()
    store.get_failure = ConnectionError("postgres unreachable")
    strategy = _ScriptedStrategy([])

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.ERROR
    assert out.error is not None
    assert "token_store.get_credentials" in out.error
    assert "ConnectionError" in out.error
    assert strategy.calls == []


async def test_update_credentials_error_returns_error_outcome() -> None:
    """A token_store.update_credentials failure on the SUCCESS path
    surfaces as ERROR (not SUCCESS)."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    store.update_failure = RuntimeError("disk full")
    strategy = _ScriptedStrategy(
        [
            RefreshResult(
                outcome=RefreshOutcome.SUCCESS,
                new_payload={"graph_token": "new"},
            )
        ]
    )

    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert out.outcome is RefreshOutcome.ERROR
    assert out.error is not None
    assert "token_store.update_credentials" in out.error


# ---------------------------------------------------------------------------
# Per-user lock
# ---------------------------------------------------------------------------


async def test_per_user_lock_serialises_concurrent_calls() -> None:
    """Two coroutines refreshing the same user share the same
    asyncio.Lock; the second waits until the first releases."""
    started_at: list[float] = []
    finished_at: list[float] = []

    class _SlowStrategy:
        async def refresh(self, email: str, *, current_payload: dict[str, Any]) -> RefreshResult:
            del email, current_payload
            started_at.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)
            finished_at.append(asyncio.get_event_loop().time())
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
    # The second strategy.refresh started AFTER the first finished.
    assert a.outcome is RefreshOutcome.SUCCESS
    assert b.outcome is RefreshOutcome.SUCCESS
    assert started_at[1] >= finished_at[0]


async def test_no_lock_path_executes_normally() -> None:
    """Without a user_lock argument the coordinator still works."""
    store = _FakeTokenStore(creds={("u@x", "teams"): {"x": 1}})
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.SUCCESS)])
    out = await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
        user_lock=None,
    )
    assert out.outcome is RefreshOutcome.SUCCESS


# ---------------------------------------------------------------------------
# Outcome carries both email + platform
# ---------------------------------------------------------------------------


async def test_outcome_carries_email_and_platform() -> None:
    """The CredentialRefreshOutcome carries email + platform so the
    bulk-loop aggregator can pivot on platform without re-querying."""
    store = _FakeTokenStore(creds={("alice@x", "moodle"): {"token": "t"}})
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.SUCCESS)])
    out = await refresh_credential(
        user_email="alice@x",
        platform="moodle",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )
    assert out.user_email == "alice@x"
    assert out.platform == "moodle"


# ---------------------------------------------------------------------------
# Strategy receives current payload
# ---------------------------------------------------------------------------


async def test_strategy_receives_loaded_credential_payload() -> None:
    """The coordinator forwards the loaded payload to the strategy."""
    payload = {"token": "abc", "team_ids": ["t1", "t2"]}
    store = _FakeTokenStore(creds={("u@x", "teams"): payload})
    strategy = _ScriptedStrategy([RefreshResult(outcome=RefreshOutcome.SUCCESS)])

    await refresh_credential(
        user_email="u@x",
        platform="teams",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    assert strategy.calls == [("u@x", payload)]


# ---------------------------------------------------------------------------
# Non-success outcome with no new_payload (e.g. SUCCESS but None)
# Specifically pin the (SUCCESS, None) edge so coverage hits it.
# ---------------------------------------------------------------------------


async def test_success_with_none_payload_falls_through_to_default_branch() -> None:
    """SUCCESS-with-None-payload doesn't write but the outcome surfaces
    via the fallthrough branch with error_message preserved."""
    store = _FakeTokenStore(creds={("u@x", "moodle"): {"token": "t"}})
    strategy = _ScriptedStrategy(
        [
            RefreshResult(
                outcome=RefreshOutcome.SUCCESS,
                new_payload=None,
                error_message=None,
            )
        ]
    )

    out = await refresh_credential(
        user_email="u@x",
        platform="moodle",
        token_store=store,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
    )

    # Falls into the final return statement with outcome=SUCCESS but
    # no persistence side-effect.
    assert out.outcome is RefreshOutcome.SUCCESS
    assert out.error is None
    assert store.update_calls == []
