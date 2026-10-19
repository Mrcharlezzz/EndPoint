"""Coverage for :class:`DjangoCredentialStore` — the only Django-touching
implementation of :class:`CredentialPort`.

Two passes:

* The structural pass uses a stub-replaced ``PlatformCredential`` manager
  so the test runs in any env (CI + local without Postgres). We mock at
  the ``core.models.PlatformCredential`` import boundary the store goes
  through. This pins the manager-call shapes (``aget``, ``filter+aupdate``,
  ``filter+aiterator``) without standing up a live ORM.
* The ``django_db``-marked tests below exercise the same paths against
  a real ORM. They auto-skip when Postgres is unreachable (per
  ``tests/daemon/conftest.py``) and pass under CI's Postgres service.
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ingestion.daemon.ports.credential_port import CredentialRow
from ingestion.daemon.ports.credential_store import DjangoCredentialStore, _to_iso

# ---------------------------------------------------------------------------
# Helpers — _to_iso is independently testable (pure function)
# ---------------------------------------------------------------------------


def test_to_iso_returns_none_for_none() -> None:
    """The helper short-circuits on ``None`` so optional ORM columns
    project cleanly to ``Optional[str]``."""
    assert _to_iso(None) is None


def test_to_iso_calls_isoformat() -> None:
    """Any object with an ``isoformat`` attribute round-trips through
    the helper. Pin the contract: the helper does not type-check or
    coerce — it forwards."""
    moment = dt.datetime(2026, 4, 26, 0, 0, 0, tzinfo=dt.UTC)
    assert _to_iso(moment) == "2026-04-26T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Stub layer for the structural pass — fakes the Django ORM surface
# the store reaches for. Lets us assert call shapes without needing
# ``django.setup()``.
# ---------------------------------------------------------------------------


class _StubRow:
    """Plain-attribute mirror of ``PlatformCredential`` columns the
    store reads. The store never asks for anything else, so a one-fixed-
    surface stub is enough."""

    def __init__(
        self,
        *,
        user_email: str = "alice@x",
        platform: str = "moodle",
        is_paused: bool = False,
        is_expired: bool = False,
        expired_at: dt.datetime | None = None,
        last_refreshed_at: dt.datetime | None = None,
        encrypted_payload: bytes = b"\x00",
    ) -> None:
        self.user_email = user_email
        self.platform = platform
        self.is_paused = is_paused
        self.is_expired = is_expired
        self.expired_at = expired_at
        self.last_refreshed_at = last_refreshed_at
        self.encrypted_payload = encrypted_payload


class _DoesNotExist(Exception):  # noqa: N818
    """Stand-in for ``PlatformCredential.DoesNotExist`` so the store's
    ``except`` clause matches our raise. Name mirrors Django's exception
    rather than carrying an ``Error`` suffix."""


class _Cursor:
    """One element in the chain ``filter(...)`` returns. Captures the
    last ``filter()`` kwargs the store passed (so tests assert the SQL
    shape) and exposes the two terminals the store calls: ``aupdate``
    and ``aiterator``."""

    def __init__(self, manager: _Manager, filters: dict[str, Any]) -> None:
        self._mgr = manager
        self._filters = filters

    async def aupdate(self, **values: Any) -> int:
        # Record the chain (filters + aupdate values) so a test can
        # assert the inverse-state filter is in place.
        self._mgr.last_aupdate = (dict(self._filters), dict(values))
        # The stub semantics: 1 row affected unless the test seeds zero.
        return self._mgr.aupdate_rowcount

    def aiterator(self) -> AsyncIterator[_StubRow]:
        # The store calls ``async for row in queryset.aiterator()``;
        # the iterator must yield rows the test pre-loaded for the
        # current filter shape.
        rows = list(self._mgr.iter_rows_for(self._filters))
        return _aiter(rows)


async def _aiter(rows: list[_StubRow]) -> AsyncIterator[_StubRow]:
    for row in rows:
        yield row


class _Manager:
    """Stand-in for ``PlatformCredential.objects``.

    Tests configure ``aget_result`` and ``rows_for_filter`` to control
    what the store sees. The manager records the most recent call shapes
    so assertions can pin "the store filtered on the inverse value".
    """

    DoesNotExist = _DoesNotExist

    def __init__(self) -> None:
        self.aget_result: _StubRow | _DoesNotExist | None = None
        self.aget_kwargs: dict[str, Any] | None = None
        self.aupdate_rowcount = 1
        self.last_aupdate: tuple[dict[str, Any], dict[str, Any]] | None = None
        self._rows: list[tuple[dict[str, Any], list[_StubRow]]] = []

    async def aget(self, **kwargs: Any) -> _StubRow:
        self.aget_kwargs = dict(kwargs)
        if isinstance(self.aget_result, _DoesNotExist):
            raise self.aget_result
        if self.aget_result is None:
            raise _DoesNotExist
        return self.aget_result

    def filter(self, **kwargs: Any) -> _Cursor:
        return _Cursor(self, dict(kwargs))

    def add_rows_for(self, filters: dict[str, Any], rows: list[_StubRow]) -> None:
        self._rows.append((dict(filters), list(rows)))

    def iter_rows_for(self, filters: dict[str, Any]) -> list[_StubRow]:
        # Return the row set whose filters equal the chain's filters.
        # Tests load one set per call, so a strict match is fine.
        for stored_filters, rows in self._rows:
            if stored_filters == filters:
                return rows
        return []


class _StubModel:
    DoesNotExist = _DoesNotExist
    objects = _Manager()


def _install_stub_model(monkeypatch: pytest.MonkeyPatch) -> _Manager:
    """Replace ``core.models.PlatformCredential`` with a stub for the
    duration of one test. Returns the per-test fresh manager so the
    test can configure aget/rows.
    """
    # Each test gets its own freshly-built manager so call recordings
    # don't leak across cases.
    fresh = _Manager()

    class _LocalStub:
        DoesNotExist = _DoesNotExist
        objects = fresh

    # The store imports lazily as ``from core.models import PlatformCredential``.
    # Pre-installing a fake ``core.models`` module covers every method.
    fake_core = types.ModuleType("core")
    fake_models = types.ModuleType("core.models")
    fake_models.PlatformCredential = _LocalStub  # type: ignore[attr-defined]
    fake_core.models = fake_models  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core", fake_core)
    monkeypatch.setitem(sys.modules, "core.models", fake_models)
    return fresh


# ---------------------------------------------------------------------------
# Structural pass — runs unconditionally
# ---------------------------------------------------------------------------


async def test_get_returns_dataclass_when_row_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _install_stub_model(monkeypatch)
    mgr.aget_result = _StubRow(
        user_email="bob@x",
        platform="teams",
        is_paused=True,
        is_expired=False,
        expired_at=dt.datetime(2026, 4, 26, 1, 2, 3, tzinfo=dt.UTC),
        last_refreshed_at=dt.datetime(2026, 4, 25, 0, 0, 0, tzinfo=dt.UTC),
        encrypted_payload=b"abc",
    )
    store = DjangoCredentialStore()
    row = await store.get("bob@x", "teams")
    assert row == CredentialRow(
        user_email="bob@x",
        platform="teams",
        is_paused=True,
        is_expired=False,
        expired_at="2026-04-26T01:02:03+00:00",
        last_refreshed_at="2026-04-25T00:00:00+00:00",
        encrypted_payload=b"abc",
    )
    # Pin the call shape — the store must filter on the natural primary
    # key of (user_email, platform).
    assert mgr.aget_kwargs == {"user_email": "bob@x", "platform": "teams"}


async def test_get_returns_none_on_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _install_stub_model(monkeypatch)
    mgr.aget_result = _DoesNotExist()
    store = DjangoCredentialStore()
    assert await store.get("ghost@x", "moodle") is None


async def test_list_for_user_emits_one_row_per_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = _install_stub_model(monkeypatch)
    rows = [
        _StubRow(user_email="alice@x", platform="moodle"),
        _StubRow(user_email="alice@x", platform="teams", is_paused=True),
    ]
    mgr.add_rows_for({"user_email": "alice@x"}, rows)
    store = DjangoCredentialStore()
    out = await store.list_for_user("alice@x")
    assert [(r.platform, r.is_paused) for r in out] == [("moodle", False), ("teams", True)]


async def test_list_for_user_returns_empty_list_for_unknown_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Pins behaviour: unknown email -> empty list, NOT
    a raise. Service layer relies on this distinction (it raises
    UserNotFoundError itself when the list is empty)."""
    _install_stub_model(monkeypatch)
    store = DjangoCredentialStore()
    assert await store.list_for_user("nobody@x") == []


async def test_list_for_platform_emits_one_row_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = _install_stub_model(monkeypatch)
    rows = [
        _StubRow(user_email="alice@x", platform="teams"),
        _StubRow(user_email="bob@x", platform="teams"),
    ]
    mgr.add_rows_for({"platform": "teams"}, rows)
    store = DjangoCredentialStore()
    out = await store.list_for_platform("teams")
    assert [r.user_email for r in out] == ["alice@x", "bob@x"]


async def test_set_paused_filters_on_inverse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inverse filter is what makes set_paused idempotent — re-call
    on an already-paused row must update zero rows. Pin the SQL shape:
    ``filter(user_email=..., platform=..., is_paused=not paused)``
    chained to ``aupdate(is_paused=paused)``."""
    mgr = _install_stub_model(monkeypatch)
    store = DjangoCredentialStore()
    affected = await store.set_paused("alice@x", "moodle", paused=True)
    assert affected == 1
    assert mgr.last_aupdate == (
        {"user_email": "alice@x", "platform": "moodle", "is_paused": False},
        {"is_paused": True},
    )


async def test_set_paused_returns_zero_on_idempotent_call(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _install_stub_model(monkeypatch)
    mgr.aupdate_rowcount = 0
    store = DjangoCredentialStore()
    assert await store.set_paused("alice@x", "moodle", paused=True) == 0


async def test_set_paused_bulk_filters_on_inverse_for_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bulk variant: one UPDATE for all credentials of one user. Pin
    the inverse-state filter so an already-bulk-state user updates zero
    rows."""
    mgr = _install_stub_model(monkeypatch)
    mgr.aupdate_rowcount = 3
    store = DjangoCredentialStore()
    affected = await store.set_paused_bulk_for_user("alice@x", paused=False)
    assert affected == 3
    assert mgr.last_aupdate == (
        {"user_email": "alice@x", "is_paused": True},
        {"is_paused": False},
    )


async def test_set_paused_bulk_for_user_returns_zero_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] No matching rows -> 0. Pins the contract that
    bulk pause for a user with no credentials is a no-op (NOT a
    raise)."""
    mgr = _install_stub_model(monkeypatch)
    mgr.aupdate_rowcount = 0
    store = DjangoCredentialStore()
    assert await store.set_paused_bulk_for_user("ghost@x", paused=True) == 0


async def test_set_expired_filters_on_inverse(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _install_stub_model(monkeypatch)
    store = DjangoCredentialStore()
    affected = await store.set_expired("alice@x", "teams", expired=True)
    assert affected == 1
    assert mgr.last_aupdate == (
        {"user_email": "alice@x", "platform": "teams", "is_expired": False},
        {"is_expired": True},
    )


async def test_set_expired_idempotent_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """[TEST-COVERAGE] Idempotent re-call on already-expired row updates
    zero rows. Mirror of set_paused's idempotence; matches the inline
    docstring's "mirrors :meth:`set_paused` semantics" claim."""
    mgr = _install_stub_model(monkeypatch)
    mgr.aupdate_rowcount = 0
    store = DjangoCredentialStore()
    assert await store.set_expired("alice@x", "teams", expired=True) == 0


def test_row_to_dataclass_uses_isoformat_for_timestamps() -> None:
    """Static helper: pure function, easy to pin without the manager
    machinery."""

    class _R:
        user_email = "u"
        platform = "p"
        is_paused = False
        is_expired = True
        expired_at = dt.datetime(2026, 4, 26, tzinfo=dt.UTC)
        last_refreshed_at = None
        encrypted_payload = b"abc"

    out = DjangoCredentialStore._row_to_dataclass(_R())
    assert out.expired_at == "2026-04-26T00:00:00+00:00"
    assert out.last_refreshed_at is None


def test_row_to_dataclass_coerces_payload_to_bytes() -> None:
    """The ORM's BinaryField yields bytes-likes; the dataclass must
    own a real ``bytes`` so downstream consumers (cipher.decrypt)
    don't see a memoryview."""

    class _R:
        user_email = "u"
        platform = "p"
        is_paused = False
        is_expired = False
        expired_at = None
        last_refreshed_at = None
        encrypted_payload = bytearray(b"abc")

    out = DjangoCredentialStore._row_to_dataclass(_R())
    assert isinstance(out.encrypted_payload, bytes)
    assert out.encrypted_payload == b"abc"


# ---------------------------------------------------------------------------
# Live-ORM pass — auto-skips when Postgres isn't reachable. Same call
# shapes as the stubbed tests above, against a real DB so the
# Django-side query semantics are pinned end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_live_get_returns_none_when_missing() -> None:
    store = DjangoCredentialStore()
    assert await store.get("nobody@x", "moodle") is None


@pytest.mark.django_db(transaction=True)
async def test_live_set_paused_idempotent() -> None:
    from core.models import PlatformCredential

    await PlatformCredential.objects.acreate(
        user_email="alice@x",
        display_name="Alice",
        platform="moodle",
        encrypted_payload=b"\x00",
        is_paused=False,
    )
    store = DjangoCredentialStore()
    assert await store.set_paused("alice@x", "moodle", paused=True) == 1
    # Second call: row is now paused; the inverse filter matches zero.
    assert await store.set_paused("alice@x", "moodle", paused=True) == 0


@pytest.mark.django_db(transaction=True)
async def test_live_list_for_user_returns_dataclasses() -> None:
    from core.models import PlatformCredential

    for platform in ("moodle", "teams"):
        await PlatformCredential.objects.acreate(
            user_email="alice@x",
            display_name="Alice",
            platform=platform,
            encrypted_payload=b"\x00",
        )
    store = DjangoCredentialStore()
    rows = await store.list_for_user("alice@x")
    platforms = sorted(r.platform for r in rows)
    assert platforms == ["moodle", "teams"]
    # Every element is the slim mirror, never a Django model instance.
    assert all(isinstance(r, CredentialRow) for r in rows)
