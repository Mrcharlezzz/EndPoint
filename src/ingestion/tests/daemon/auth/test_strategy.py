"""Unit tests for ``ingestion.daemon.auth.strategy``.

Targets 100% line + branch coverage on the protocol module: the
``RefreshResult`` dataclass shape, the ``RefreshError`` hierarchy, the
``RefreshOutcome`` re-export, the ``RefreshStrategy`` Protocol's
runtime-checkable behaviour, and the ``wrap_unexpected`` helper.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from ingestion.daemon.auth.strategy import (
    AuthenticationProbeError,
    RefreshError,
    RefreshOutcome,
    RefreshResult,
    RefreshStrategy,
    RefreshUnsupportedError,
    wrap_unexpected,
)

# ---------------------------------------------------------------------------
# RefreshOutcome — re-export sanity
# ---------------------------------------------------------------------------


def test_refresh_outcome_values_exposed() -> None:
    """The strategy module re-exports RefreshOutcome from refresh.py.
    Pin every member exists with its canonical string value."""
    assert RefreshOutcome.SUCCESS.value == "success"
    assert RefreshOutcome.NO_COOKIES.value == "no_cookies"
    assert RefreshOutcome.NO_TOKENS.value == "no_tokens"
    assert RefreshOutcome.EXPIRED.value == "expired"
    assert RefreshOutcome.ERROR.value == "error"


def test_refresh_outcome_is_str_enum() -> None:
    """StrEnum so log.info(outcome=outcome.value) works without coercion."""
    assert isinstance(RefreshOutcome.SUCCESS, str)
    assert RefreshOutcome.SUCCESS == "success"


# ---------------------------------------------------------------------------
# RefreshResult dataclass shape
# ---------------------------------------------------------------------------


def test_refresh_result_is_frozen_dataclass() -> None:
    """Frozen so a strategy can't mutate the returned object after the
    coordinator sees it."""
    result = RefreshResult(outcome=RefreshOutcome.SUCCESS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = RefreshOutcome.ERROR  # type: ignore[misc]


def test_refresh_result_uses_slots() -> None:
    """slots=True keeps each result lightweight — bulk loops can churn
    thousands per cycle."""
    assert "__slots__" in RefreshResult.__dict__
    result = RefreshResult(outcome=RefreshOutcome.SUCCESS)
    assert not hasattr(result, "__dict__")


def test_refresh_result_default_payload_and_error() -> None:
    """new_payload defaults to None (probe-not-rotate doesn't write
    back). error_message defaults to None on success path. latency_ms
    defaults to 0.0."""
    result = RefreshResult(outcome=RefreshOutcome.SUCCESS)
    assert result.new_payload is None
    assert result.error_message is None
    assert result.latency_ms == 0.0


def test_refresh_result_carries_all_fields() -> None:
    payload = {"token": "abc"}
    result = RefreshResult(
        outcome=RefreshOutcome.SUCCESS,
        new_payload=payload,
        error_message=None,
        latency_ms=42.5,
    )
    assert result.outcome is RefreshOutcome.SUCCESS
    assert result.new_payload is payload
    assert result.error_message is None
    assert result.latency_ms == 42.5


def test_refresh_result_field_set_is_pinned() -> None:
    """Pin the field surface — adding a new field needs a coordinated
    edit on the coordinator + bulk_refresh."""
    expected = {"outcome", "new_payload", "error_message", "latency_ms"}
    actual = {f.name for f in dataclasses.fields(RefreshResult)}
    assert actual == expected


# ---------------------------------------------------------------------------
# RefreshError hierarchy
# ---------------------------------------------------------------------------


def test_refresh_error_is_exception_subclass() -> None:
    assert issubclass(RefreshError, Exception)


def test_authentication_probe_error_chains_to_refresh_error() -> None:
    """A "we couldn't even ask the platform" failure (DNS, network blip,
    TLS) — distinct from RefreshOutcome.EXPIRED ("probe succeeded; the
    platform said dead")."""
    err = AuthenticationProbeError("dns lookup failed")
    assert isinstance(err, RefreshError)
    assert isinstance(err, Exception)


def test_refresh_unsupported_error_chains_to_refresh_error() -> None:
    """Defensive: the bulk loop pre-filters on supports-flag, but a
    hand-wired test that skips the gate gets a clear error."""
    err = RefreshUnsupportedError("not implemented")
    assert isinstance(err, RefreshError)


def test_refresh_error_can_be_raised_and_caught() -> None:
    with pytest.raises(RefreshError):
        raise AuthenticationProbeError("boom")
    with pytest.raises(RefreshError):
        raise RefreshUnsupportedError("nope")


# ---------------------------------------------------------------------------
# RefreshStrategy Protocol — runtime_checkable + structural conformance
# ---------------------------------------------------------------------------


class _MinimalStrategy:
    """Smallest object that satisfies RefreshStrategy structurally."""

    async def refresh(
        self,
        email: str,
        *,
        current_payload: dict[str, Any],
    ) -> RefreshResult:
        del email, current_payload
        return RefreshResult(outcome=RefreshOutcome.SUCCESS)

    async def is_refresh_supported(self) -> bool:
        return True


def test_minimal_strategy_satisfies_runtime_checkable_protocol() -> None:
    """The Protocol is decorated @runtime_checkable so isinstance checks
    work — used by the coordinator as defense-in-depth."""
    s = _MinimalStrategy()
    assert isinstance(s, RefreshStrategy)


def test_object_missing_methods_does_not_satisfy_protocol() -> None:
    """An object with neither method must NOT satisfy the Protocol."""

    class _NotAStrategy:
        pass

    assert not isinstance(_NotAStrategy(), RefreshStrategy)


def test_object_with_partial_surface_does_not_satisfy_protocol() -> None:
    """Missing one of the two required methods rejects the
    isinstance check."""

    class _OnlyRefresh:
        async def refresh(
            self,
            email: str,
            *,
            current_payload: dict[str, Any],
        ) -> RefreshResult:
            del email, current_payload
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)

    assert not isinstance(_OnlyRefresh(), RefreshStrategy)


async def test_strategy_protocol_methods_are_async() -> None:
    """Both methods are async — the bulk loop drives them with await
    rather than ``asyncio.to_thread``."""
    s = _MinimalStrategy()
    result = await s.refresh("u@x", current_payload={})
    assert result.outcome is RefreshOutcome.SUCCESS
    assert await s.is_refresh_supported() is True


# ---------------------------------------------------------------------------
# wrap_unexpected — defensive boundary helper
# ---------------------------------------------------------------------------


class _RaisingStrategy:
    """Strategy that raises an unexpected exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def refresh(
        self,
        email: str,
        *,
        current_payload: dict[str, Any],
    ) -> RefreshResult:
        del email, current_payload
        raise self._exc

    async def is_refresh_supported(self) -> bool:
        return True


async def test_wrap_unexpected_passes_through_normal_result() -> None:
    """Happy path: a strategy returning a RefreshResult is returned
    unchanged."""
    s = _MinimalStrategy()
    result = await wrap_unexpected(s, "u@x", current_payload={})
    assert result.outcome is RefreshOutcome.SUCCESS


async def test_wrap_unexpected_converts_exception_to_error_result() -> None:
    """An unexpected exception (RuntimeError, KeyError, etc.) gets
    wrapped to RefreshOutcome.ERROR with a typed error_message."""
    s = _RaisingStrategy(RuntimeError("internal explosion"))
    result = await wrap_unexpected(s, "u@x", current_payload={})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.new_payload is None
    assert result.error_message is not None
    assert "RuntimeError" in result.error_message
    assert "internal explosion" in result.error_message


async def test_wrap_unexpected_truncates_long_exception_text() -> None:
    """The wrap truncates at 200 chars so a noisy backtrace doesn't
    blow up the structured-log payload size."""
    long_message = "x" * 500
    s = _RaisingStrategy(ValueError(long_message))
    result = await wrap_unexpected(s, "u@x", current_payload={})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    # 200-char cap on the wrapped message.
    assert len(result.error_message) <= 200


async def test_wrap_unexpected_handles_keyerror() -> None:
    """KeyError on a missing payload field gets caught — would
    otherwise propagate and take down the worker."""
    s = _RaisingStrategy(KeyError("missing_token_key"))
    result = await wrap_unexpected(s, "u@x", current_payload={})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "KeyError" in result.error_message


async def test_wrap_unexpected_handles_arbitrary_exception_subclass() -> None:
    """Any Exception subclass (custom or stdlib) collapses cleanly."""

    class _CustomBoomError(Exception):
        pass

    s = _RaisingStrategy(_CustomBoomError("bespoke failure"))
    result = await wrap_unexpected(s, "u@x", current_payload={})
    assert result.outcome is RefreshOutcome.ERROR
    assert result.error_message is not None
    assert "_CustomBoomError" in result.error_message


async def test_wrap_unexpected_passes_payload_through() -> None:
    """The current_payload is forwarded to the strategy unchanged."""

    seen: dict[str, Any] = {}

    class _Capturer:
        async def refresh(
            self,
            email: str,
            *,
            current_payload: dict[str, Any],
        ) -> RefreshResult:
            seen["email"] = email
            seen["payload"] = current_payload
            return RefreshResult(outcome=RefreshOutcome.SUCCESS)

        async def is_refresh_supported(self) -> bool:
            return True

    payload = {"token": "abc", "csa_token": "def"}
    await wrap_unexpected(_Capturer(), "alice@x", current_payload=payload)
    assert seen["email"] == "alice@x"
    assert seen["payload"] is payload


# ---------------------------------------------------------------------------
# Module __all__ surface
# ---------------------------------------------------------------------------


def test_module_all_exposes_expected_names() -> None:
    """Pin __all__ so a reordering (or accidental removal) is visible."""
    from ingestion.daemon.auth import strategy as strat_mod

    assert "RefreshOutcome" in strat_mod.__all__
    assert "RefreshResult" in strat_mod.__all__
    assert "RefreshStrategy" in strat_mod.__all__
    assert "RefreshError" in strat_mod.__all__
    assert "RefreshUnsupportedError" in strat_mod.__all__
    assert "AuthenticationProbeError" in strat_mod.__all__
    assert "wrap_unexpected" in strat_mod.__all__
